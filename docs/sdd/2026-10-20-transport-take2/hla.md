# Transport Take 2: High-Level Architecture

- Status: Slices 1 and 2 implemented in PR A (#863) and slices 3 and 4 in PR B; slices 5 to 8 not
  started.
- Date: 2026-10-31
- Requirements: [frd.md](./frd.md)
- Plan: [plan.md](./plan.md)
- Source baseline: `eee56117c` (`main` after PR #851)
- Input: the saga lead's salvage list for this effort,
  [message-2026-10-23-transport-salvage.md](./message-2026-10-23-transport-salvage.md). Facts
  credited to it below were measured in its sources, the predecessors' live reports.

## Summary

One package, `agentworks.execution`, owns everything core does on a managed VM after provisioning.
It reaches the VM only through the installed OpenSSH client at the VM's tailnet address. Every
request runs the Agentworks launcher, `agw-launch`, a Python program the client sends on stdin
behind a constant first stage. The client then sends the whole request on the same stdin: argv or
script, environment, working directory and interpreter, followed by the caller's own input. Buffered
and streaming commands, terminal attachment, files and detached work are all built on that one
mechanism. Host keys live in an Agentworks-owned store bound to the VM.

Platforms stop exposing a general native transport. Each one keeps its native access private and
supplies a single recovery capability that runs a core-owned script as root. Provisioning reports
the tailnet address. Core's first connection to a new VM enrolls its host key, and the VM contract
is checked on the connections that follow.

```text
 capability stages              core managers (vms, sessions, workspaces, agents, backup)
        |                                         |
  RunContext.admin_access()                execution.vm_access(vm, config, identity)
  RunContext.agent_access()                       |
        +-------------------+---------------------+
                            |
                      VMAccess (passive)
        run / stream / attach / forward / detached / files
                            |
       stdin: agw-launch source, envelope, caller input
                            |
     ssh client process: resolved executable, VM policy, VM-bound trust
                            |
            installed OpenSSH  ==tailnet==>  sshd  ->  stage 0  ->  agw-launch (python3)

 platform plugins: private native access + run_as_root(script) + optional native shell
 shared SSH machinery for platforms: execution.ssh_process (host policy or VM policy)
```

## Design principles

- **One mechanism.** Files, detached work and terminals are built from the same guest request, not
  from scp, per-caller wrappers or channel-specific code. A need that cannot be built from it goes
  back to the lead as a design question.
- **No lowest common denominator.** No size limit, quoting rule or staging scheme exists to serve a
  native channel. The FRD names that as the cause of the predecessor's growth.
- **Build for callers that exist.** Each operation below names its current consumers and lands in
  the slice that migrates them. General jobs and cancellation stay deferred (R6).
- **Passive until run.** Constructing access objects resolves nothing and connects to nothing (R9).

## Components

| Module                                   | Responsibility                                                                                                                                                                                | Adopted from                                                      |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `execution/client.py`                    | Resolve the OpenSSH executable once to an absolute path, read its version and build, refuse unsupported builds before dispatch                                                                | `carriers/ssh/client.py` version probe                            |
| `execution/ssh_process.py`               | Build a client argv from a policy (VM or host), hold stage 0, and start the client. The only place that spawns `ssh`; its pump (stdin source, stdout and stderr, local deadline) is `pump.py` | `carriers/ssh/connection.py`, `_io.py`                            |
| `execution/trust.py`                     | The VM-bound host-key store: lookup, enrollment, import, re-establishment, removal                                                                                                            | new                                                               |
| `execution/envelope.py`                  | Encode a request into its envelope; frame and parse the start and completion markers; load the agw-launch source                                                                              | `preparation.py`, adapted                                         |
| `execution/agw_launch.py`                | `agw-launch`: the Python program stage 0 runs on the guest, including the terminal handover                                                                                                   | `bootstrap.py` ideas, rewritten in Python                         |
| `execution/requests.py`, `results.py`    | The caller vocabulary and typed outcomes                                                                                                                                                      | `preparation.py` types; `carrier.py` ideas                        |
| `execution/errors.py`                    | Typed failures, each subclassing the matching `agentworks.errors` kind                                                                                                                        | new                                                               |
| `execution/access.py`                    | `VMAccess`: identity-bound, passive entry point holding `run`, `stream`, `attach` and `forward`, plus `files` and `detached`                                                                  | new                                                               |
| `execution/files.py`                     | File operations built on `run` (D6)                                                                                                                                                           | semantics from `native_files.py` callers                          |
| `execution/detached.py`                  | Launch, observe and stop work that outlives its connection (D7)                                                                                                                               | `remote_exec.py` wrapper idea, copied                             |
| `execution/attach.py`, `forwarding.py`   | Terminal attach under `agentworks.terminal.guarded_terminal`; port forwards with an explicit lifetime                                                                                         | `transports/base.py`, `vms/manager/tailscale.py`                  |
| `execution/reachability.py`              | Wait until a VM answers over SSH, with the operator-facing wait wording                                                                                                                       | `transports.wait_for_reconnect`                                   |
| `execution/oplog.py` (slice 3)           | Format one request's log record: label, request shape, outcome and streams; never environment values, script source or stdin                                                                  | `SSHLogger` record shape                                          |
| `execution/root_script.py` (slice 4)     | Run a core script as root through a platform's `run_as_root`: the marker wrapper and its one embedded secret, `RootScriptResult`, the local-process channel and the host-key read             | new                                                               |
| `capabilities/vm_platform/public_ssh.py` | The cloud platforms' SSH to a VM's public address under the VM policy, for provisioning and recovery, composed from `ssh_process`                                                             | cloud `SSHTransport` uses                                         |
| `capabilities/vm_platform/lima_host.py`  | `limactl` locally or on the placement host: commands, `run_as_root`, the shell and Remote Lima's detached create                                                                              | `transports/lima.py`, `remote_lima.py`, `remote_exec.py`          |
| `vms/contract.py`, `vms/recovery.py`     | The VM contract checks; core's repair, rekey and logout scripts, each with one function over `root_script.run`                                                                                | new; scripts from `vms/initializer/credentials.py` and `power.py` |
| `vms/manager/recover.py` (slice 4)       | `agw vm recover`: trust re-establishment, the repair, the address and one wait                                                                                                                | new                                                               |
| `vms/initializer/phase_a.py` (slice 3)   | Phase A for a new VM: first contact in Enroll mode, release attestation and the base checks, inside the fail-closed span; no legacy import                                                    | `driver.py` Phase A                                               |

`agentworks.terminal` is not one of the legacy modules, so attach keeps using its guard. The proof
modules that do not fit are deleted when the package is adopted: `carrier.py`'s generic carrier
protocol, `carriers/proxmox.py` and `carriers/_proxmox_http.py` (R10).

## Decisions

### D1. OpenSSH client resolution (R8)

The resolver runs once per process and yields one absolute path. Every later spawn and version check
uses that path; the argv builder takes it as a required argument, so no caller can launch a bare
name. The salvage message measured both hazards this guards against: on Windows a bare `ssh`
resolves from the current directory first, and an empty `PATH` element resolves to the current
directory.

- **POSIX** resolves `ssh` along `PATH`, skipping empty and relative elements.
- **Windows** never searches `PATH`, because `PATH` commonly finds the MSYS build bundled with Git
  for Windows, whose argv handling corrupts every invocation (measured). It tries
  `%SystemRoot%\System32\OpenSSH\ssh.exe`, then `%ProgramFiles%\OpenSSH\ssh.exe` (the Win32-OpenSSH
  MSI install), preferring their `Sysnative` and `ProgramW6432` forms for a 32-bit Python. It
  accepts a build only when its `ssh -V` banner is the `OpenSSH_for_Windows_` form.
- **Every workstation** requires 8.5 or newer. A missing, unsupported or too-old client gets a typed
  `ClientUnsupported` refusal before credentials are resolved. The refusal names the paths tried and
  how to install a supported client.
- **Windows 10.** Its inbox client is 8.1 (measured, per the salvage message), so a Windows 10
  workstation needs the MSI install. The refusal says so.
- **Doctor.** `agw doctor` reports this resolver's verdict beside its existing `PATH` presence rows
  for `ssh`, `scp` and `tailscale`. The legacy paths still find `ssh` and `scp` along `PATH`
  (absolute elements only, `programs.resolve_program`), so the `ssh` and `scp` rows stay until the
  last legacy user goes (slice 7 or 8).

### D2. Policies and request delivery (R4, R8)

`ssh_process` builds every client argv from one of two policies.

- **VM policy**, for every connection the transport makes to a VM:
  - **Options.** `-F none`, so no operator or system configuration applies, plus explicit `-o`
    options for what that leaves to say: `BatchMode`, `PreferredAuthentications=publickey`,
    `SecurityKeyProvider=none`, an explicit `ConnectTimeout`, and the 8.5 `CertificateFile`
    workaround. Connection sharing (ADR 0015), agent and X11 forwarding, proxy commands and password
    authentication are off by the defaults that `-F none` leaves, with no configuration read; the CI
    `ssh -G` test and the live 8.5 and Windows cells guard those defaults.
  - **Identity.** `IdentityFile` is `operator.ssh_private_key` with `IdentitiesOnly=yes`. The
    default agent stays enabled, so an encrypted key can be served by it, but only that identity is
    offered. ssh never prompts for a passphrase (BatchMode), so when a connection fails before the
    start marker and that key is a passphrase-protected OpenSSH key (its cipher name, read with the
    existing identity parser), the error's hint says to load it into the agent if it is not loaded
    already. The wording is conditional because the same failure has other causes; nothing asks the
    agent, and nothing is checked when a connection succeeds.
  - **Trust.** The VM's store options (D9).
  - **Keepalives.** `ServerAliveInterval` and `ServerAliveCountMax` on every connection, so a dead
    peer ends even a request with no deadline.
- **Host policy**, for a platform's own SSH to its host, which is Remote Lima's placement host. It
  honors operator configuration, which R8 allows outside VM connections (ruled on 2026-10-23), and
  Remote Lima's host connection depends on that today. Its trust is the fixed host trust and it
  carries no identity of its own, so it is one argv builder, `build_host_argv`, not a policy type.
  The clouds' SSH to a VM's public address uses the VM policy (D11).

**Request delivery.** Everything request-bearing travels on the client's stdin, never on a command
line or in the environment.

- **What reaches the command line.** The client's arguments are the policy options, `-T` or `-t`
  (`-N -L` for a forward), the destination and the constant stage 0 command (D4). None of them
  carries request data.
- **Why stdin.** The pipe belongs to the client, so request content appears in no process list on
  either machine and never touches disk. It has no size limit of its own, and it depends on no
  client version and no sshd configuration beyond the admin's existing passwordless sudo, so it
  works from core's first connection to a new VM.
- **Terminals.** An attach is the one case where stdin is the operator's terminal. It uses two
  connections (D8), so its request still travels on a private stdin.

### D3. The request envelope (R3, R4)

The client writes three things on stdin, in order:

1. **The agw-launch source**, length-prefixed, which stage 0 reads and runs (D4).
2. **The envelope**, a length-prefixed JSON object holding a random nonce, the interpreter and
   startup flags, the literal argv or script source, the environment and the working directory. Its
   values are strings, which the client refuses if they contain NUL, so JSON carries every value R3
   names exactly, newlines, quotes, `%` and `${...}` included. It needs no version tag, because the
   client sends agw-launch and the envelope together from one build.
3. **The caller's input**, untouched, until end of file.

- **Exact reads.** Stage 0 reads the agw-launch source, and agw-launch reads the envelope, each with
  unbuffered reads of exactly its length. The payload inherits stdin positioned at the caller's
  first byte, with no copy loop in agw-launch. (Where sudo's I/O logging is enabled, sudo itself
  relays stdin for an elevated request.)
- **Size.** Nothing bounds a request but memory and the kernel's limit of 131,071 bytes on any one
  argv element or `NAME=VALUE` environment string, which the client refuses beyond; a total over
  `ARG_MAX` is refused by the guest's `exec`. A script's source goes to an anonymous file, not argv,
  so it is not bounded by that limit. The largest program in a request today is the native probe
  (`artifacts/native/probe.py`, 14,391 bytes).
- **What it replaces.** The current stack's coalesced `-o SetEnv` truncates at a newline and is
  first-value-wins (ADR 0014), and OpenSSH 10.x expands `%` and `${HOME}` in `SetEnv` values where
  9.x does not (#845). Nothing in the envelope passes through OpenSSH option parsing. The config and
  secret refusals of values containing a newline (`manifests/decode.py:254-261`, the environment use
  in `env/compose.py:73-81`) exist only for `SetEnv`, so they retire in slice 7 with its last user.

### D4. agw-launch and completion (R3, R4)

**Stage 0.** The remote command is one constant line, `/usr/bin/python3 -I -S -c '<stage 0>'`, with
`sudo -n` in front for an elevated request and a handover path after it for the terminal half of an
attach (D8). Stage 0 is one line of under 200 characters: it reads the length-prefixed agw-launch
source from stdin, or from the handover socket, and runs it. If it cannot read or start it, Python
exits non-zero before any start marker, so the request is not delivered. The client discards
everything before the marker, so a traceback quoting misaligned request bytes, as a startup file
that reads stdin produces, never reaches the error or a log. Stage 0's line contains no single
quote, double quote, backslash, `!` or newline, so the account shell passes it through unchanged and
Windows argument quoting has nothing to escape. `-I` ignores Python's environment variables and the
user site directory, and `-S` skips site packages, so no Python startup applies. The short line
stays far below Windows' 32,767-character command-line limit and keeps sudo's log entry to one line.
Elevation is the whole operation running under `sudo -n`, so no variable has to survive sudo.

**Account shell precondition.** sshd hands that command to the destination account's login shell as
`$SHELL -c`, so the account shell parses one constant line before stage 0 runs. Managed accounts
must use `sh`, `dash`, `bash` or `zsh`. The admin and agent templates enforce this at the config
boundary, and the base contract check warns about an admin or agent whose shell is outside the list
(D10). A shell outside that list, such as `tcsh`, was measured (salvage list) parsing the old Bash
launcher line wrongly while reporting an ordinary-looking status. The Python stage 0 did parse under
`tcsh` in the integration report on #856, but the list stays to the four shells the conformance
vectors cover. Before stage 0 starts, the account shell may read its own non-interactive startup
file: zsh's `.zshenv`, or the `.bashrc` Debian's bash reads under sshd. R3, as amended on
2026-11-01, accepts that and lists what agw-launch resets: output written before its start marker,
the environment, the working directory, the umask, signal handling and open descriptors. Its three
known gaps are in step 4 below. A startup file that reads stdin would consume the request; the
request then fails as not delivered, and a conformance vector covers it.

**What agw-launch needs.** The python3 of every recognized Debian release (`debian.py`), which
provisioning installs (D10), its standard library, and `sudo` for elevation. It is written in plain
syntax that needs no particular python3 release. A python3 without its standard library, as
`python3-minimal` alone gives, exits with a dedicated status before the start marker and is typed
`VMNotReady`. It is rewritten in Python from the proof's `bootstrap.py`; the locale handling of that
Bash program has no counterpart (step 4).

**Relationship to agw-atc.** The follow-up containment effort introduces `agw-atc`, an installed,
long-running VM program for sessions, limits and process-to-session lookup. The two stay distinct by
lifecycle: agw-launch arrives with every request and always matches the client that sent it, and
agw-atc is installed and versioned with the VM. The operator asked on 2026-11-01 whether the two
should be aligned; this design keeps two names and fixes its own side: agw-launch stays the only way
a request starts, and it outlives its own request only as the brief parked half of a handover. Work
agw-atc will own is a request whose payload runs `agw-atc`, so running a request never depends on
agw-atc being installed. The containment effort's own SDD owns everything else about agw-atc. The
saga's waves 5 and 7 ruling adds the other constraint on it: a future VM-side observability wrapper,
if one proves necessary, is the same installed program (`2026-08-04-next-steps/target-state.md`).

**What agw-launch does:**

1. **Reads the envelope and the passwd entry.** The client validates the envelope and one build
   ships both halves, so agw-launch does not re-validate its schema. A malformed envelope, a missing
   passwd entry (sshd cannot authenticate a uid without one) or a string that cannot be encoded to
   bytes is an exception before the start marker, so the request is not delivered.
2. **Checks its standard streams.** sshd gives the command pipes or sockets, stdout and stderr
   distinct (the terminal in TTY mode). If a startup file redirected one, for example with
   `exec >/dev/null` or `exec 2>&1`, agw-launch exits non-zero before the start marker, so the
   payload never runs and the request is reported as not delivered. A startup file that swaps both
   streams for other pipes that discard their input still defeats the check; that contrived case is
   one of R3's known gaps (step 4).
3. **Writes the start marker**, `<nonce> start`, to stdout and to stderr.
4. **Resets what the payload inherits:**
   - the environment, rebuilt from `HOME`, `USER`, `LOGNAME` and `SHELL` from the identity's passwd
     entry, a fixed `PATH`, `LANG=C.UTF-8` with no `LC_*`, `SHLVL=1` (so a bash child of the payload
     never reads the account's `~/.bashrc`, whichever of Debian bash's triggers is present), then
     the session variables today's shells and session workloads use, each taken from a source
     startup files cannot touch: `TERM` from the envelope (the operator's terminal type, with a
     default the LLD sets for a Windows workstation that has none) and `SSH_TTY` from the terminal's
     name in TTY mode, and `XDG_RUNTIME_DIR` as `/run/user/<uid>` when that directory exists and
     belongs to the identity; then the request's environment. Nothing is copied from the inherited
     environment, since only these sources are out of startup files' reach, so `SSH_CLIENT` and
     `SSH_CONNECTION` are no longer passed, and release notes say so;
   - the working directory (the home directory by default) and a fixed umask;
   - signal dispositions back to their defaults, the signal mask cleared, and no inherited
     descriptor above 2 other than a script's.

   R3 (operator, 2026-11-01) states three known gaps. Resource limits and scheduling priority a
   startup file lowered cannot be reset, since an unprivileged process cannot tell them from PAM's
   or raise them again. A background process a startup file leaves holding the session's streams can
   write into the payload's output after the start marker; keeping it out would mean framing every
   byte of payload output. And step 2 cannot see a startup file swap both streams for pipes that
   discard their input.

5. **Runs the payload as a child.** A literal argv runs with no shell. A script runs under the
   chosen interpreter, which reads the source from a file descriptor and never from its arguments
   (D2), which the launcher keeps in an anonymous in-memory file. Login or interactive startup
   applies only when requested. `sh` and `bash` are fixed paths. The default shell is the
   destination identity's passwd entry. A missing interpreter fails rather than being substituted.
6. **Reports completion.** Except in TTY mode, once the payload exits, or once it finds it cannot
   start it, agw-launch writes an end marker, `<nonce> end`, to stdout and then one framed trailer
   to stderr: `<nonce> exit <status>`, or `<nonce> refused <reason>` when the payload could not
   start (a command or interpreter that cannot be executed, `exec <errno>`, or a working directory
   it cannot enter, `cwd`). Every trailer follows an end marker. In TTY mode it execs the shell
   instead.

**Completion rules:**

- **Start marker.** A marker on either stream means the payload may have started. The client
  discards everything before the start marker on each stream, so shell-startup output never reaches
  the command's caller. On the terminal half of an attach the client is not in the stream, so such
  output can appear on the operator's terminal; it still never reaches the command.
- **The end marker and trailer are the only completion evidence.** The client reads stdout to its
  end marker and stderr to its trailer, strips both, and then ends the ssh process. The status is
  the trailer's, never the ssh client's, so a guest exit of 255 is reported as completed with
  status 255. Bytes after either marker come from background children of the payload and are
  dropped. The client never waits for end of file, because a background child that holds stderr
  would otherwise stall the request for its whole lifetime (measured in the integration report on
  #856), and never for a timer, because stdout and stderr travel separately and the trailer can
  arrive before the last of stdout. The LLD fixes the framing, so payload output need not end in a
  newline.
- **Not delivered.** A remote exit status other than 255 with no start marker means the payload
  never started: a clean exit delivers all prior output first, and step 2 refuses streams the marker
  could not reach. Status 127 without `sudo` there means the account shell found no python3, which
  is typed `VMNotReady`, as is the dedicated status of a python3 without its standard library. Under
  `sudo` a missing python3 exits 1 instead; the unelevated base check (D10) finds it first.
- **Unknown.** Everything else is unknown, whatever the client's own exit status: a start marker
  without both the end marker and the trailer, or 255 with no start marker. The client sends ssh's
  own diagnostics to a private log file (`-E`), apart from the guest's stderr, and does not read
  them to tell a refused connection from a dropped one. It shows the log's last 20 lines in the
  error, and reads one token from it to type the error: `Host key verification failed` on a 255
  becomes `TrustMismatch`, and `Permission denied (publickey` on a 255 becomes
  `Unknown(AUTH_FAILED)`. Its hint names the store file: after verifying the new key through the
  platform console or provider, remove that file and follow the `TrustMissing` guidance (import in
  PR A, `agw vm recover` from PR B), or recreate the VM. Recovery alone cannot fix a mismatch, since
  it re-establishes only a missing record; no flow in this design replaces a changed key.
- **TTY mode.** An attach never elevates to root: `elevated` is an option of `Command` and `Script`
  only, so an attach cannot be asked to, and no current attach consumer elevates. Elevation is the
  `sudo -n` prefix of the stage 0 command line, not part of the envelope, and the attach path never
  passes it, so the prefix and the handover argument are never combined. An attach returns its exit
  code; a 255 is reported as "connection ended or command exited 255", never guessed. TTY mode runs
  a login shell, adding `-i` only when stdin is a terminal, and resets every ignored signal before
  the exec. A refusal there prints one line and exits 127 for a missing command, 126 for any other
  `exec` failure and 125 for a working directory or handover refusal; `stream` reports a `refused`
  trailer with the same statuses.
- **Forgery.** The nonce is never in the payload's argv or environment, so ordinary payload output
  cannot forge a marker. A hostile process of the same user could read agw-launch's memory. That is
  outside R4's threat model, which concerns honesty, not a hostile same-user process.

### D5. Requests and results (R3, R4)

| Type                   | Shape                                                                                                                                                                        | Current consumers                                                                                                                                            |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Command`              | Literal argv tuple                                                                                                                                                           | most buffered calls                                                                                                                                          |
| `Script`               | Source plus `Shell.SH`, `Shell.BASH` or `Shell.USER_DEFAULT`, with `login` and `interactive` flags                                                                           | initializers                                                                                                                                                 |
| `LoginShell`           | The identity's default shell as an interactive login shell; valid only for `attach`                                                                                          | `vm shell`, `agent shell`                                                                                                                                    |
| `PythonProgram`        | Core-owned Python source and arguments, run by `/usr/bin/python3 -I -S` reading the source from a file descriptor, with the same `login` and `interactive` flags as `Script` | the file program, native probing, the native CLI's login resolution                                                                                          |
| Common options         | `env`, `cwd`, `elevated`, `deadline`, `log_streams` (not `LoginShell`)                                                                                                       | all                                                                                                                                                          |
| Input                  | `NoInput`, `Bytes(data)`, `FromFile(path)`                                                                                                                                   | `Bytes`: the file program, native probing and the native CLI's login resolution (slice 5), and `upload_tree` (slice 6); `FromFile`: workspace copy (slice 7) |
| Output sink (buffered) | `CaptureOutput(limit)`, `DiscardOutput`, `ToFile(path, on_progress=)`                                                                                                        | `DiscardOutput`: `command_checks.py`, native CLI; `ToFile`: backup and workspace copy (slice 7)                                                              |
| `Result`               | `completion` is `Completed(status)`, `NotDelivered(reason)` or `Unknown(reason)`; `stdout` and `stderr` are bytes                                                            | all                                                                                                                                                          |

Each vocabulary item lands in the slice that migrates its first consumer. `PythonProgram` is not one
of R3's script interpreters. It carries core's own Python programs, which travel in `python3 -c`
argv on `main` (`artifacts/native/probe.py:402`, `native_files.py:338`), so their source stays out
of guest arguments (D2). With `login` and `interactive`, the identity's default shell runs that
startup and then execs the interpreter, with the program's arguments passed positionally so no
quoting is needed. That is how the native probe keeps seeing the native workload's login and
interactive environment, as it does today through `"$SHELL" -lic` (`probe.py:402-410`), and the
native CLI's login resolution its login environment (`native_cli.py:44`). The interactive half
matters: the seeded `.bashrc` returns early for a non-interactive shell before it sources the mise
activation (`capabilities/vm_platform/skel.py:46-50`, `:124-125`).

- **Bytes, not text.** Results carry bytes with no newline conversion; a `text()` helper decodes.
  Stdin is written in binary mode on every workstation (incident row I2).
- **Sensitive inputs.** Sensitive input, environment values and script content never appear in logs,
  exceptions or retained artifacts; sensitive is what the caller marks secret (D13). A
  secret-bearing environment does not suppress output (R4). Retained output has the request's secret
  values redacted (D13), and a request with `log_streams=False` keeps both streams out of the log.
  Stdin is never formatted into a record or an error.
- **Deadlines.** A deadline bounds the local wait. If it passes before both closing markers (D4)
  have arrived, it kills the local client and returns `Unknown`, since the output may be incomplete.
  Once both have arrived the client ends ssh at once and nothing after completion is awaited, so a
  background child can never turn a completed request into `Unknown`. It never claims remote
  termination; the predecessors measured guest trees outliving the deadline, and cancellation stays
  deferred.
- **Retries.** `VMAccess` never retries by itself. A request may be dispatched again only if its
  caller declares it a read-only observation (`observation=True`, an option that arrives with its
  first caller, `detached.observe` in slice 7), and only after `NotDelivered` or `Unknown`. Until
  then the one retry loop is `reachability.wait`, which polls a fixed read-only probe. Mutating
  requests are never re-dispatched.
- **`check=True`** turns anything but `Completed(0)` into a typed error carrying the `Result`.

### D6. Files built on execution (R5)

Every file operation is a `run` of one core-owned Python file program, sent as a `PythonProgram`
(D5) with the operation as its first argument, and content on stdin or stdout. It starts from the
file program `native_files.py` already dispatches the same way on `main` (`_FILE_PROGRAM`). Keeping
it a separate payload, rather than part of agw-launch, keeps file code off the requests that do not
use it. There is no scp.

| Operation                                                        | Mechanism                                                                                                                                                                                                                                                                                                                                          | Current consumers (slice)                                                                                 |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `read(path) -> (bytes, digest)`                                  | Open without following symlinks and copy to stdout; the workstation computes the digest from the bytes it received                                                                                                                                                                                                                                 | artifacts, settings (5)                                                                                   |
| `stat(path) -> Absent \| Directory \| RegularFile(digest, mode)` | `lstat` relative to the parent; only a regular file is opened, without following links, and hashed on the guest                                                                                                                                                                                                                                    | artifact publication, native settings (5)                                                                 |
| `write(path, data, mode, owner, group)`                          | Content read whole from stdin and checked against its size and digest, then, under the lock, a temporary file in the destination directory with owner and mode set, `fsync`ed and renamed into place. None of its consumers keeps an existing file's metadata                                                                                      | initializer fragments, profiles, tmuxinator, sources (6)                                                  |
| `write_if(path, data, expected, ...)`                            | As `write`, comparing the current digest (or absence) with `expected` under the lock before the rename. With `preserve_metadata=True` the existing file's owner, group, mode and extended attributes (ACLs) are copied onto the temporary file under the lock, before the rename, as `native_files.py` does today (`:116`, `:125-126`, `:155-172`) | artifact publication (with `preserve_metadata=True`, `artifacts/publication.py:205`), settings merges (5) |
| `update(path, transform, ...)`                                   | `read`, apply `transform` on the workstation, `write_if`; retry on conflict                                                                                                                                                                                                                                                                        | harness JSON and TOML settings, marked profile blocks (5, 6)                                              |
| `remove(path)`, `remove_if(path, expected)`                      | Under the lock                                                                                                                                                                                                                                                                                                                                     | artifact cleanup (5), workspaces (7)                                                                      |
| `ensure_directory`, `prune_empty_parents`                        | Operations of the same program                                                                                                                                                                                                                                                                                                                     | artifacts (5), workspaces (7)                                                                             |
| `upload_tree(local_dir, dest)`                                   | A `tarfile` stream from the workstation, extracted beside `dest` and swapped in for it, as its one caller needs (`fetch_dir` replaces `dest` today, `sources.py:282-287`)                                                                                                                                                                          | sources' `fetch_dir` (6)                                                                                  |

- **One lock for every writer.** Every mutating file operation, tree uploads and removals included,
  takes `flock` on one VM-wide lock file, `/run/lock/agentworks-files.lock`. `/run/lock` is a
  sticky, world-writable tmpfs on Debian, so whichever identity first needs the file creates it and
  a boot clears it; no provisioning, init step or contract check is involved. Because Debian sets
  `fs.protected_regular` to 2, an `O_CREAT` open of another identity's file there fails with
  `EACCES`, for root as well (measured on Debian 12 and 13 in the integration delta on #856), so
  every identity, elevated or not, follows one order: the file program opens it read-only, without
  following symlinks or blocking, creates it with `O_CREAT|O_EXCL` only when it is missing, sets
  mode 0644 explicitly whatever the umask, and on losing that race opens it read-only again. One
  fixed file means a tree upload and a conditional write beneath it can never hold different locks,
  and replacing a directory never replaces a lock, so `write_if` is atomic with respect to every
  Agentworks writer, which is R5's cooperating-writer guarantee. A tree upload extracts into a
  temporary directory beside its destination without the lock and takes it only to move the result
  into place (the old tree aside and the new one in), and a `write` reads its content whole before
  it takes the lock and then holds it from its walk to its rename, so every hold is short, and a
  lock wait has a deadline. Workspace copy's opaque tar stream is a `run`, not a file operation, and
  takes no lock. An agent that creates the file unreadable, or holds the lock to stall other
  writers, is a hostile writer, which stays with the permissions effort along with hostile path
  races.
- **Symlinks.** A destination that is a symlink is refused, as `native_files.py` refuses today,
  rather than followed as the hand-written `tee` paths do.
- **The merge stays on the workstation.** `update` runs
  `capabilities/harness_integration/settings.py` locally. That module already keeps JSON null as a
  value and rejects malformed input, so R5's merge rules hold by construction, and TOML merges get
  the same conditional write.
- **Opaque archives.** Workspace copy moves an opaque tar stream with `run`, `ToFile` and
  `FromFile`, and never extracts on the workstation, so modes, owners and symlinks survive a Windows
  workstation.
- **Elevation and identity.** Elevated placement is the request's `elevated` flag, so callers write
  no copy, chmod or sudo wrappers. Agent writes use the agent's `VMAccess`.
- **Failures.** A failure removes only the temporary file the operation created.

### D7. Detached work (R6)

The only current core caller is VM backup. Remote Lima's host-side detached create is
platform-private (D11).

- **Launch.** `detached.launch(request) -> LaunchId` takes an elevated request, since backup
  launches as root (`vms/backup.py:367`), and creates a fresh directory under
  `/var/lib/agentworks/detached`, root-owned 0700. A base for unelevated launches waits for a caller
  that needs one. It then starts a wrapper with `setsid nohup` that records the pid, output and exit
  status there, and returns at once. No world-writable path is used. The id names the directory, so
  an independent new launch never matches an earlier one.

- **Observe.** `detached.observe(id, offset)` is an observation request returning new output, the
  state and the status. Backup uses it across dropped connections within one invocation. A later
  invocation can do the same with the recorded id (R6).
- **Stop.** `detached.stop(id)` signals the process group. Backup calls it from its interrupt
  handler, as today.
- **Discard.** `detached.discard(id)` removes the directory once the caller is done.
- **Holds.** The caller records the id and owns the VM availability hold (the existing activation
  gate, including the WSL2 keepalive). The transport never starts or holds a VM.

### D8. Streaming, attach, forwarding and reachability (R2, R4)

- **`stream(request)`** writes the agw-launch source and the envelope, then copies the operator's
  stdin into the pipe in binary mode until end of file. After the start marker each stream passes
  through a filter that holds back only bytes that could begin its closing marker (the end marker on
  stdout, the trailer on stderr) and stops there, so live output is never delayed by a timer and
  nothing after the markers reaches the operator. It returns the guest's status, and a refusal
  becomes status 127, 126 or 125 with one line on stderr (D4). Consumers: `vm exec`, `agent exec`.
- **`attach(request, clear_screen_on_exit=)`** uses two connections, so ssh keeps the operator's
  real terminal (its window size and modes come from its own stdin) while the request still travels
  on a private stdin. A first `-T` request carries the envelope marked for handover, with a token
  the client chose. agw-launch first checks that the working directory is enterable and the shell
  executable, so a refusal comes back typed before the terminal is touched. It then creates the
  directory `/tmp/agw-handover-<token>` (mode 0700; an existing name is a refusal, so a planted path
  is never reused), listens on a Unix socket in it, writes a bare `parked` marker and waits. The
  client then runs `ssh -t` under `guarded_terminal` with the handover form of stage 0 naming the
  socket path, which fetches the agw-launch source and the envelope and execs the payload on the
  terminal as in D4. The parked agw-launch serves one connection, removes its directory and exits
  with a trailer. After 30 seconds without a connection, or when the client closes its end, it exits
  with an `end` marker and a `refused handover` trailer, and `SIGHUP` and `SIGTERM` take the same
  cleanup path, so nothing is left behind. Only the account's own processes can reach the socket,
  and they can already read that account's processes. Every attach pays one extra SSH handshake,
  which ADR 0015 measured at 150 to 300 ms. The post-guard notice for a dropped connection and the
  Windows clear behavior are kept. Consumers: session and console attach, `vm shell`, `agent shell`.

- **Console panes.** A pane added to an existing console's tmux server inherits only that server's
  environment plus `tmux -e` arguments, and today those arguments carry resolved secrets on a guest
  command line every VM account can read (`sessions/multi_console/tmux_build.py:222-245`), which R4
  forbids. The handover replaces them. The pane's environment and shell are parked by a request over
  the pane identity's own connection (the agent's, for an agent pane). The pane's command is the
  handover form of stage 0, under `sudo -n -u <agent>` for an agent pane in the admin's server, so
  it connects as the same account that parked. That leaves the `--preserve-env` path and the
  `Defaults:<admin> setenv` sudoers grant (`vms/initializer/shell_env.py:543`) with no job, and
  slice 7 deletes them. Sessions need no handover, since each session starts its own tmux server,
  which inherits the request environment, so their `tmux -e` arguments (`sessions/tmux.py:737`) go
  too.

- **`forward(forwards, verbose=)`** blocks while it runs `ssh -N -L` under the VM policy with
  `ExitOnForwardFailure=yes`, and returns the client's exit status. A setup failure is the same
  non-zero exit, which `ExitOnForwardFailure` and `ConnectTimeout` make visible, so there is no
  readiness probe. On a non-zero exit it shows the last lines of the client's `-E` log, and
  `Host key verification failed` there raises `TrustMismatch`; under `verbose` the client's stderr
  is the operator's and neither applies. The client always ends on the way out. `vm port-forward`
  owns its signal handling and returns 0 on an operator stop. Consumer: `vm port-forward`.
- **`reachability.wait(access, *, wording)`** polls a fixed read-only probe (`true`) until it
  completes, with today's wait wording and flap double-check, and returns whether the VM answered.
  It polls only connection-level outcomes (`CONNECTION`, `TRUNCATED`, `DEADLINE`) and raises every
  other outcome at once, including a host-key mismatch and an authentication failure, so a failure
  that waiting cannot fix is neither waited out nor repeated against the VM's sshd. It owns neither
  the exhaustion warning nor the decision to proceed; its callers do. Consumers: create's
  first-contact and post-Tailscale steps, `vm start`, the activation gate, `vm recover` and rekey.

### D9. The VM-bound host-key store (R8)

**Trust modes.** There are three, and the code that builds a connection chooses one explicitly:

| Mode       | Used by                                                                                         | Options                                                                                                                                                                                                                         |
| ---------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| VM         | every connection to a VM, at its tailnet or public address                                      | the VM's store file and alias, `StrictHostKeyChecking=yes`                                                                                                                                                                      |
| Enroll     | create's first-contact connections (the polled wait for the VM to answer, up to 17)             | the VM's freshly reset store file and alias, `StrictHostKeyChecking=accept-new`                                                                                                                                                 |
| Unverified | cloud provisioning before the VM is in the store; a cloud host-key read when the record is lost | a temporary known-hosts file for that one run, `StrictHostKeyChecking=accept-new`, deleted when the run ends. First-use trust that pins for the rest of the run, as provisioning has today, without writing the operator's file |

Host connections such as Remote Lima's keep their own configuration and are not VM trust at all.

- **Layout.** One file per VM, at `~/.config/agentworks/known_hosts/vm-<vm>` (the prefix keeps
  Windows reserved names such as `con` usable), in `known_hosts` format, with records under a
  host-key alias derived from the VM name. Every connection pins the global known-hosts file away,
  plus `UpdateHostKeys=no` and `HashKnownHosts=no`, as `-o` options that configuration cannot
  override. They pin `GlobalKnownHostsFile` to the null device rather than `none`, since a `none`
  value before 8.5 names a file in the working directory, and the null device is accepted by every
  version, so the legacy paths and the VM policy share one function
  ([trust-lld.md](./trust-lld.md)).
- **Identity.** The VM name keys `vms` (`db/migrations.py:240`) and survives every tailnet address
  change. Delete removes the store file, and create resets it, so a reused name never inherits a
  key.
- **Lookup.** A missing record is a typed `TrustMissing` naming the import and recovery commands. It
  never falls back to first use, so a missing record never makes an existing VM new.
- **Enrollment.** Create resets the VM's store file and makes its first-contact connections in
  Enroll mode, reached only through the distinct factory `execution.enrollment_access`. They are the
  polled `true` probe, up to 17 connections (sixteen attempts and the confirming probe);
  `accept-new` records a key only when none is known, so once one reaches sshd the rest verify
  against it. The base contract check (D10) runs on the VM-mode connections that follow. The store
  is the only place the key is written. If create is interrupted after the reset, the VM is retained
  unattested and gated, and the remedy is delete and recreate: `agw vm recover` refuses a VM whose
  provisioning did not complete. A VM with no importable entry can be brought in with an
  operator-verified file passed to `--known-hosts` (`ssh-keyscan` output checked against the
  console's or provider's fingerprints). The `TrustMissing` hint names both.
- **Import.** `agw vm import-host-keys (--all | VM...) [--known-hosts PATH]`:
  - Reads the default `~/.ssh/known_hosts`, or the named file, without modifying it.
  - Matches each VM's recorded tailnet address with OpenSSH's own `ssh-keygen -F`, so the result is
    exactly what `ssh` would have trusted (wildcards, negation, bracketed ports, comma lists and
    `|1|` hashed entries), and copies matching key records into that VM's store file.
  - Reports an `@cert-authority` or `@revoked` marker whose pattern matches a VM, and imports
    nothing for that VM.
  - Never writes to an existing record, so it can neither replace a key nor widen the set.
- **Re-establishment.** This is part of `agw vm recover` (D11). It calls the platform's
  `read_host_keys`, shows the fingerprints, and writes them to the store only after the operator
  confirms.
  - **Default implementation.** `read_host_keys` returns `tuple[HostKey, ...]`. Its base-class
    default runs core's fixed script through the platform's `run_as_root` (D11), printing the public
    host keys, and keeps each line that parses as a key, since guest output is a boundary. Its
    channel does not use the VM's store: WSL2's `wsl.exe`, Proxmox's guest agent, and Lima's
    `limactl shell` over Lima's own host-local access.
  - **Clouds.** They override it and read the keys inside their transient route under a provisioning
    binding, in Unverified mode. No provider-side fingerprint source is used: the operator compares
    the fingerprints with the VM's console (a cloud's serial console or system log), and R7 requires
    only the operator's confirmation.
  - **Limits.** Unverified mode is used for nothing else after provisioning. Every other
    `run_as_root` call is VM trust only.
- **Managed SSH entries.** Once a VM has a store record, `ssh_config.py` adds `HostKeyAlias`,
  `UserKnownHostsFile` (the store file) and `StrictHostKeyChecking yes` to its managed VM and agent
  entries, so the operator's own `ssh` verifies against the store. Entries for a VM without a record
  stay as they are today, so an operator's own `ssh` keeps working through the upgrade until they
  import. Slice 8 makes the store options unconditional, so R8's end state does not depend on every
  operator importing.
- **Legacy paths during migration.** Slice 1 gives the legacy VM-target factories an explicit trust
  binding in VM or Enroll mode. These are `transport()`, `agent_transport()`,
  `transport_for_user()`, Phase A's builder, the backup scp, the workspace-copy ssh, the port
  forward and the clouds' public-address native transports (`aws/platform.py:718`,
  `azure/platform.py:824`, `gcp/platform.py:591`), which reach the same sshd. Host targets are
  unchanged, and cloud provisioning moves to Unverified mode. This edits legacy code in the
  direction R10 allows: legacy code calls new code, never the reverse.

### D10. The VM contract and its checks (R1)

`vms/contract.py` holds one table of read-only checks. A check is an id, a severity (`FAIL` or
`WARN`) and a probe, and the table renders one structured report naming each failed item. Every
failure names recreation; what initialization can repair is not a contract row, because a failed
install already fails Phase B.

- **Base checks** run on the VM-mode connections that follow create's enrolling first contact, and
  first at reinit:
  - a recognized Debian release, under ADR 0025's policy and recording; it is the first contract
    request, right after the first connection, and a failure stops the rest;
  - systemd as PID 1;
  - the admin account and its sudo policy, as an elevated request;
  - python3 and agw-launch running, which the first request proves, plus coreutils, util-linux and
    `tar`;
  - Tailscale joined and OpenSSH reachable on the tailnet, which the connection itself proves: it
    reached the VM at its recorded address and verified the VM-bound host key. Nothing checks that
    the address is a tailnet one, which a controlled environment accepts.
- **Warnings** never fail the contract:
  - supported login shells (`sh`, `dash`, `bash` or `zsh`) for the admin and every agent, naming the
    command that fixes each account;
  - pam_env settings: agw-launch does not apply pam_env (the operator's 2026-11-01 ruling), so if
    `/etc/environment`, `/etc/security/pam_env.conf` or the admin's `~/.pam_environment` holds
    anything but comments and blank lines, the check warns and names the file. The home file counts
    only where `/etc/pam.d/sshd` passes `user_readenv=1` to `pam_env.so`, as Debian 12 does; Debian
    13's sshd never reads it. `/etc/default/locale`, which Debian's sshd PAM stack also reads, is
    left out on purpose, since it is set on every VM and agw-launch sets the locale itself. Agent
    homes are not read.
- **Diagnosis.** A failed check leaves the VM not ready and names recreation. A warning names the
  file or account and the command that fixes it.
- **Provisioning additions.** python3 joins the shared provisioning package list
  (`PROVISIONING_PACKAGES`, `capabilities/vm_platform/cloud_init.py:16`), which every platform's
  bootstrap installs, so a new VM runs agw-launch from core's first connection. Init keeps
  installing it (`INIT_SYSTEM_PACKAGES`, `:27`), as the operator asked, but from slice 3 reinit
  starts with the base check over agw-launch, so init cannot reach a VM that lacks it.
- **Older VMs without python3.** python3 joined init only on 2026-09-14 (`2841cb9fa`), so a VM
  initialized before then lacks it unless its image shipped it; the WSL2 Debian root filesystem is
  the likely case. Such a VM gets `VMNotReady`. In PR A its hint names `agw vm reinit`, whose
  release probe and initialization stay on the legacy stack until slice 3 (reinit has no Phase A),
  so reinit still runs there without python3; from PR B it names `agw vm recover`, whose access
  repair installs python3 (D11). Release notes say so.

### D11. The platform surface (R1, R2, R7)

Changes to `capabilities/vm_platform/base.py`. The vm-platform `contract_version` becomes 2 with the
first of them, in slice 3, and slice 4 lands under the same number in the same PR:

| Today                                            | After                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ProvisionResult.native_transport` (required)    | Removed in slice 3, when Phase A and Debian attestation move onto SSH                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `ProvisionResult.tailscale_ip: str \| None`      | `tailnet_address: str`, required. After a successful join each platform settles the address through one shared helper, `settle_tailnet_address`, over its own `run_as_root`, retrying within its own provisioning step (six reads, two seconds apart), so a transient failure costs neither the VM nor a second use of the key; reading the address sends no key. Each read is parsed as an IP address, and core parses it again before recording it, because the platform result is the plugin boundary. If the reads are exhausted, the helper logs the node out over that same channel, still under the provisioning trust, and create fails without a second join, like any other create failure: the platform rolls back its backend state or raises `RetainedProvisioningError` when it cannot confirm the rollback (`capabilities/vm_platform/base.py:265-273`). This matters for the clouds: core never enrolled the host key, so delete's logout, which runs under VM trust, could not log the node out later. When the logout does not report success, create's error names the node for removal in the Tailscale admin console. No bootstrap script carries the key: every platform joins through the shared `join_tailscale` after its bootstrap, and the bootstrap's parser no longer requires an address. The join-once rule (`tailscale_join.py:65-71`) carries over |
| `native_transport(vm, ctx, config)`              | Removed in slice 4, when rekey, logout, recovery and the native shell move                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| (none)                                           | `run_as_root(vm, ctx, script, deadline) -> RawRun`: runs `/bin/bash -s` as root through the platform's channel with core's script on stdin, once, with no Tailscale dependency and VM trust only, and returns both streams when the channel ends or the deadline passes, with output bounded. `RawRun` holds the two streams and no status. A descriptor requirement                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| (none)                                           | `read_host_keys(vm, ctx) -> tuple[HostKey, ...]`, the key-reading half of the same R7 capability, not a second one. A base-class default runs core's fixed script through `run_as_root`; a cloud overrides it with its private access under Unverified trust                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| (none)                                           | `ClassVar recovery_needs_guest_sshd: bool`, a descriptor requirement                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `native_shell_unavailable_hint`                  | `native_shell(vm, ctx) -> int`. A platform offers a shell by overriding it. Core detects the override on the class before the operation boundary, so it refuses with `FeatureUnavailable` before credentials are resolved (R2). `native_shell_unavailable_hint` stays as the refusal's guidance text                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `transient_route`, `probe_failure_hint`          | Private to each platform's own native code (recovery and native shell)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `ProvisionRequest.ssh_private_key: Path \| None` | `Path`: create always passes the configured operator key                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |

**The root script wrapper.** Core's `root_script.run` in `execution/root_script.py` wraps the
payload in one Bash brace group that closes stdin, holds the one secret a script may need as an
unexported shell variable, and writes D4's start and closing markers. It passes the bytes to the
platform's `run_as_root` once and reads the streams with D4's reader. Its `RootScriptResult` is the
payload's status when both closing markers arrived and unknown otherwise, since no channel's own
status is trusted; the start marker only separates the channel's own output from the script's. A
script is never dispatched again automatically (R4), so telling not delivered from unknown would buy
the operator nothing. A platform author runs `/bin/bash -s` as root and returns what came back; only
core knows the markers.

- **Shared with provisioning.** The same wrapper carries provisioning's join, address reads and
  logout. The script is the request's finite sensitive stdin (R7): the auth key travels inside it
  and reaches Tailscale through `--auth-key=file:/dev/stdin`, so it is in no argument on either
  machine.
- **Failure.** A script prints its reason on stderr and exits 1. The reason reaches the operation
  log, never an error message (D13).
- **A dropped channel.** Only the logout and rekey scripts ignore `SIGHUP` and `SIGPIPE`, and they
  capture each step's output and print it once at the end, so on an SSH channel a drop mid-command
  does not stop them.
- **No reliance on channels.** Core's scripts leave no background child, so no channel waits on a
  descendant that holds its output open. Nothing relies on `limactl` or `wsl.exe` exit codes; the
  salvage message measured WSL passing raw wait statuses through, which makes a signal and an exit
  indistinguishable. The capability takes no environment, directory or files.

The channels' details are in [platform-lld.md](./platform-lld.md).

**Per platform:**

| Platform        | Root channel                                                                                                  | Needs guest sshd | Native shell | Private access after migration                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------- | ---------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Lima, local     | `limactl shell <instance> sudo -n /bin/bash -s`                                                               | yes              | yes          | `lima_host.py`, from `transports/lima.py`                                                                                 |
| Remote Lima     | the host argv, then the same `limactl shell`                                                                  | yes              | yes          | `lima_host.py`: the host connection under the host policy, and the host-side detached create copied from `remote_exec.py` |
| WSL2            | `wsl --distribution <distro> --user root --exec setsid -w /bin/bash -s`                                       | no               | yes          | `wsl.exe` wrappers and the keepalive                                                                                      |
| Proxmox         | QEMU guest agent exec of `/bin/bash -s` with `input-data`                                                     | no               | no           | QGA client as `plugins/proxmox/guest_agent.py`, replacing `plugins/proxmox/transport.py`                                  |
| AWS, Azure, GCP | `public_ssh.py` to the public address under the VM policy, `sudo -n /bin/bash -s`, inside the transient route | yes              | yes          | provisioning and lost-record key reads under Unverified trust; recovery under VM trust                                    |

- **Cloud public-address SSH.** The public address reaches the same sshd, with the same host key, as
  the tailnet, so cloud recovery verifies against the VM's store entry. It uses the VM policy with
  the constant remote command `sudo -n /bin/bash -s`, so `-F none` keeps forwarding and connection
  sharing off. R8 lets this connection honor operator configuration but does not require it; the
  price is that an operator's `ssh_config` stanza for a cloud address no longer applies to recovery
  and provisioning. agw-launch is not involved, so a VM without python3 can still be recovered.
- **Core's recovery scripts** live in `vms/recovery.py`:
  - **Repair** rewrites the admin's managed `authorized_keys` as reinit does, so a key the
    configured one replaced stops working, installs python3 when the interpreter agw-launch runs
    does not start, repairs sshd (unmask, enable, configuration test, clearing a crash loop's failed
    state, restart) on every platform, and joins Tailscale only when the node reports `NeedsLogin`.
    Only `agw vm recover` runs it. Starting a VM, by `vm start` (`vms/manager/power.py:83-112`) or
    by the activation gate (`LiveVMNode.auto_start`, `vms/nodes.py:232-273`), starts it and waits
    for it to answer over SSH; when it does not, the start fails with a hint naming
    `agw vm recover`, as the operator ruled on 2026-10-31 and 2026-11-01. Both starts' rejoin code
    goes, and with it the secret resolution that exists only for it: `vm start`'s conditional
    auth-key read, and the gate's lazy repair-secret protocol, `GateTarget.repair_secret_refs` and
    `_GateSecrets` (`orchestration/activation.py:96-202`), `vms/nodes.py:186-205` and
    `sessions/manager/_scope.py:366-433`. A VM whose ephemeral-key node was removed while it was
    offline therefore fails `vm start` with that hint, and `agw vm recover` rejoins it in one step;
    the operator ruled to document this rather than detect it.
  - **Rekey** is `vm rekey`: one script that restarts `tailscaled`, logs out, joins and restarts
    again, then the address settle and a wait over SSH.
  - **Logout** runs at delete (the operator's 2026-11-08 ruling), after the checks that can refuse
    and the site binding, and before the provider delete, best effort inside a hold that never
    gates. Its result is the script's own status, or unknown if the connection drops first. A cloud
    VM with no store record is not logged out, since delete never logs out under Unverified trust,
    nor is a guest the platform cannot confirm is the VM's; a node not logged out is named for the
    admin console once the row goes. A delete that fails after a logout keeps the row, and its hint
    leads with finishing the delete, naming `vm rekey` only as the way back onto Tailscale.
- **The operator entry point** is the new `agw vm recover <vm>`. It refuses a VM whose provisioning
  failed or whose power state the platform cannot report, starts a stopped VM itself, and resolves
  the auth key in its boundary. It first offers host-key re-establishment (D9) when the VM has no
  store record, since every other step needs trust. It then runs the repair, reads the address
  (settling, with its logout on exhaustion, only when the repair joined), syncs the SSH config,
  whose aliases name the configured key, and waits once over SSH. It then names any agent accounts,
  which keep their previous key until `agw agent reinit`. When the channel needs the guest's sshd
  and sshd is broken, or recovery fails, it stops and tells the operator to repair the machine (the
  platform's native shell when the repair ran, its console otherwise) or recreate it (R7).
- **Debian attestation at create** moves from the native transport to the base checks (D10). The
  Phase A `tailscale ip -4` fallback goes away with the required `tailnet_address` (R10).

### D12. Delivery to consumers (R9)

- **The access object.** `VMAccess` is built from a VM row, the config and an identity, `Admin` or
  `Agent(agent)`. Construction copies names and paths and resolves nothing. The client, trust file,
  secrets and route are resolved when an operation runs.
- **Capability accessors.** `RunContext` gains `admin_access()` and `agent_access()`, returning
  `VMAccess | None`, beside `admin_target()` and `agent_target()` until the last capability consumer
  moves.
- **Core managers** call `execution.vm_access(vm, config, identity)` directly, without a
  `RunContext`. Either may pass a `log` callable, which `run` calls with each request and its result
  (D13).
- **Agent identity.** An agent-bound `VMAccess` connects as the agent's Linux user (ADR 0011). It
  refuses an elevated request before dispatch with a typed `PermissionDenied`, derived from the
  identity the access was built for. The guest's `sudo -n` remains a second layer, so an agent
  placed in a sudo group by hand still never gains admin authority through the transport.
- **Tests.** Manager tests use a recording fake `VMAccess` returning canned typed results, so they
  run on every CI host. agw-launch runs only in the conformance suite, against a local sshd and
  python3 on Linux and against live targets.

### D13. Errors and the operation log

`execution/errors.py` subclasses the matching `agentworks.errors` kinds:

| Error                                        | Kind                 |
| -------------------------------------------- | -------------------- |
| `ExecutionIncomplete` (carries the `Result`) | `ConnectivityError`  |
| `CommandFailed` (carries the `Result`)       | `ExternalError`      |
| `PermissionDenied`                           | `AuthorizationError` |
| `FeatureUnavailable`, `VMNotReady`           | `StateError`         |
| `RequestRefused`                             | `StateError`         |
| `TrustMissing`, `TrustMismatch`              | `StateError`         |
| `FileConflict`                               | `StateError`         |
| `PathRefused`                                | `StateError`         |
| `FileLockTimeout`                            | `StateError`         |
| `ClientUnsupported`                          | `ConfigError`        |

`ExecutionIncomplete` covers both `NotDelivered` and `Unknown` completions and names which one, so
the facts are held once, and shows the last lines of the client's `-E` log. `RequestRefused` is a
`refused` trailer under `run` and under a refused handover. `VMNotReady` also covers a python3
without its standard library. `TrustMismatch` names the manual path (D4), since import never
rewrites an existing record and recovery re-establishes only a missing one. `FeatureUnavailable`
lands in slice 4 with `vm shell --platform`, its first consumer. No error message, `repr` or hint
holds a stream, an environment value or script text.

`execution/oplog.py` formats one log record: `format_record(request, result, *, label)` returns the
label, the request shape, the outcome, the streams and ssh's diagnostics tail, and never the
envelope's environment values (their names only), script source or stdin. The label is the caller's
step heading, the `SSHLogger` step in effect when the request ran. `VMAccess` takes an optional
`log` callable that `run` calls with each request and its result; `SSHLogger.record` is the one the
commands pass, and it writes the block through the logger's existing sink, so redaction stays at
that one place. Every retained stream has the secret values registered when the log was constructed
(the Tailscale key, git credentials and resolved setup values) removed before it is written, and the
set is complete before the first write, as `SSHLogger` does today (`cli/agentworks/ssh.py:127-137`,
`:250-262`). R4's "sensitive" qualifies input, environment and script content alike, so script
source that an interpreter quotes in its own error output is retained output, with the caller's
secret values removed like any other. A request with `log_streams=False` keeps both streams out of
the log entirely; the option lands in slice 5 with its first callers, the file program's reads and
the native CLI. `LOG_DIR`, its readers (`doctor_state.py`, `vms/manager/lifecycle.py`,
`vms/manager/_helpers.py`, `vms/manager/power.py`, `vm logs`) and the logger move to `oplog` in
slice 8, when `SSHLogger` is deleted.

### D14. Enforcing independence (R10)

- **The package guard.** The existing fresh-interpreter guard in
  `cli/tests/execution/test_independence.py` stays until slice 8 deletes it with the legacy modules
  it guards against. It walks every `agentworks.execution` submodule with `pkgutil.walk_packages`,
  so new modules are covered automatically. `vms/contract.py` joins it in slice 3. Slice 4 adds
  explicit imports of `capabilities/vm_platform/public_ssh.py` and `vms/recovery.py`, once the
  rehome leaves `capabilities/vm_platform/__init__.py` importing no legacy code (today it reaches
  `lima.py` and `wsl2.py`, which do). The platform plugins cannot pass the guard until slice 5,
  since importing any of them loads the plugin index; they join the static check instead.
- **The test guard.** A conftest under `cli/tests/execution/` blocks the legacy modules for the
  whole directory, so the package's tests pass with them unavailable, as R10 requires.
- **Migrated consumers.** These cannot pass an import guard until the end, because most of the tree
  imports legacy modules transitively. Each slice instead adds its migrated files to a static check
  that rejects any direct import of a legacy module, top-level or inside a function. Deleting the
  modules in slice 8 proves the rest.

## Migration

The work is eight slices delivered in three PRs, as the operator chose on 2026-10-31. Each PR is
complete on its own terms and carries live evidence for what it touches (R10, R11).

- **PR A, trust and the execution core:** slices 1 and 2.
- **PR B, the VM lifecycle:** slices 3 and 4.
- **PR C, consumers and removal:** slices 5 to 8.

The slices:

1. **Trust.** The store, explicit trust modes, import, create's enrollment, managed entries, and the
   legacy VM-target factories bound to the store.
2. **Execution core with the operator exec surface.** Client resolution, `ssh_process`, the
   envelope, agw-launch with the terminal handover, results and errors. Consumers: `vm exec`,
   `vm shell`, `agent exec`, `agent shell`, `vm verify-connection` and `vm port-forward`.
3. **First contact and the VM contract.**
   - Platforms report the required tailnet address, and the vm-platform `contract_version` moves
     from 1 to 2.
   - Provisioning installs python3.
   - Phase A and release probing move onto `VMAccess`, in Enroll mode at create, with the base
     checks and Debian attestation.
   - `ProvisionResult.native_transport` goes away, and `reachability` lands for create.
   - `oplog.py` lands as `format_record`, with `SSHLogger.record` writing it and create's Phase A
     its first caller; `LOG_DIR` and its readers move in slice 8.

   Phase B stays on the legacy stack until slice 6.

4. **Recovery and the platform surface.**
   - `run_as_root` on every platform, with `root_script.py` and `public_ssh.py`, landing before
     slice 3's address settle so the settle is written once, over `run_as_root`.
   - Each platform's native code rehomed as private modules: `lima_host.py` for Lima and Remote Lima
     with the host-side detached create, the Proxmox QGA client, and the WSL2 wrappers. WSL2 and
     Proxmox run a key-free bootstrap through `run_as_root` and join after it, and the legacy
     platform transports and `plugins/proxmox/transport.py` are deleted.
   - Rekey and logout move onto core's scripts, starting a VM stops rejoining, the lazy
     repair-secret protocol goes, and `native_shell` lands.
   - `vm recover` is added, and `native_transport` and its factory are deleted.
   - The host policy (D2) lands for Remote Lima's host connection, its only caller.
5. **Files and capability delivery.** `files.py`, the `RunContext` accessors, and finite stdin with
   `Bytes(data)` and `log_streams`, with their conformance vectors. Consumers: harness, artifact,
   native-file, command-check and git-credential. `native_files`, `harness_setup.runner`,
   `admin_target()` and `agent_target()` are deleted.
6. **Initialization.** VM and agent initializers, hardening, DNS and sources.
7. **Workspaces, sessions and backup.** Workspace backends, copy, rehome and repair; tmux, sessions
   and consoles; `detached.py` with backup.
8. **Removal.** Delete the remaining legacy modules (slice 4 deletes the Proxmox transport, and
   slice 5 `native_files` and `harness_setup.runner`), doctor's `ssh` and `scp` `PATH` rows, and
   their tests (slice 2 already deleted the proof modules the package does not adopt), after the
   incident inventory is re-proved and the evidence matrix is complete.

### Incident inventory (R10)

Each row is re-proved in the slice that moves the code carrying it, and the evidence is recorded in
the plan. Rows I1 to I3 come from the transport-improv migration strategy, and the rest from
scouting. The seed is not exhaustive, so each slice audits the entry points it moves (their ADRs,
linked issues and fix history) and adds rows before anything is deleted.

| Row | Behavior                                                                                                                                                                                                                                                                                                                                                                                                                             | Re-proved in |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ |
| I1  | Closed stdin instead of a forced TTY on Windows (ADR 0020)                                                                                                                                                                                                                                                                                                                                                                           | 2            |
| I2  | Byte-exact stdin on Windows (text-mode rewrite)                                                                                                                                                                                                                                                                                                                                                                                      | 2            |
| I3  | Guest mechanics tested on real targets, not Git for Windows tools                                                                                                                                                                                                                                                                                                                                                                    | 2            |
| I4  | Sensitive input refused with a forced TTY; value-free errors                                                                                                                                                                                                                                                                                                                                                                         | 2            |
| I5  | The `--` fence for dash-led commands: inherent in a literal argv, and in `vm exec`'s shell-joined form because the line is delivered as a script file, never as a shell argument                                                                                                                                                                                                                                                     | 2            |
| I6  | The attach notice after the terminal guard                                                                                                                                                                                                                                                                                                                                                                                           | 2            |
| I6a | Console recovery after a killed `ssh -t`                                                                                                                                                                                                                                                                                                                                                                                             | 7            |
| I7  | Keepalives on long-lived paths                                                                                                                                                                                                                                                                                                                                                                                                       | 2            |
| I8  | `nohup` outside `sudo` is replaced by whole-operation elevation plus `setsid`; the output-capturing brace group is kept                                                                                                                                                                                                                                                                                                              | 7            |
| I9  | Host-key preservation across stop and start                                                                                                                                                                                                                                                                                                                                                                                          | 1            |
| I10 | Provisioning's first Tailscale join sends the auth key once, out of command lines, logs, exceptions and retained provisioning artifacts (`bootstrap_script.py:262-265`, `lima.py:181-183`, `tailscale_join.py:22-48`, `wsl2_bootstrap.py:100-120`)                                                                                                                                                                                   | 4            |
| I11 | A bash child of the payload reads no account startup file (measured in the integration report on #856: Debian bash sources `~/.bashrc` when `SSH_CLIENT` is set)                                                                                                                                                                                                                                                                     | 2            |
| I12 | No connection sharing: no `ControlMaster`, since a shared master keeps stale supplementary groups and PAM state (ADR 0015; `execution/carriers/ssh/connection.py:140-141` at `fd1256b1f`)                                                                                                                                                                                                                                            | 2            |
| I13 | The operator's identity file passed explicitly on every VM connection, for keys with non-default names on Windows (`3b8413291`; `transports/__init__.py:97`)                                                                                                                                                                                                                                                                         | 2            |
| I14 | `-T` on every non-terminal run, so an operator's `RequestTTY force` never injects a pseudo-terminal (`584ac5f6e`, `e9eb9565a`; `transports/ssh.py:186-192`, `:443`)                                                                                                                                                                                                                                                                  | 2            |
| I15 | The reconnect wait: bounded at about 240 s, with a second probe confirming a success that follows a failed attempt, with closed wording (#251; `transports/__init__.py:241`, `:272-309`)                                                                                                                                                                                                                                             | 3            |
| I16 | A failed or interrupted Phase A secures the VM before the error propagates (`vms/initializer/driver.py:188`, `:206`)                                                                                                                                                                                                                                                                                                                 | 3            |
| I17 | Azure's network propagation: the provisioning probe retries and shows the platform's hint before the error (`eb99a3f99`, `17f5c92c8`; `transports/__init__.py:223-237`)                                                                                                                                                                                                                                                              | 4            |
| I18 | A backend delete that cannot run (the site no longer binds) or runs and fails keeps the VM's row and its store record, naming the backend identifiers the row holds; `vm delete --force` removes them whatever kept the backend delete from completing, warning with those identifiers (#329; `vms/manager/power.py`)                                                                                                                | 4            |
| I19 | The agent probe tells an unreachable VM from an agent that predates the rollout (ADR 0011; `agents/manager/_common.py:237-286`)                                                                                                                                                                                                                                                                                                      | 2, 6         |
| I20 | Backup's download stops the copy and removes the partial file on Ctrl-C (`97e11a168`; `vms/backup.py:504-505`)                                                                                                                                                                                                                                                                                                                       | 7            |
| I21 | The local terminal is sanitized on entry and restored on exit after a dropped or killed attach (`897248ead`, `80c813db4`)                                                                                                                                                                                                                                                                                                            | 2            |
| I22 | An operator interrupt during Phase A still closes the kept VM's provisioning access before it propagates (`vms/initializer/driver.py:193-210`)                                                                                                                                                                                                                                                                                       | 3            |
| I23 | A first connection that left no key makes create fail where the cause is known (`vms/initializer/driver.py:438-445`)                                                                                                                                                                                                                                                                                                                 | 3            |
| I24 | The auth key is joined exactly once: a failed address read never causes a second join, and now logs the node out (`tailscale_join.py:65-71`, `7307e2f60`)                                                                                                                                                                                                                                                                            | 3            |
| I25 | The platform's availability hold spans Phase A and Phase B (WSL2 idle shutdown) (`vms/manager/lifecycle.py:643`)                                                                                                                                                                                                                                                                                                                     | 3            |
| I26 | A release the guest reports is attested by core, never taken from the platform; an unattested or mismatched VM is retained unattested and gated, with delete guidance, never recorded (`93aca2cfb`, `vms/manager/lifecycle.py:599-626`)                                                                                                                                                                                              | 3            |
| I27 | Ordinary operations refuse live release drift; adoption is the explicit `confirm-release` (`vms/manager/release.py:28-35`)                                                                                                                                                                                                                                                                                                           | 3            |
| I28 | A log's redaction set is complete before its first write, and no secret is retained in raw or shell-quoted form (`a38e49489`, `ssh.py:127-137`)                                                                                                                                                                                                                                                                                      | 3            |
| I29 | Rekey restarts `tailscaled` before the logout and after the join, with 15-second pauses, because a stale daemon hangs the logout and a rekeyed node can be unreachable (`cdd7a0045`, `vms/manager/power.py:489-518`)                                                                                                                                                                                                                 | 4            |
| I30 | Over an SSH channel, a logout or rekey whose channel drops mid-command still finishes on the guest (`21bb259f9`, `fae1d7dd2`; `vms/manager/tailscale.py:177-181`)                                                                                                                                                                                                                                                                    | 4            |
| I31 | A Tailscale key never reaches an operation log in clear; the rejoin refused any logger (`vms/manager/tailscale.py:108-118`), and recover's and rekey's loggers register the key before their first write                                                                                                                                                                                                                             | 4            |
| I32 | A cloud VM whose provider reports no public address fails with a typed error naming the console, not a connection to an empty host (#118; `transports/__init__.py:215-228`)                                                                                                                                                                                                                                                          | 4            |
| I33 | A failing logout never skips the backend delete, and an operator abort still keeps the row (#329; `vms/manager/power.py:268-310`)                                                                                                                                                                                                                                                                                                    | 4            |
| I34 | WSL2's bootstrap runs with no controlling terminal, so `dpkg` cannot stop apt with `SIGTTIN` (`1ae933a88`; `wsl2_bootstrap.py:131`)                                                                                                                                                                                                                                                                                                  | 4            |
| I35 | A native file is never read or written through a symbolic link, at the destination or any parent, and the refusal names the path and tells a link or wrong type from a denial (`25e972553`, `a2eaba12e`; `native_files.py:20-40`, `:109-115`, `:199-215`)                                                                                                                                                                            | 5            |
| I36 | Traversal needs only search permission on each ancestor, so a search-only directory above a destination does not block setup (`93a4e1a6c`; `native_files.py:32`; `docs/guides/native-harness-setup.md:49`)                                                                                                                                                                                                                           | 5            |
| I37 | A conditional publication checks the destination's digest and replaces it atomically from a sibling temporary file, so a concurrent edit fails it instead of being overwritten (`25e972553`; `native_files.py:140-184`; `cli/tests/artifacts/test_generated_sections.py:109`)                                                                                                                                                        | 5            |
| I38 | Generated-section updates and retirements keep the existing file's owner, group, mode, extended attributes and ACLs, drop an ACL the new file inherited from its directory, and leave the original untouched when the copy fails (`0decc2a75`, `3b3b9f69c`; `native_files.py:155-172`; `cli/tests/artifacts/test_generated_sections.py:193-265`)                                                                                     | 5            |
| I39 | Owned-file verification compares digest and mode on the guest without copying the content back (`4f4ab01ee`; `native_files.py:425-446`; `artifacts/publication.py:107`, `:137`)                                                                                                                                                                                                                                                      | 5            |
| I40 | Elevated native file access stays inside the dedicated root directories for desired and recorded paths alike, and retirement keeps the boundary directories (`66f0edb54`; `native_files.py:193`, `:229-238`; `artifacts/publication.py:89-100`)                                                                                                                                                                                      | 5            |
| I41 | Retirement prunes only empty parents up to the recorded package root, and keeps, with a warning, an empty root its parent will not release, after checking it is the directory it walked (`55ff7aee3`, `ad5a9733a`, `cd735e8b6`; `native_files.py:41-81`, `:408-423`)                                                                                                                                                                | 5            |
| I42 | Native file contents and native CLI commands and output stay out of the operation log (`25e972553`; `native_files.py:334-354`; `plugins/_harness_native/native_cli.py:170-185`; `docs/guides/native-harness-setup.md:55-57`)                                                                                                                                                                                                         | 5            |
| I43 | Native executables are resolved once from the actual user's login `PATH`, and an explicit `PATH` in the setup environment wins (`7f4a79b5c`; `plugins/_harness_native/native_cli.py:29-57`, `:118-144`)                                                                                                                                                                                                                              | 5            |
| I44 | Session-resume probes (Claude's transcript, Codex's recorder and rollouts, Grok's session) read their exit code, so a probe that could not run never reads as no conversation and never launches over one (`86de4c6a3`, `a3965d7d8`, `fb801c5e3`; `plugins/claude/harness_integration.py:495-537`; `plugins/codex/harness_integration.py:1148-1340`)                                                                                 | 5            |
| I45 | Probe diagnostics have every secret the operation resolved or its log registered removed, raw, shell-quoted and normalized, and are bounded to one line, before they reach an error; plain environment values are kept. Setup facets pass `SetupInvocation.redactions` and session starts the artifact context's set, both built from the operation log (`15b714480`, `5b51afcab`, `d323e5832`; `artifacts/native/probe.py:411-417`) | 5            |
| I46 | Native discovery requests travel on stdin, so a large package needs no argument (`14a4b1336`; `artifacts/native/probe.py:402-410`)                                                                                                                                                                                                                                                                                                   | 5            |
| I47 | A settings write that finds nothing to change leaves the file and its permissions alone (`9e20740e7`, `e77d1a24f`; `plugins/_harness_native/native.py:122-123`; `docs/guides/native-harness-setup.md:52-53`)                                                                                                                                                                                                                         | 5            |
| I48 | GitHub and Azure CLI readiness is checked read-only in the target user's shell with the CLI's output suppressed, never blocks helper installation, and warns when the result is indeterminate (`f72884fdd`, `11e1e62ba`, `b2f1dc992`; `capabilities/git_credential/github.py:156-186`; `plugins/azure/azdo.py:138-166`; `capabilities/git_credential/README.md:96-99`)                                                               | 5            |
| I49 | A required-command check runs in the user's login and interactive shell and reports the command's own status apart from a startup failure (`b2f1dc992`; `command_checks.py:15-77`)                                                                                                                                                                                                                                                   | 5            |
| I50 | A setup request's prepared environment reaches it unchanged when it runs elevated (`5797e5110`; `harness_setup/runner.py:54-57`)                                                                                                                                                                                                                                                                                                     | 5            |
| I51 | A plugin's request environment cannot override or add `AGENTWORKS_*` identity values (`bcca20a86`; `harness_setup/runner.py:29-37`; `docs/guides/harness-facets.md:189-192`)                                                                                                                                                                                                                                                         | 5            |
| I52 | When Claude's version cannot be read in the login environment, the session-prompt workaround warns and launches without `--system-prompt-snapshot` instead of failing (`21a9b4b7d`; `plugins/claude/artifacts.py:178-201`)                                                                                                                                                                                                           | 5            |
| I53 | The native probe finds the harness command and its native home override (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GROK_HOME`) in the user's login and interactive environment, so one set only in a dotfile is honored and a missing command is reported before publication (`951f21841`, `3b4ea08b6`; `artifacts/native/probe.py:28-49`)                                                                                                 | 5            |

Rows I29 to I34 cite `60d347220`, the platform LLD's baseline, and rows I35 to I53 cite `e645220ad`,
the files LLD's.

### Release behaviors to preserve (R10)

The transport-improv migration strategy's release-behavior table needs no inventory of its own here.
Each of its rows is either a decision this design already makes (the probe's login environment in
D5, metadata and ACL preservation in D6, python3 from provisioning in D10) or behavior of a module
this effort only repoints onto `VMAccess` (settings merges, publication and sections, session
identity, restart confirmation), whose existing tests stay green in the slice that moves its caller.
The source table's row asking that destination and home paths come from composition and bounded file
observations rather than discovery commands is dropped on purpose: those probes (the machine-id read
and destination `stat` at `harness_setup/dispatch.py:47`, `:52`, and the `HOME` read at
`artifacts/session.py:76`) stay ordinary read-only requests, and the `HOME` they see is the one
agw-launch rebuilt from the passwd entry (D4).

## Evidence matrix (R11)

Supported pairings, from each platform's `unsupported_reason` and readiness checks, less local Lima
on Windows, which the operator ruled on 2026-10-31 is not supported because it runs on WSL2 and adds
nothing beyond the WSL2 platform:

| Platform                 | Linux | macOS         | Windows |
| ------------------------ | ----- | ------------- | ------- |
| Lima, local              | yes   | yes           | no      |
| Remote Lima (macOS host) | yes   | yes (primary) | yes     |
| WSL2                     | no    | no            | yes     |
| Proxmox                  | yes   | yes           | yes     |
| AWS                      | yes   | yes           | yes     |
| Azure                    | yes   | yes           | yes     |
| GCP                      | yes   | yes           | yes     |

That is 18 cells. Lima's code still accepts local placement on Windows whenever `limactl` is
installed (`capabilities/vm_platform/lima.py:189`); issue #861 refuses it there, as the operator
ruled on 2026-11-01. Each slice names the cells it covers. Removal needs every cell, or the
operator's disposition of it.

## ADR amendments

| ADR                               | Change                                                                                                                                                                                                                                                         | Slice |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| 0013 (secret injection)           | Secret environment travels in the request on stdin, not as `-o SetEnv` arguments                                                                                                                                                                               | 2     |
| 0014 (`AcceptEnv` wildcard)       | The transport no longer sends environment through SSH; slice 8 decides whether the fragment still has a user                                                                                                                                                   | 2, 8  |
| 0017 (console pane environment)   | Superseded: console panes take their environment from the terminal handover (D8), retiring `tmux -e` and the `--preserve-env` carry                                                                                                                            | 7     |
| 0003 (Tailscale networking)       | Native-transport sessions become the clouds' `run_as_root` and native shell routes; ephemeral keys stay supported without a rejoin on start: a VM whose node was removed while offline fails `vm start` with the recover hint, and `agw vm recover` rejoins it | 4     |
| 0019 (command plans)              | Its sanctioned conditional late acquisition of the rejoin key goes with the lazy repair-secret protocol                                                                                                                                                        | 4     |
| 0019 (command plans)              | Its `admin_target`/`agent_target` description moves to the new accessors                                                                                                                                                                                       | 5     |
| 0025 (one current Debian release) | Core attests the release over SSH as the first contract request; the vm-platform capability is version 2                                                                                                                                                       | 3     |

## Risks and early proofs

| Risk                                                                                                                          | Proof, and its slice                                        |
| ----------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Stage 0 parsed alike by `sh`, `dash`, `bash` and `zsh`, with noisy startup files                                              | Conformance vector per shell, slice 2                       |
| A startup file that redirects the standard streams or reads stdin                                                             | Conformance vectors, slice 2                                |
| Exact stdin positioning, so the payload reads the caller's first byte                                                         | Conformance vector, slice 2                                 |
| `-F none` and the stdin protocol on 8.5 and on Win32-OpenSSH                                                                  | Round trip from all three workstations, slice 2             |
| Copying the operator's stdin for `stream` on Windows: binary mode, end of file, early exit                                    | Live `vm exec` from Windows, slice 2                        |
| The terminal handover: timeout, and cleanup when the second connection fails                                                  | Conformance vectors and live attach from all three, slice 2 |
| `sudo -n` passing stdin through under Debian's sudoers defaults                                                               | Elevated request on a fresh Debian 12 and 13 VM, slice 3    |
| python3 at provisioning on every platform's image                                                                             | Create on every platform, slice 3                           |
| The end marker and trailer with a background child holding both streams, and stdout's tail arriving after the trailer         | Conformance vector, slice 2                                 |
| The shared lock file under `/run/lock` with `protected_regular`, a creator in another group, and `flock`, on Debian 12 and 13 | Conformance vector, slice 5                                 |
| Encrypted keys served by the agent                                                                                            | Live cell, slice 1                                          |
| Cloud host-key re-establishment                                                                                               | Lost-record recovery on one cloud, slice 4                  |
| `GlobalKnownHostsFile=NUL` with Win32-OpenSSH on the legacy paths                                                             | Cloud create from Windows, slice 1                          |

## Operator decisions

The operator answered the design's open questions on 2026-10-31 and 2026-11-01. The
requirement-level rulings (R3, R7, R11 and python3 at provisioning) are quoted in the FRD.

1. **Command names.** `agw vm import-host-keys` and `agw vm recover` stand.
2. **Account shell startup (R3).** R3 now accepts the account shell's own startup running before
   stage 0. It lists what agw-launch resets and states resource limits, scheduling priority, a
   startup file's background process and swapped discarding streams as known gaps (D4). Managed
   accounts are limited to the `sh`, `dash`, `bash` and `zsh` login shells, a new restriction on
   operator-authored config, which this design needs for the constant command line to parse.
3. **Automatic rejoin (R7).** Neither `vm start` nor the activation gate rejoins Tailscale. Each
   starts the VM, waits for it to answer over SSH, and fails with a hint naming `agw vm recover`
   when it does not (D11). A VM whose ephemeral-key node expired while it was stopped therefore
   fails to start with that hint, and recover rejoins it; the operator chose to document this.
4. **Local Lima on Windows** is not a supported pairing (evidence matrix). The operator first added
   it, then reversed that the same day, and issue #861 refuses it in Lima's readiness check.
5. **The launcher's name** is `agw-launch`, the Agentworks launcher.
6. **Three PRs** instead of eight (migration).
7. **Delivery.** Every request travels on stdin, agw-launch is written in Python, python3 is a
   provisioning requirement, and terminals use the two-connection handover.
8. **Stage 0** reads agw-launch from stdin, so the command line stays one short constant line.
9. **R1** says provisioning's first Tailscale join sends the auth key once and keeps it out of
   retained artifacts, command lines, logs and exceptions (incident row I10), and init keeps
   installing python3.
10. **Unsupported, with an alert.** Shells without `--workspace` print no MOTD or last-login line,
    ssh never prompts for a key passphrase, and pam_env is not applied. Where an operator could be
    relying on one of these, Agentworks says so: a failed connection with a passphrase-protected key
    hints at the SSH agent (D2), and pam_env settings draw a warning (D10). The operator asked for
    no extra machinery beyond that.
11. **Script-bundle capabilities** are a later effort, outside this one. This design supplies the
    pieces they need: `Script` and `PythonProgram` requests that take their inputs as data, the file
    operations, the contract's check table, and the identities and elevation of `VMAccess`.

## Awaiting the operator

Nothing. The operator ruled on 2026-11-01 (FRD rulings): R1 now carries the auth-key rule, and init
keeps installing python3.

## Alternatives considered

- **The whole request by `SendEnv`**, this document's first draft. It needed the AcceptEnv fragment
  at provisioning, a sudo `env_keep` line, numbered parts under a 256 KiB cap and a not-ready path
  for VMs that lacked the AcceptEnv fragment, and it passed secrets through the login shell's
  environment.
- **`SendEnv` for terminal requests only.** A second delivery path kept for one mode.
- **Relaying the terminal through agw.** ssh takes window size and terminal modes from its own
  stdin, so agw would have to relay a pseudo-terminal on three workstation operating systems, and
  wait for the guest to turn echo off before sending the request.
- **A reverse-forwarded socket with a one-time token by `SendEnv`.** It saves the second handshake,
  but adds a workstation listener, a forward that may not be ready when the command starts, and an
  unmeasured Windows forward of a VM-side Unix socket.
- **A long-lived control channel multiplexing requests.** It would bring back the stale
  supplementary groups and PAM state that ADR 0015 removed connection sharing to avoid.
- **`/bin/sh` as every managed account's login shell.** It would change the operator's direct SSH
  sessions.
- **The launcher in Bash.** Exact stdin reads, a rebuilt environment, the handover socket and the
  file programs are all simpler in Python's standard library.

## Requirements traceability

| Requirement | Decisions                          |
| ----------- | ---------------------------------- |
| R1          | D10, D11, incident row I10         |
| R2          | D2, D8, D11                        |
| R3          | D3, D4, D5                         |
| R4          | D2, D3, D4, D5, D8, D13            |
| R5          | D6                                 |
| R6          | D7                                 |
| R7          | D11                                |
| R8          | D1, D2, D9, D11                    |
| R9          | D12                                |
| R10         | D14, Migration, incident inventory |
| R11         | Evidence matrix, plan              |
