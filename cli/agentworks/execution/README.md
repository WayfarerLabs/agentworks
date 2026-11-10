# Execution

`agentworks.execution` is how core runs commands on a managed VM. It reaches the VM only through the
installed OpenSSH client at the VM's tailnet address, and every request is sent on the client's
stdin, never on a command line or in the environment. The commands that use it today are `vm exec`,
`vm shell`, `agent exec`, `agent shell`, `vm verify-connection` and `vm port-forward`, plus create's
first contact (Phase A), the VM contract (`agentworks.vms.contract`) that create and reinit run,
`vm confirm-release`, and the wait for a VM to answer (`reachability.wait`) after `vm start`, the
activation gate's automatic start, `vm rekey` and `vm recover`. Everything else still reaches VMs
through the legacy transport stack (`agentworks.transports`), which this package must never import.
A platform's own channel is not part of either: core reaches it through `VMPlatform.run_as_root` and
`native_shell`.

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

`VMAccess` has four operations, and `files()` (below) for file operations built on `run`:

| Operation                                     | Returns                  | Use                                                       |
| --------------------------------------------- | ------------------------ | --------------------------------------------------------- |
| `run(request, stdout=, stderr=, check=False)` | `Result`                 | Buffered, into each stream's sink                         |
| `stream(request, stdin=, stdout=, stderr=)`   | the guest's status       | Live output; `stream_to_operator` wires the process stdio |
| `attach(login_shell)`                         | the shell's exit status  | The operator's terminal, under `guarded_terminal`         |
| `forward(forwards, verbose=False)`            | the client's exit status | Blocking `ssh -N -L`; the caller owns the interrupt       |

A request is a `Command` (a literal argv, run with no shell), a `Script` (source text run by `sh`,
`bash` or the identity's default shell), a `PythonProgram` (core's own Python source and its
arguments) or a `LoginShell` (valid only for `attach`). Each takes `env` and `cwd` (absolute; `None`
is the home directory); the first three also take `deadline` (seconds of local waiting; it never
claims the guest command stopped). `vm exec` sends a `Script` under the identity's default shell,
with several arguments shell-joined.

`Script` and `PythonProgram` take `login` and `interactive`, for a payload that must see the
environment the account's login or interactive startup builds, as a harness's own shell would. A
script's source and a program's source travel in the request and reach the guest as an in-memory
file, never as an argument. An interactive `Script` is sourced by an interactive shell
(`<shell> [-l] -i -c '. "$0"' <file>`). A `PythonProgram` runs as
`/usr/bin/python3 -I -S <file> <args>`, the system interpreter with no Python startup; with either
flag the identity's default shell runs that startup first and then execs it. The source is staged at
descriptor 100 or above, clear of the low descriptors a startup file may close or reuse. Staging
needs a soft open-file limit (`RLIMIT_NOFILE`) above 100; below it the request is
`NotDelivered(LAUNCHER_FAILED)` with an error naming the limit.

`run`'s sinks are `CaptureOutput(limit=1 MiB)`, the default, which keeps up to `limit` bytes and
makes more `Unknown(OUTPUT_LIMIT)`, and `DiscardOutput()`, which keeps nothing while the stream is
still read to its closing marker. A caller whose output can exceed 1 MiB sets the limit per call.
Plugins hold an access, so `run` checks what their types promise: input that is neither `NoInput()`
nor `Bytes` of `bytes`, any other sink, or a limit that is not a positive `int` is a
`ValidationError` before anything resolves.

`Command`, `Script` and `PythonProgram` take the payload's input as `stdin`: `NoInput()`, the
default, or `Bytes(data)`, which the client writes after the request and then closes, so the payload
reads it from its first byte. Its size is bounded only by memory; the pump writes it to ssh in 64
KiB pieces, as fast as ssh takes them, and the request's `deadline` bounds how long that takes.
`stream` takes the operator's stdin as its own argument instead, and refuses a request that carries
input with a `ValidationError`. Input is never formatted into a log record or an error, and `Bytes`
keeps `data` out of its `repr`.

They also take `elevated`, which runs the whole operation as root under `sudo -n`: it is the prefix
of the constant first-stage command (below), not part of the request message, and the launcher
rebuilds the payload's environment from root's passwd entry. An agent-bound access refuses an
elevated request with `PermissionDenied` before anything resolves, and the guest's `sudo -n` is the
second layer. `LoginShell` has no such option, so an attach never elevates.

`reachability.wait(access, wording=)` is the one retry loop: it polls a fixed `true` probe (16
attempts, 10 seconds each, 5 seconds apart, a second probe confirming an answer that followed a
failure) and returns whether the VM answered. It polls only `CONNECTION`, `TRUNCATED` and `DEADLINE`
and raises every other outcome at once, so a changed host key or a refused key is neither waited out
nor repeated against the VM's sshd. The caller decides what an exhausted wait means.

`execution.enrollment_access(vm, config, log=)` is the admin's access in Enroll mode, which may
record a new host key. Create's first contact is its only caller; every other access verifies
against the store.

`vm_access(..., log=)` takes an operation log's record callable, `SSHLogger.record` today, which
`run` calls once per request before raising for its outcome, so failures are logged. The block is
`oplog.format_record`'s: the step label, the request's shape (the argv, or a script's shell and
login flag, with the working directory, the environment's names and the deadline), the completion,
both streams and ssh's diagnostics. A request with `log_streams=False` has neither stream in its
record, for a payload whose output is file content or a command line that must not be retained. No
environment value, script source or input reaches the formatter; the logger redacts the secrets
registered when it was constructed, at its one sink.

## What a request becomes

Every connection runs the same constant command, `python3 -I -S -c '<stage 0>'` (`ssh_process.py`),
behind `sudo -n` for an elevated request. Stage 0 is a one-line program that reads the launcher's
source from stdin with exact-length reads and runs it. Its reader is recursive, so a pathological
drip of tiny reads fails safely, as not delivered, instead of running. The launcher,
`agw_launch.py`, reads the request, resets what the payload inherits, runs the payload as a child
and reports completion. The client writes three things to ssh's stdin in order: the launcher source,
the JSON envelope (`envelope.py`), and the caller's input untouched. A frame is ten ASCII digits
then that many bytes, so the payload's stdin starts at the caller's first byte.

The guest's account shell may print or export before stage 0 runs. The launcher brackets the
payload's output with marker lines (`0x1E`, a per-request nonce, a word, a newline), and the client
drops everything before the start marker and after the closing lines. The payload gets a rebuilt
environment (a fixed `PATH`, `LANG=C.UTF-8`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `SHLVL=1`, then the
request's `env`), a fixed umask, default signal dispositions and no descriptor above 2 but its
source file's. A payload running as the same user could learn the nonce, which only lets it
misreport its own result.

## Outcomes

Completion comes from the markers, never from ssh's exit status:

- `Completed(status)`: the payload ran; a guest exit of 255 is a completion, not a lost connection.
- `NotDelivered(reason)`: the payload never started (`PYTHON_MISSING`, `STDLIB_MISSING`,
  `LAUNCHER_FAILED`, or `REFUSED` with the guest's refusal code).
- `Unknown(reason)`: it may have run (`CONNECTION`, `HOST_KEY`, `TRUNCATED`, `DEADLINE`,
  `OUTPUT_LIMIT`). `AUTH_FAILED` is the exception the evidence allows: ssh's 255 with no start
  marker and `Permission denied (publickey` in its own log means the VM refused the configured key,
  so nothing ran. `CONNECTION` stays "may have run": the start marker is written just before the
  payload starts, so a connection lost then can drop it. Both errors' hints name `agw vm recover`.

`Result.require_completed` and `run(check=True)` turn the rest into typed errors (`errors.py`):
`VMNotReady`, `TrustMismatch`, `RequestRefused`, `ExecutionIncomplete`, `CommandFailed`. No error,
repr or hint holds a stream, an environment value or script text; the only transcript an error may
show is the last lines of ssh's own diagnostics, which the client keeps in a private log (`-E`)
apart from the payload's output. On a connection or authentication failure, `VMAccess` reads the
operator's key file once to decide whether to hint at `ssh-add`, and only then. `stream` reports a
refusal the way a shell does: 127 for a missing command, 126 for any other `exec` failure, 125 for a
working directory. A consumer that leaves (`| head`) gets what ssh gave it. For stdout the client
closes its read end, so a writer takes `SIGPIPE`, while a quiet payload still runs to its end and
`stream` reports its own status. A departed stderr ends the request once held stdout is delivered,
since ssh does not end the channel for it, unless its trailer had already arrived; then stdout is
read to its end and the guest's status stands. With no status to report, `stream` returns 141.

## Files

`access.files(deadline=None)` returns that identity's `Files` (`files.py`). Each method is one `run`
of the guest file program (`file_program.py`, sent as a `PythonProgram` like agw-launch's source,
with no import-time effect): the operation and one JSON object of parameters are its arguments, and
content travels as `Bytes` on stdin, never as an argument. So do the digests `write`, `write_if` and
`remove_if` compare and verify, so the operation log, which records arguments, holds no content and
no digest of it. The deadline bounds every request.

| Method                                          | Does                                                                                                     |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `read(path, limit=1 MiB)`                       | The file's bytes, or `None` when the file or a parent is absent                                          |
| `stat(path)`                                    | `Absent()`, `Directory()` or `RegularFile(digest, mode)`, hashed on the guest                            |
| `write(path, data, mode=, owner=, group=, ...)` | Replaces the file whatever it holds, with that mode and ownership; creates missing parents               |
| `write_if(path, data, expected=, mode=, ...)`   | Replaces the file only while its digest is `expected` (`None`: still absent)                             |
| `update(path, data, observed=, mode=, ...)`     | `write_if` against what the caller read; data equal to `observed` sends nothing, returns false           |
| `remove_if(path, expected=)`                    | Removes the file only while its digest is `expected`                                                     |
| `ensure_directory(path, mode=, owner=, group=)` | Creates the directory and missing parents with `mode`; with an owner or group, converges an existing one |
| `prune_empty_parents(path, root=)`              | Removes empty parents up to and including `root`; returns a `Pruned`                                     |

Each takes `elevated`, which an agent's access refuses before dispatch. Paths must be absolute and
normalized (`guest_path`). Nothing is read or written through a symbolic link: every component is
opened relative to its parent without following links, and a link or the wrong type anywhere on the
path is `PathRefused(UNSUITABLE)`, a permission denial `PathRefused(DENIED)`. `write` and `write_if`
read their whole content and check its size and SHA-256 before creating anything, so a dropped
connection publishes nothing (`FileContentMismatch`); each writes a sibling temporary file with the
requested owner, group and mode, set before the rename, so the destination never holds the content
without them. That temporary file's name is fixed per destination, so the next write replaces one a
killed write left; a directory at it is `PathRefused(UNSUITABLE)` and is left alone. `write` keeps
nothing of the file it replaces, and a link or anything but a regular file there is refused. With
`write_if`, `preserve_metadata=True` gives the temporary file the existing file's owner, group, mode
and extended attributes instead, and a changed destination is `FileConflict`, with nothing written.
`update` is how a caller that reads, decides and writes publishes: an unchanged file keeps its
inode, mode and ACLs, and a file another writer changed since the read is `FileConflict`, never
retried. `ensure_directory` leaves an existing directory as it is, unless given an `owner` or
`group`: then it opens the directory, new or existing, without following a link and sets them and
`mode` on that descriptor, which is how an elevated caller places a directory inside another
account's home. `read` and `stat` keep their streams out of the operation log.

Every mutating operation holds one VM-wide lock, `/run/lock/agentworks-files.lock`, from its walk to
its last system call, whichever identity runs it: opened read-only, created world-readable only when
absent (an `O_CREAT` open of another identity's file in the sticky `/run/lock` is refused under
`fs.protected_regular`), and locked with `flock`. A lock not taken within 30 seconds is
`FileLockTimeout`. Reads take no lock, since a rename replaces a file whole. The lock orders
Agentworks operations among themselves; a writer that ignores it is out of scope. Modes and
placement policy (which directories root may write) belong to the caller.

## Client and policy

`client.py` resolves the OpenSSH client once per process to an absolute path and a version, and
requires 8.5 or newer. On POSIX it is the first `ssh` on `PATH` (skipping empty and relative
elements). On Windows `PATH` is never searched, because it commonly finds the MSYS build bundled
with Git for Windows; the native Win32-OpenSSH locations are tried in order. `agw doctor` shows the
verdict.

`ssh_process.py` builds the argv under `-F none`, so no operator or system configuration applies,
with the explicit identity (`IdentitiesOnly`), `BatchMode`, publickey-only authentication, the VM's
host-key trust options and keepalives on every connection. Its `spawn` is the only place a client is
started; it also starts a platform's root channel (below), and a program other than the OpenSSH
client that cannot start is a `ConnectivityError` naming it, not `ClientUnsupported`.
`build_host_argv` is the one other policy, for a platform's own connection to its host (Remote
Lima's placement host): it honors the operator's SSH configuration (no `-F none`), trusts the host
through `HOST_TRUST`, and keeps the connect timeout and keepalives, with `BatchMode` only when
piped. `pump.py` drives a started client through pipes: one thread over non-blocking pipes, with the
operator's stdin reaching it through a daemon thread that reads the raw stream. A deadline or an
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

## Root scripts

`root_script.py` is how core runs a script as root through a platform's own channel, which works
without the VM's Tailscale state. A platform implements one primitive,
`run_as_root(script, deadline) -> RawRun`: run `/bin/bash -s` as root with `script` on stdin and
return both streams, with no status, because no channel's status is trusted.
`root_script.run(run_as_root, payload, *, deadline, secret=None, log=None)` wraps the payload in one
brace group that Bash parses whole before running (so a payload that reads stdin gets end of file,
and a script cut short runs nothing), frames it with the execution markers, and returns a
`RootScriptResult` whose `status` is the payload's only when both closing markers arrived. The one
secret a script may need is an unexported shell variable, `AGW_SECRET`, in the script text: it is in
no argument and no child's environment, and it is redacted from every field of the result and the
log record.

`run_process(argv, script, deadline)` is `run_as_root` for a channel that is a local process
(`limactl`, `wsl.exe`, `ssh`): the pump drives it with `nonce=None`, passing both streams through,
until it exits and its pipes go quiet, the deadline passes or a stream passes 1 MiB.
`read_host_keys(run_as_root)` reads `/etc/ssh/ssh_host_*_key.pub` and keeps the lines
`trust.is_valid_key` accepts; `trust.fingerprint` prints a key as OpenSSH's `SHA256:` form.

A platform's interactive shell is not a root script: `VMPlatform.native_shell` runs it on the
operator's terminal through `ssh_process.run_terminal`, and a platform that offers none is refused
with `FeatureUnavailable` (`errors.py`) before the command's boundary resolves anything.

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

Nothing in this package imports `agentworks.transports`, `agentworks.ssh` or
`agentworks.remote_exec`. Three guards hold that. A fresh-interpreter test
(`tests/execution/test_independence.py`) imports every submodule with them blocked, and with them
the modules outside the package that are built on it alone: `vms.contract`, `vms.recovery`,
`public_ssh`, which loads the `vm_platform` package, and every module of `agentworks.plugins`, which
loads the plugin index. A conftest blocks them for every test in `tests/execution/`. A static check
rejects any import of them, in a function body or under `TYPE_CHECKING` too, in the files already
migrated onto this package (`tests/test_execution_import_boundary.py`); a slice that migrates a file
adds it to that list. The tests are described in
[`tests/execution/README.md`](../../tests/execution/README.md).
