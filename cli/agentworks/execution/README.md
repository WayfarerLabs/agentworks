# Execution

`agentworks.execution` is how core runs commands on a managed VM. It reaches the VM only through the
installed OpenSSH client at the VM's tailnet address, and every request is sent on the client's
stdin, never on a command line or in the environment. The commands that use it today are `vm exec`,
`vm shell`, `agent exec`, `agent shell`, `vm verify-connection` and `vm port-forward`. Everything
else still reaches VMs through the transport stack it will replace, which this package must never
import.

## Use

```python
import agentworks.execution as execution
from agentworks.execution.access import Admin
from agentworks.execution.requests import Command

execution.resolve_ssh()  # first: an unsupported client refuses before anything else
access = execution.vm_access(vm, config, Admin())  # passive: reads and spawns nothing
result = access.run(Command(("systemctl", "is-active", "ssh"), deadline=10), check=True)
```

Always call `vm_access` and `resolve_ssh` through the package attribute, as above. Tests replace
those two attributes once (the `vm_access_recorder` fixture) and every manager follows.

`VMAccess` has four operations:

| Operation                                   | Returns                  | Use                                                       |
| ------------------------------------------- | ------------------------ | --------------------------------------------------------- |
| `run(request, check=False)`                 | `Result`                 | Buffered: up to 1 MiB per stream, then `OUTPUT_LIMIT`     |
| `stream(request, stdin=, stdout=, stderr=)` | the guest's status       | Live output; `stream_to_operator` wires the process stdio |
| `attach(login_shell)`                       | the shell's exit status  | The operator's terminal, under `guarded_terminal`         |
| `forward(forwards, verbose=False)`          | the client's exit status | Blocking `ssh -N -L`; the caller owns the interrupt       |

A request is a `Command` (a literal argv, run with no shell), a `Script` (source text run by `sh`,
`bash` or the identity's default shell, optionally as a login shell) or a `LoginShell` (valid only
for `attach`). Each takes `env` and `cwd` (absolute; `None` is the home directory); `Command` and
`Script` also take `deadline` (seconds of local waiting; it never claims the guest command stopped).
`vm exec` sends a `Script` under the identity's default shell, with several arguments shell-joined.

## What a request becomes

Every connection runs the same constant command, `python3 -I -S -c '<stage 0>'` (`ssh_process.py`).
Stage 0 is a one-line program that reads the launcher's source from stdin with exact-length reads
and runs it. Its reader is recursive, so a pathological drip of tiny reads fails safely, as not
delivered, instead of running. The launcher, `agw_launch.py`, reads the request, resets what the
payload inherits, runs the payload as a child and reports completion. The client writes three things
to ssh's stdin in order: the launcher source, the JSON envelope (`envelope.py`), and the caller's
input untouched. A frame is ten ASCII digits then that many bytes, so the payload's stdin starts at
the caller's first byte.

The guest's account shell may print or export before stage 0 runs. The launcher brackets the
payload's output with marker lines (`0x1E`, a per-request nonce, a word, a newline), and the client
drops everything before the start marker and after the closing lines. The payload gets a rebuilt
environment (a fixed `PATH`, `LANG=C.UTF-8`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `SHLVL=1`, then the
request's `env`), a fixed umask, default signal dispositions and no descriptors above 2. A payload
running as the same user could learn the nonce, which only lets it misreport its own result.

## Outcomes

Completion comes from the markers, never from ssh's exit status:

- `Completed(status)`: the payload ran; a guest exit of 255 is a completion, not a lost connection.
- `NotDelivered(reason)`: the payload never started (`PYTHON_MISSING`, `STDLIB_MISSING`,
  `LAUNCHER_FAILED`, or `REFUSED` with the guest's refusal code).
- `Unknown(reason)`: it may have run (`CONNECTION`, `HOST_KEY`, `TRUNCATED`, `DEADLINE`,
  `OUTPUT_LIMIT`).

`Result.require_completed` and `run(check=True)` turn the rest into typed errors (`errors.py`):
`VMNotReady`, `TrustMismatch`, `RequestRefused`, `ExecutionIncomplete`, `CommandFailed`. No error,
repr or hint holds a stream, an environment value or script text; the only transcript an error may
show is the last lines of ssh's own diagnostics, which the client keeps in a private log (`-E`)
apart from the payload's output. On a connection failure, `VMAccess` reads the operator's key file
once to decide whether to hint at `ssh-add`, and only then. `stream` reports a refusal the way a
shell does: 127 for a missing command, 126 for any other `exec` failure, 125 for a working
directory. A consumer that leaves (`| head`) gets what ssh gave it. For stdout the client closes its
read end, so a writer takes `SIGPIPE`, while a quiet payload still runs to its end and `stream`
reports its own status. A departed stderr ends the request once held stdout is delivered, since ssh
does not end the channel for it, unless its trailer had already arrived; then stdout is read to its
end and the guest's status stands. With no status to report, `stream` returns 141.

## Client and policy

`client.py` resolves the OpenSSH client once per process to an absolute path and a version, and
requires 8.5 or newer. On POSIX it is the first `ssh` on `PATH` (skipping empty and relative
elements). On Windows `PATH` is never searched, because it commonly finds the MSYS build bundled
with Git for Windows; the native Win32-OpenSSH locations are tried in order. `agw doctor` shows the
verdict.

`ssh_process.py` builds the argv under `-F none`, so no operator or system configuration applies,
with the explicit identity (`IdentitiesOnly`), `BatchMode`, publickey-only authentication, the VM's
host-key trust options and keepalives on every connection. Its `spawn` is the only place a client is
started. `pump.py` drives a started client through pipes: one thread over non-blocking pipes, with
the operator's stdin reaching it through a daemon thread that reads the raw stream. A deadline or an
output limit kills the client at once. A sink that would block (the operator's own stdout is a
non-blocking pipe) is waited on, never dropped.

## Terminal handover

ssh takes the window size and terminal modes from its own stdin, so the operator's terminal has to
be the terminal half's stdin, but the request must still travel privately. `attach` therefore opens
two connections. The first sends the request marked `park`: the launcher creates a private socket
directory under `/tmp`, listens and waits. The second runs the terminal half, whose stage 0 connects
to that socket, collects the launcher and the request, and execs the login shell on the terminal. No
guest text reaches a command line, and the parked launcher removes its directory when the second
connection is served, when the client closes its end, after 30 seconds, or on `SIGHUP` or `SIGTERM`.
The client gives up on a parking connection that never reports `parked` within 60 seconds (its own
bound for connecting and running the account's startup files; the 30 seconds above start only after
`parked`), and raises before the terminal is touched.

## Host-key trust

`trust.py` owns the per-VM host-key store (`~/.config/agentworks/known_hosts/vm-<name>`, records
under the alias `agentworks-vm-<name>`) and the OpenSSH options each trust mode selects. The legacy
stack calls it; it imports nothing legacy.

- `TrustBinding(mode, vm_name, provisioning_file=None)` names how a connection to a VM establishes
  trust. `VM` verifies against the store with `StrictHostKeyChecking=yes` and raises `TrustMissing`
  (`errors.py`) when the VM has no record, never falling back to first use. `ENROLL` is create's
  first connection only and writes the VM's freshly reset record under `accept-new`. `UNVERIFIED` is
  cloud provisioning before the VM is in the store: it accepts a key once into a private file the
  create owns (`provisioning_binding`) and deletes that file when the create ends.
- `HOST_TRUST` marks a connection to a host, such as Remote Lima's, rather than a VM. It keeps the
  operator's own SSH configuration and `known_hosts`.
- `ssh_options(trust)` returns the `-o` arguments. They pin the global known-hosts file to the null
  device and turn host-key updates, hashing and IP checks off, so no configuration file can change
  them. VM options are read when a command is spawned, not when the transport is built.
- `entry_options(vm_name)` gives the managed SSH config lines that make the operator's own `ssh`
  verify against the store once the VM has a record.
- `begin_enrollment`, `write_records`, `stored_keys` and `remove` are the record's lifecycle. Writes
  are atomic and private.

Every `SSHTransport` and `SSHTarget` is constructed with an explicit trust choice, so a connection
that skips the decision fails to type-check. `TrustMissing` is a state error, not an `SSHError`, so
best-effort and retrying SSH handlers cannot absorb it. Operator guidance is in the command
reference and the 0.20 upgrade guide.

## Independence

Nothing in this package imports `agentworks.transports`, `agentworks.ssh`, `agentworks.remote_exec`,
`agentworks.harness_setup.runner`, `agentworks.native_files` or
`agentworks.plugins.proxmox.transport`. Three guards hold that: a fresh-interpreter test imports
every submodule with them blocked (`tests/execution/test_independence.py`), a conftest blocks them
for every test in `tests/execution/`, and a static check rejects any import of them in the files
already migrated onto this package (`tests/test_execution_import_boundary.py`). A slice that
migrates a file adds it to that list. The tests are described in
[`tests/execution/README.md`](../../tests/execution/README.md).
