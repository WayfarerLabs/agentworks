# Transport Take 2: High-Level Architecture

- Status: Draft for review. Nothing here is implemented.
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
script, environment, working directory, interpreter and elevation, followed by the caller's own
input. Buffered and streaming commands, terminal attachment, files and detached work are all built
on that one mechanism. Host keys live in an Agentworks-owned store bound to the VM.

Platforms stop exposing a general native transport. Each one keeps its native access private and
supplies a single recovery capability that runs a core-owned script as root. Provisioning reports
the tailnet address. Core's first connection to a new VM enrolls its host key and checks the VM
contract.

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

 platform plugins: private native access + run_recovery(script, stdin) + optional native shell
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

| Module                                   | Responsibility                                                                                                                                    | Adopted from                                                      |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `execution/client.py`                    | Resolve the OpenSSH executable once to an absolute path, read its version and build, refuse unsupported builds before dispatch                    | `carriers/ssh/client.py` version probe                            |
| `execution/ssh_process.py`               | Build a client argv from a policy (VM or host) and run it: stdin source, stdout and stderr pump, local deadline. The only place that spawns `ssh` | `carriers/ssh/connection.py`, `_io.py`                            |
| `execution/trust.py`                     | The VM-bound host-key store: lookup, enrollment, import, re-establishment, removal                                                                | new                                                               |
| `execution/envelope.py`                  | Encode a request into its envelope; frame and parse the start and completion markers                                                              | `preparation.py`, adapted                                         |
| `execution/launcher.py`                  | `agw-launch`: the constant stage 0, the Python launcher it receives, and the terminal handover                                                    | `bootstrap.py` ideas, rewritten in Python                         |
| `execution/requests.py`, `results.py`    | The caller vocabulary and typed outcomes                                                                                                          | `preparation.py` types; `carrier.py` ideas                        |
| `execution/errors.py`                    | Typed failures, each subclassing the matching `agentworks.errors` kind                                                                            | new                                                               |
| `execution/access.py`                    | `VMAccess`: identity-bound, passive entry point holding `run`, `stream`, `attach` and `forward`, plus `files` and `detached`                      | new                                                               |
| `execution/files.py`                     | File operations built on `run` (D6)                                                                                                               | semantics from `native_files.py` callers                          |
| `execution/detached.py`                  | Launch, observe and stop work that outlives its connection (D7)                                                                                   | `remote_exec.py` wrapper idea, copied                             |
| `execution/attach.py`, `forwarding.py`   | Terminal attach under `agentworks.terminal.guarded_terminal`; port forwards with an explicit lifetime                                             | `transports/base.py`, `vms/manager/tailscale.py`                  |
| `execution/reachability.py`              | Wait until a VM answers over SSH, with the operator-facing wait wording                                                                           | `transports.wait_for_reconnect`                                   |
| `execution/oplog.py`                     | Per-VM operation log: label, request shape, outcome; never payloads                                                                               | `SSHLogger` file layout                                           |
| `capabilities/vm_platform/public_ssh.py` | The cloud platforms' SSH to a VM's public address, for provisioning and recovery, composed from `ssh_process`                                     | cloud `SSHTransport` uses                                         |
| `vms/contract.py`, `vms/recovery.py`     | The VM contract checks; core's recovery scripts and `vm recover`                                                                                  | new; scripts from `vms/initializer/credentials.py` and `power.py` |

`agentworks.terminal` is not one of the six legacy modules, so attach keeps using its guard. The
proof modules that do not fit are deleted when the package is adopted: `carrier.py`'s generic
carrier protocol, `carriers/proxmox.py` and `carriers/_proxmox_http.py` (R10).

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
  MSI install). It accepts a build only when its `ssh -V` banner is the `OpenSSH_for_Windows_` form.
- **Every workstation** requires 8.5 or newer. A missing, unsupported or too-old client gets a typed
  `ClientUnsupported` refusal before credentials are resolved. The refusal names the paths tried and
  how to install a supported client.
- **Windows 10.** Its inbox client is 8.1 (measured, per the salvage message), so a Windows 10
  workstation needs the MSI install. The refusal says so.
- **Doctor.** `agw doctor`'s existing check, which looks for `ssh` and `scp` on `PATH`, becomes this
  resolver's verdict, since scp is no longer used.

### D2. Policies and request delivery (R4, R8)

`ssh_process` builds every client argv from one of two policies.

- **VM policy**, for every connection the transport makes to a VM:
  - **Options.** `-F none`, so no operator or system configuration applies, plus explicit `-o`
    options adopted from the proof's list. These include `BatchMode`, connection sharing off per ADR
    0015, agent and X11 forwarding off, no proxy commands, `ConnectionAttempts=1`, an explicit
    `ConnectTimeout`, and the 8.5 `CertificateFile` workaround.
  - **Identity.** `IdentityFile` is `operator.ssh_private_key` with `IdentitiesOnly=yes`. The
    default agent stays enabled, so an encrypted key can be served by it, but only that identity is
    offered.
  - **Trust.** The VM's store options (D9).
  - **Keepalives.** `ServerAliveInterval` and `ServerAliveCountMax`, which the current stack also
    applies, on streaming, attach and forward.
- **Host policy**, for platforms' own access to a host or to a VM's public address. It honors
  operator configuration, which R8 allows outside VM connections (ruled on 2026-10-23), and the
  platform supplies the trust options. Remote Lima's host connection depends on that today.

**Request delivery.** Everything request-bearing travels on the client's stdin, never on a command
line or in the environment.

- **What reaches the command line.** The client's arguments are the policy options, `-T` or `-tt`
  (`-N -L` for a forward), the destination and the constant stage 0 command (D4). None of them
  carries request data.
- **Why stdin.** The pipe belongs to the client, so request content appears in no process list on
  either machine and never touches disk. It has no size limit, and it depends on no client version
  and no sshd configuration beyond the admin's existing passwordless sudo, so it works from core's
  first connection to a new VM.
- **Terminals.** An attach is the one case where stdin is the operator's terminal. It uses two
  connections (D8), so its request still travels on a private stdin.

### D3. The request envelope (R3, R4)

The client writes three things on stdin, in order:

1. **The agw-launch source**, length-prefixed, which stage 0 reads and runs (D4).
2. **The envelope**, a length-prefixed JSON object holding a random nonce, the elevation choice, the
   interpreter and startup flags, the literal argv or script source, the environment and the working
   directory. Its values are strings, which the client refuses if they contain NUL, so JSON carries
   every value R3 names exactly, newlines, quotes, `%` and `${...}` included. It needs no version
   tag, because the client sends agw-launch and the envelope together from one build.
3. **The caller's input**, untouched, until end of file.

- **Exact reads.** One reading function, defined by stage 0 and reused by agw-launch, reads each
  prefixed part with unbuffered reads of exactly its length. The payload inherits stdin positioned
  at the caller's first byte, with no copy loop in agw-launch. (Where sudo's I/O logging is enabled,
  sudo itself relays stdin for an elevated request.)
- **Size.** Nothing bounds a request but memory. The largest program in a request today is the
  native probe (`artifacts/native/probe.py`, 14,391 bytes).
- **What it replaces.** The current stack's coalesced `-o SetEnv` truncates at a newline and is
  first-value-wins (ADR 0014), and OpenSSH 10.x expands `%` and `${HOME}` in `SetEnv` values where
  9.x does not (#845). Nothing in the envelope passes through OpenSSH option parsing. The config and
  secret refusals of values containing a newline (`manifests/decode.py:254-261`, the environment use
  in `env/compose.py:73-81`) exist only for `SetEnv`, so they retire in slice 7 with its last user.

### D4. agw-launch and completion (R3, R4)

**Stage 0.** The remote command is one constant line, `/usr/bin/python3 -I -S -c '<stage 0>'`, with
`sudo -n` in front for an elevated request and a handover path after it for the terminal half of an
attach (D8). Stage 0 is one line of under 200 characters: it reads the length-prefixed agw-launch
source from stdin, or from the handover socket, and runs it. On any failure to read or start it,
stage 0 exits with a fixed status and prints nothing, so a startup file that ate the stream gives a
clean "not delivered" rather than a traceback. It contains no single quote, so the account shell
passes it through unchanged. `-I` ignores Python's environment variables and the user site
directory, and `-S` skips site packages, so no Python startup applies. The short line stays far
below Windows' 32,767-character command-line limit and keeps sudo's log entry to one line. Elevation
is the whole operation running under `sudo -n`, so no variable has to survive sudo.

**Account shell precondition.** sshd hands that command to the destination account's login shell as
`$SHELL -c`, so the account shell parses one constant line before stage 0 runs. Managed accounts
must use `sh`, `dash`, `bash` or `zsh`. The admin and agent templates enforce this at the config
boundary, and the base contract check verifies it for the admin and every agent (D10). A shell
outside that list, such as `tcsh`, was measured (salvage list) parsing the old Bash launcher line
wrongly while reporting an ordinary-looking status. The Python stage 0 did parse under `tcsh` in the
integration report on #856, but the list stays to the four shells the conformance vectors cover.
Before stage 0 starts, the account shell may read its own non-interactive startup file: zsh's
`.zshenv`, or the `.bashrc` Debian's bash reads under sshd. R3, as amended on 2026-10-31, accepts
that because nothing those files do reaches the command: agw-launch drops output written before its
start marker, and it rebuilds the environment, directory and umask itself. A startup file that reads
stdin would consume the request; the request then fails as not delivered, and a conformance vector
covers it.

**What agw-launch needs.** The python3 of every recognized Debian release (`debian.py`), which
provisioning installs (D10), its standard library, and `sudo` for elevation. It is written in plain
syntax that needs no particular python3 release. It is rewritten in Python from the proof's
`bootstrap.py`, keeping that program's measured handling of locale and private names.

**Relationship to agw-atc.** The follow-up containment effort introduces `agw-atc`, an installed,
long-running VM program for sessions, limits and process-to-session lookup. The two stay distinct by
lifecycle: agw-launch arrives with every request and always matches the client that sent it, and
agw-atc is installed and versioned with the VM. The operator asked on 2026-11-01 for the two to be
aligned ("It feels like we should align these one way or another"), and the lead's answer is to keep
two names and fix this design's side: agw-launch stays the only way a request starts, and it
outlives its own request only as the brief parked half of a handover. Work agw-atc will own is a
request whose payload runs `agw-atc`, so running a request never depends on agw-atc being installed.
The containment effort's own SDD owns everything else about agw-atc. The saga's waves 5 and 7 ruling
adds the other constraint on it: a future VM-side observability wrapper, if one proves necessary, is
the same installed program (`2026-08-04-next-steps/target-state.md`).

**What agw-launch does:**

1. **Reads the envelope.** A malformed one fails before the start marker, so the request is not
   delivered.
2. **Checks its standard streams.** sshd gives the command pipes or sockets, stdout and stderr
   distinct (the terminal in TTY mode). If a startup file redirected one, for example with
   `exec >/dev/null` or `exec 2>&1`, agw-launch exits with a fixed status before the start marker,
   so the payload never runs and the request is reported as not delivered. A startup file that swaps
   both streams for other pipes that discard their input still defeats the check; that contrived
   case is a stated residual with the resource limits below.
3. **Writes the start marker**, `<nonce> start`, to stdout and to stderr.
4. **Resets what the payload inherits:**
   - the environment, rebuilt from `HOME`, `USER`, `LOGNAME` and `SHELL` from the identity's passwd
     entry, a fixed `PATH`, the locale, then the session variables today's shells and session
     workloads use, each taken from a source startup files cannot touch: `TERM` from the envelope
     (the operator's terminal type, with a default the LLD sets for a Windows workstation that has
     none) and `SSH_TTY` from the terminal's name in TTY mode, and `XDG_RUNTIME_DIR` as
     `/run/user/<uid>` when that directory exists and belongs to the identity; then the request's
     environment. Nothing is copied from the inherited environment, so `SSH_CLIENT` and
     `SSH_CONNECTION`, which no Agentworks code reads, are no longer passed, and release notes say
     so;
   - the working directory (the home directory by default) and a fixed umask;
   - signal dispositions back to their defaults, and no inherited descriptor above 2.

   Three things cannot be reset: resource limits and scheduling priority a startup file lowered,
   since an unprivileged process cannot tell them from PAM's or raise them again, and a background
   process a startup file leaves holding the session's streams, which can write into the payload's
   output after the start marker. Keeping that process out would mean framing every byte of payload
   output. R3 states them as known gaps (operator, 2026-11-01).

5. **Runs the payload as a child.** A literal argv runs with no shell. A script runs under the
   chosen interpreter, which reads the source from a file descriptor and never from its arguments
   (R4). Login or interactive startup applies only when requested. `sh` and `bash` are fixed paths.
   The default shell is the destination identity's passwd entry. A missing interpreter fails rather
   than being substituted.
6. **Reports completion.** Except in TTY mode, after the payload exits it writes one framed trailer
   to stderr: `<nonce> exit <status>`, or `<nonce> refused <reason>` when it could not start the
   payload (a missing interpreter or passwd entry). In TTY mode it execs the payload instead.

**Completion rules:**

- **Start marker.** A marker on either stream means the payload may have started. The client
  discards everything before the start marker on each stream, so shell-startup output never reaches
  the command's caller. On the terminal half of an attach the client is not in the stream, so such
  output can appear on the operator's terminal; it still never reaches the command.
- **The trailer is the only completion evidence.** The client strips it, so a guest exit of 255 is
  reported as completed with status 255. Once the trailer and the remote exit status have arrived,
  the client keeps reading for a short, fixed linger and then ends the client process. Bytes that
  arrive in that window, from background children of the payload, are kept as stderr. It never waits
  for end of file, because a background child that holds stderr would otherwise stall the request
  for its whole lifetime (measured in the integration report on #856).
- **Not delivered.** A remote exit status other than 255 with no start marker means the payload
  never started: a clean exit delivers all prior output first, and step 2 refuses streams the marker
  could not reach. Status 127 without `sudo` there means the account shell found no python3, which
  is typed `VMNotReady`. Under `sudo` a missing python3 exits 1 instead; the unelevated base check
  (D10) finds it first.
- **Unknown.** Everything else without a trailer is unknown, whatever the client's own exit status:
  a start marker with no trailer, or 255 with no start marker. The client does not read OpenSSH's
  diagnostics to tell a refused connection from a dropped one. It reads one OpenSSH token only to
  type an error: `Host key verification failed` on a 255 becomes `TrustMismatch`, naming
  `agw vm recover`, since a host key that no longer matches the store needs the operator.
- **TTY mode.** An attach never elevates to root; an elevated attach request is refused before
  dispatch, and no current attach consumer elevates. The root `sudo -n` prefix and the handover
  argument are never combined. An attach returns its exit code; a 255 is reported as "connection
  ended or command exited 255", never guessed.
- **Forgery.** The nonce is never in the payload's argv or environment, so ordinary payload output
  cannot forge a marker. A hostile process of the same user could read agw-launch's memory. That is
  outside R4's threat model, which concerns honesty, not a hostile same-user process.

### D5. Requests and results (R3, R4)

| Type                   | Shape                                                                                                                                                                        | Current consumers                                                                         |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `Command`              | Literal argv tuple                                                                                                                                                           | most buffered calls                                                                       |
| `Script`               | Source plus `Shell.SH`, `Shell.BASH` or `Shell.USER_DEFAULT`, with `login` and `interactive` flags                                                                           | initializers                                                                              |
| `PythonProgram`        | Core-owned Python source and arguments, run by `/usr/bin/python3 -I -S` reading the source from a file descriptor, with the same `login` and `interactive` flags as `Script` | the file program, native probing, the native CLI's login resolution                       |
| Common options         | `env`, `cwd`, `elevated`, `deadline`, `observation`                                                                                                                          | `observation`: status probes that retry today                                             |
| Input                  | `NoInput`, `Bytes(data, sensitive=)`, `FromFile(path)`                                                                                                                       | `FromFile`: workspace copy (slice 7)                                                      |
| Output sink (buffered) | `Capture(limit)`, `Discard`, `ToFile(path, on_progress=)`                                                                                                                    | `Discard`: `command_checks.py`, native CLI; `ToFile`: backup and workspace copy (slice 7) |
| `Result`               | `completion` is `Completed(status)`, `NotDelivered(reason)` or `Unknown(reason)`; `stdout` and `stderr` are bytes                                                            | all                                                                                       |

Each vocabulary item lands in the slice that migrates its first consumer. `PythonProgram` is not one
of R3's script interpreters. It carries core's own Python programs, which travel in `python3 -c`
argv on `main` (`artifacts/native/probe.py:402`, `native_files.py:338`), so their source stays out
of guest arguments (R4). With `login` and `interactive`, the identity's default shell runs that
startup and then execs the interpreter, with the program's arguments passed positionally so no
quoting is needed. That is how the native probe and the native CLI's login resolution keep seeing
the native workload's login and interactive environment, as they do today through `"$SHELL" -lic`
(`probe.py:402-410`). The interactive half matters: the seeded `.bashrc` returns early for a
non-interactive shell before it sources the mise activation
(`capabilities/vm_platform/skel.py:46-50`, `:124-125`).

- **Bytes, not text.** Results carry bytes with no newline conversion; a `text()` helper decodes.
  Stdin is written in binary mode on every workstation (incident row I2).
- **Sensitive inputs.** Sensitive input, environment values and script content never appear in logs,
  exceptions or retained artifacts. A secret-bearing environment does not suppress output (R4).
  Retained output has the request's secret values redacted (D13), and an explicitly sensitive stdin
  marks the streams as not retained at all.
- **Deadlines.** A deadline bounds the local wait, kills the local client and returns `Unknown`. It
  never claims remote termination; the predecessors measured guest trees outliving the deadline, and
  cancellation stays deferred.
- **Retries.** Only `observation=True` requests may retry, and only after `NotDelivered` or
  `Unknown`. Mutating requests are never re-dispatched.
- **`check=True`** turns anything but `Completed(0)` into a typed error carrying the `Result`.

### D6. Files built on execution (R5)

Every file operation is a `run` of one core-owned Python file program, sent as a `PythonProgram`
(D5) with the operation as its first argument, and content on stdin or stdout. It starts from the
file program `native_files.py` already dispatches the same way on `main` (`_FILE_PROGRAM`). Keeping
it a separate payload, rather than part of agw-launch, keeps file code off the requests that do not
use it. There is no scp.

| Operation                                   | Mechanism                                                                                                                                                                                                                                                                                                              | Current consumers (slice)                                                                        |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `read(path) -> (bytes, digest)`             | Open without following symlinks and copy to stdout; the workstation computes the digest from the bytes it received                                                                                                                                                                                                     | artifacts, native CLI transcripts, settings (5)                                                  |
| `write(path, data, mode, owner, group)`     | Under the lock: a temporary file in the destination directory filled from stdin, owner and mode set on it, `fsync`, then rename. With `preserve=True` the temporary file takes the existing file's owner, group, mode and extended attributes (ACLs) instead, as `native_files.py` does today (`:120-126`, `:153-172`) | initializer fragments, profiles, tmuxinator, sources (5, 6)                                      |
| `write_if(path, data, expected, ...)`       | As `write`, after comparing the current digest (or absence) with `expected` under the lock, with the same `preserve` option                                                                                                                                                                                            | artifact publication (with `preserve=True`, `artifacts/publication.py:205`), settings merges (5) |
| `update(path, transform, ...)`              | `read`, apply `transform` on the workstation, `write_if`; retry on conflict                                                                                                                                                                                                                                            | harness JSON and TOML settings, marked profile blocks (5, 6)                                     |
| `remove(path)`, `remove_if(path, expected)` | Under the lock                                                                                                                                                                                                                                                                                                         | artifact cleanup (5), workspaces (7)                                                             |
| `ensure_directory`, `prune_empty_parents`   | Operations of the same program                                                                                                                                                                                                                                                                                         | artifacts (5), workspaces (7)                                                                    |
| `upload_tree(local_dir, dest, replace=)`    | A `tarfile` stream from the workstation into `tar -x -C dest`; `replace` clears `dest` first, as `copy_dir_to` does today                                                                                                                                                                                              | sources' `fetch_dir` (6)                                                                         |

- **One lock for every writer.** Every mutating file operation, tree uploads and removals included,
  takes `flock` on one VM-wide lock file, `/run/lock/agentworks-files.lock`. `/run/lock` is a
  sticky, world-writable tmpfs on Debian, so whichever identity first needs the file creates it and
  a boot clears it; no provisioning, init step or contract check is involved. Because Debian sets
  `fs.protected_regular` (2 on this design's own Debian 12 host), opening another identity's file
  there with `O_CREAT` fails, so the file program opens it read-only without following symlinks,
  creates it with `O_CREAT|O_EXCL` only when it is missing, sets mode 0644 explicitly whatever the
  umask, and on losing that race opens it read-only again. One fixed file means a tree upload and a
  conditional write beneath it can never hold different locks, and replacing a directory never
  replaces a lock, so `write_if` is atomic with respect to every Agentworks writer, which is R5's
  cooperating-writer guarantee. A tree upload extracts into a temporary directory beside its
  destination without the lock and takes it only to move the result into place (with `replace`, the
  old tree aside and the new one in), so every hold is short, and a lock wait has a deadline.
  Workspace copy's opaque tar stream is a `run`, not a file operation, and takes no lock. An agent
  that creates the file unreadable, or holds the lock to stall other writers, is a hostile writer,
  which stays with the permissions effort along with hostile path races.
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
  stdin into the pipe in binary mode until end of file. Stdout passes through after the start
  marker, and stderr goes through the trailer filter, which holds back only bytes that could begin a
  trailer, so live stderr is never delayed by a timer. Consumers: `vm exec`, `agent exec`.
- **`attach(request, clear_screen_on_exit=)`** uses two connections, so ssh keeps the operator's
  real terminal (its window size and modes come from its own stdin) while the request still travels
  on a private stdin. A first `-T` request carries the envelope marked for handover: agw-launch
  listens on a Unix socket in a fresh 0700 directory, reports the path and waits. The client then
  runs `ssh -tt` under `guarded_terminal` with the handover form of stage 0 naming that path, which
  fetches the agw-launch source and the envelope and execs the payload on the terminal as in D4. The
  parked agw-launch serves one connection, removes its directory and exits with a trailer, or exits
  after 30 seconds with none, so nothing is left behind. Only the account's own processes can reach
  the socket, and they can already read that account's processes. Every attach pays one extra SSH
  handshake, which ADR 0015 measured at 150 to 300 ms. The post-guard notice for a dropped
  connection and the Windows clear behavior are kept. Consumers: session and console attach,
  `vm shell`, `agent shell`.

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

- **`forward(local, remote)`** is a context manager running `ssh -N -L` under the VM policy, minus
  the forwarding restrictions, with `ExitOnForwardFailure=yes`. It returns once the local port
  accepts a connection, raises a typed error with the client's diagnostic if the client exits first,
  and ends the client on exit. Consumer: `vm port-forward`.
- **`reachability.wait(access, ...)`** polls an observation request until it completes, with today's
  wait wording and flap double-check. Consumers: create's post-Tailscale step, start, rejoin and
  rekey.

### D9. The VM-bound host-key store (R8)

**Trust modes.** There are three, and the code that builds a connection chooses one explicitly:

| Mode       | Used by                                                                                         | Options                                                                                                                                                                                                                         |
| ---------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| VM         | every connection to a VM, at its tailnet or public address                                      | the VM's store file and alias, `StrictHostKeyChecking=yes`                                                                                                                                                                      |
| Enroll     | create's first core connection only                                                             | the VM's freshly reset store file and alias, `StrictHostKeyChecking=accept-new`                                                                                                                                                 |
| Unverified | cloud provisioning before the VM is in the store; a cloud host-key read when the record is lost | a temporary known-hosts file for that one run, `StrictHostKeyChecking=accept-new`, deleted when the run ends. First-use trust that pins for the rest of the run, as provisioning has today, without writing the operator's file |

Host connections such as Remote Lima's keep their own configuration and are not VM trust at all.

- **Layout.** One file per VM, at `~/.config/agentworks/known_hosts/vm-<vm>` (the prefix keeps
  Windows reserved names such as `con` usable), in `known_hosts` format, with records under a
  host-key alias derived from the VM name. Every connection pins the global known-hosts file away,
  plus `UpdateHostKeys=no` and `HashKnownHosts=no`, as `-o` options that configuration cannot
  override. Slice 2's VM policy uses `none` values (`GlobalKnownHostsFile=none`,
  `KnownHostsCommand=none`) under `-F none` with D1's 8.5 client. Until consumers move to it, the
  legacy paths keep their client and pin `GlobalKnownHostsFile` to the null device instead, since a
  `none` value before 8.5 names a file in the working directory ([trust-lld.md](./trust-lld.md)).
- **Identity.** The VM name keys `vms` (`db/migrations.py:240`) and survives every tailnet address
  change. Delete removes the store file, and create resets it, so a reused name never inherits a
  key.
- **Lookup.** A missing file is a typed `TrustMissing` naming the import and recovery commands. It
  never falls back to first use, so a missing record never makes an existing VM new.
- **Enrollment.** Create resets the VM's store file and makes its first core connection in Enroll
  mode. That connection runs the base contract check (D10). The store is the only place the key is
  written. If create is interrupted after the reset, the VM is retained as failed. From PR B,
  re-establishment (below) is its recovery path. In PR A, which ships before recovery, the remedy is
  delete and recreate, and a VM with no importable entry can be brought in with an operator-verified
  file passed to `--known-hosts` (`ssh-keyscan` output checked against the console's or provider's
  fingerprints). The `TrustMissing` hint names both.
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
  - **Default implementation.** `run_recovery` with a fixed script that prints the public host keys.
    Its channel does not use the VM's store: WSL2's `wsl.exe`, Proxmox's guest agent, and Lima's
    `limactl shell` over Lima's own host-local access.
  - **Clouds.** They override it and read the keys in Unverified mode. Where a provider publishes
    fingerprints (for example EC2 console output or GCE guest attributes), the platform may return
    them for the operator to compare; R7 requires only the operator's confirmation. The platform LLD
    settles the source per provider.
  - **Limits.** Unverified mode is used for nothing else after provisioning. Every `run_recovery`
    call is VM trust only.
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

`vms/contract.py` holds one table of read-only checks. Each check carries a phase tag, `base` or
`initialized`, and the table renders one structured report naming each failed item and its fix.

- **Base checks** run on core's first connection at create, and first at reinit:
  - a recognized Debian release, under ADR 0025's policy and recording;
  - systemd as PID 1;
  - the admin account and `sudo -n true`;
  - supported login shells (`sh`, `dash`, `bash` or `zsh`) for the admin and every agent;
  - Tailscale up at the recorded address;
  - python3 and agw-launch running, which the check's own request proves, plus coreutils, util-linux
    and `tar`.

  Because this is core's first operation, the release check is the first thing core does on the
  common path.

- **Initialized checks** run at the end of Phase B: the `INIT_SYSTEM_PACKAGES` tools.
- **Diagnosis.** A failure reinit can fix leaves the VM not ready and names reinit. A failure reinit
  cannot fix (an unrecognized release, no systemd) names recreation.
- **Provisioning additions.** python3 joins the shared provisioning package list
  (`PROVISIONING_PACKAGES`, `capabilities/vm_platform/cloud_init.py:16`), which every platform's
  bootstrap installs, so a new VM runs agw-launch from core's first connection. Init keeps
  installing it (`INIT_SYSTEM_PACKAGES`, `:27`), as the operator asked, but from slice 3 reinit
  starts with the base check over agw-launch, so init cannot reach a VM that lacks it.
- **Older VMs without python3.** python3 joined init only on 2026-09-14 (`2841cb9fa`), so a VM
  initialized before then lacks it unless its image shipped it; the WSL2 Debian root filesystem is
  the likely case. Such a VM gets `VMNotReady`. In PR A its hint names `agw vm reinit`, whose
  release probe and Phase A stay on the legacy stack until slice 3, so reinit still runs there
  without python3; from PR B it names `agw vm recover`, whose access repair installs python3 (D11).
  Release notes say so.

### D11. The platform surface (R1, R2, R7)

Changes to `capabilities/vm_platform/base.py`:

| Today                                         | After                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ProvisionResult.native_transport` (required) | Removed in slice 3, when Phase A and Debian attestation move onto SSH                                                                                                                                                                                                                                                                                                                                                                                            |
| `ProvisionResult.tailscale_ip: str \| None`   | `tailnet_address: str`, required. After a successful join the platform retries reading the address within its own provisioning step, so a transient failure costs neither the VM nor a second use of the key. If it still cannot read it, create fails without a second join, and the failed VM is kept for `agw vm delete`, whose logout removes the node. Today's join-once rule (`tailscale_join.py:65-71`, `vms/initializer/driver.py:386-398`) carries over |
| `native_transport(vm, ctx, config)`           | Removed in slice 4, when rejoin, rekey, logout and the native shell move                                                                                                                                                                                                                                                                                                                                                                                         |
| (none)                                        | `run_recovery(vm, ctx, script, stdin, deadline) -> RecoveryResult`: root, once, no Tailscale dependency, VM trust only                                                                                                                                                                                                                                                                                                                                           |
| (none)                                        | `read_host_keys(vm, ctx) -> HostKeyReading`, the key-reading half of the same R7 capability, not a second one: the VM's public host keys, plus any provider-side fingerprints. The default runs a fixed script through `run_recovery`; a cloud overrides it with its private access under Unverified trust                                                                                                                                                       |
| (none)                                        | `ClassVar recovery_needs_guest_sshd: bool`                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `native_shell_unavailable_hint`               | `native_shell(vm, ctx) -> int`. A platform offers a shell by overriding it. Core detects the override on the class before the operation boundary, so it refuses with `FeatureUnavailable` before credentials are resolved (R2). A platform may set guidance text for the refusal                                                                                                                                                                                 |
| `transient_route`, `probe_failure_hint`       | Private to each platform's own native code (recovery and native shell)                                                                                                                                                                                                                                                                                                                                                                                           |

**RecoveryResult.** `run_recovery` wraps the core script in a small `sh` wrapper that writes the
same nonce trailer as D4, so a recovery result is the script's status, delivery failure, or unknown.
It never relies on `limactl` or `wsl.exe` exit codes; the salvage message measured WSL passing raw
wait statuses through, which makes a signal and an exit indistinguishable. Sensitive stdin follows
D5. The capability takes no environment, directory or files.

**Per platform:**

| Platform        | Recovery channel                                                  | Needs guest sshd | Native shell | Private access after migration                                                                 |
| --------------- | ----------------------------------------------------------------- | ---------------- | ------------ | ---------------------------------------------------------------------------------------------- |
| Lima, local     | `limactl shell`                                                   | yes              | yes          | `limactl` wrappers, copied from `transports/lima.py`                                           |
| Remote Lima     | host SSH, then `limactl shell`                                    | yes              | yes          | host connection under the host policy; host-side detached create, copied from `remote_exec.py` |
| WSL2            | `wsl.exe --user root`                                             | no               | yes          | `wsl.exe` wrappers and the keepalive                                                           |
| Proxmox         | QEMU guest agent                                                  | no               | no           | QGA client as `plugins/proxmox/guest_agent.py`, replacing `plugins/proxmox/transport.py`       |
| AWS, Azure, GCP | `public_ssh.py` to the public address, inside the transient route | yes              | yes          | provisioning and lost-record key reads under Unverified trust; recovery under VM trust         |

- **Cloud public-address SSH.** The public address reaches the same sshd, with the same host key, as
  the tailnet, so cloud recovery verifies against the VM's store entry. It runs the wrapped script
  under `sudo -n /bin/sh`, with sensitive input on stdin. The script is core-owned and secret-free,
  but R4 still keeps script content out of arguments, so it travels on stdin too; the platform LLD
  settles how. agw-launch is not involved, so a VM without python3 can still be recovered.
- **Core's recovery scripts** live in `vms/recovery.py`:
  - **Rejoin** is today's `rejoin_tailscale`, run only by `agw vm recover`. Starting a VM, by
    `vm start` (`vms/manager/power.py:83-112`) or by the activation gate (`LiveVMNode.auto_start`,
    `vms/nodes.py:232-273`), starts it and waits for Tailscale to reconnect; when it does not, the
    start fails with a hint naming `agw vm recover`, as the operator ruled on 2026-10-31 and
    2026-11-01. Both starts' rejoin code goes, and with it the secret resolution that exists only
    for it: `vm start`'s conditional auth-key read, and the gate's lazy repair-secret protocol,
    `GateTarget.repair_secret_refs` and `_GateSecrets` (`orchestration/activation.py:96-202`),
    `vms/nodes.py:186-205` and `sessions/manager/_scope.py:366-433`.
  - **Rekey** is `vm rekey`, verified over SSH afterwards, where access returns.
  - **Logout** runs at delete. Its result is the script's own status, or unknown if the connection
    drops first.
  - **Access repair** restores the admin authorized key, installs python3 when it is missing, and
    reloads sshd on every platform, and prints the public host keys. Where the platform's channel
    does not need sshd, it also repairs and restarts sshd.
- **The operator entry point** is the new `agw vm recover <vm>`. It first offers host-key
  re-establishment (D9) when the VM has no store record, since every other step needs trust. It then
  runs access repair, rejoins if needed, and verifies over SSH. When the channel needs the guest's
  sshd and sshd is broken, or recovery fails, it stops and tells the operator to repair or recreate
  the machine (R7).
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
  `RunContext`.
- **Agent identity.** An agent-bound `VMAccess` connects as the agent's Linux user (ADR 0011). It
  refuses an elevated request before dispatch with a typed `PermissionDenied`. The guest's `sudo -n`
  remains a second layer, so an agent placed in a sudo group by hand still never gains admin
  authority through the transport.
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
| `TrustMissing`, `TrustMismatch`              | `StateError`         |
| `ClientUnsupported`                          | `ConfigError`        |

`ExecutionIncomplete` covers both `NotDelivered` and `Unknown` completions and names which one, so
the facts are held once.

`execution/oplog.py` writes the per-VM operation log under `~/.config/agentworks/logs/`, as today.
It records a label, the request shape, the outcome and the streams, never the envelope's environment
values, script source or stdin. Every retained stream has the request's secret values (its secret
environment values and any other values the caller marks secret) removed before it is written, as
`SSHLogger` does today (`cli/agentworks/ssh.py:113-131`); a sensitive stdin keeps the streams out of
the log entirely. The readers of the old `LOG_DIR` constant (`doctor_state.py`,
`vms/manager/lifecycle.py`, `vms/manager/_helpers.py`, `vm logs`) move to the new constant in
slice 2.

### D14. Enforcing independence (R10)

- **The package guard.** The existing fresh-interpreter guard in
  `cli/tests/execution/test_independence.py` stays until slice 8 deletes it with the legacy modules
  it guards against. It walks every `agentworks.execution` submodule with `pkgutil.walk_packages`,
  so new modules are covered automatically. `vms/contract.py` joins it in slice 3. `public_ssh.py`
  and `vms/recovery.py` join it in slice 4, once the rehome leaves
  `capabilities/vm_platform/__init__.py` importing no legacy code (today it reaches `lima.py` and
  `wsl2.py`, which do).
- **The test guard.** A conftest under `cli/tests/execution/` blocks the six legacy modules for the
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
   envelope, agw-launch with the terminal handover, results, errors and the operation log.
   Consumers: `vm exec`, `vm shell`, `agent exec`, `agent shell`, `vm verify-connection` and
   `vm port-forward`.
3. **First contact and the VM contract.**
   - Platforms report the required tailnet address.
   - Provisioning installs python3.
   - Phase A and release probing move onto `VMAccess`, in Enroll mode at create, with the base
     checks and Debian attestation; the initialized checks land too.
   - `ProvisionResult.native_transport` goes away, and `reachability` lands for create.

   Phase B stays on the legacy stack until slice 6.

4. **Recovery and the platform surface.**
   - `run_recovery` on every platform, with `public_ssh.py`.
   - Each platform's native code rehomed as private modules: Remote Lima's host connection and
     host-side detached create, the Proxmox QGA client, and the WSL2 and Lima wrappers.
   - Rejoin, rekey and logout move onto recovery, starting a VM stops rejoining on its own, and
     `native_shell` lands.
   - `vm recover` is added, and `native_transport` and its factory are deleted.
5. **Files and capability delivery.** `files.py` and the `RunContext` accessors. Consumers: harness,
   artifact, native-file and git-credential.
6. **Initialization.** VM and agent initializers, hardening, DNS, sources and the setup runners.
7. **Workspaces, sessions and backup.** Workspace backends, copy, rehome and repair; tmux, sessions
   and consoles; `detached.py` with backup.
8. **Removal.** Delete the six legacy modules, `admin_target()` and `agent_target()`, and their
   tests (slice 2 already deleted the proof modules the package does not adopt), after the incident
   inventory is re-proved and the evidence matrix is complete.

### Incident inventory (R10)

Each row is re-proved in the slice that moves the code carrying it, and the evidence is recorded in
the plan. Rows I1 to I3 come from the transport-improv migration strategy, and the rest from
scouting. The seed is not exhaustive, so each slice audits the entry points it moves (their ADRs,
linked issues and fix history) and adds rows before anything is deleted.

| Row | Behavior                                                                                                                                                                                                                                           | Re-proved in |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| I1  | Closed stdin instead of a forced TTY on Windows (ADR 0020)                                                                                                                                                                                         | 2            |
| I2  | Byte-exact stdin on Windows (text-mode rewrite)                                                                                                                                                                                                    | 2            |
| I3  | Guest mechanics tested on real targets, not Git for Windows tools                                                                                                                                                                                  | 2            |
| I4  | Sensitive input refused with a forced TTY; value-free errors                                                                                                                                                                                       | 2            |
| I5  | The `--` fence for dash-led commands, now inherent in literal argv                                                                                                                                                                                 | 2            |
| I6  | The attach notice after the terminal guard                                                                                                                                                                                                         | 2            |
| I6a | Console recovery after a killed `ssh -t`                                                                                                                                                                                                           | 7            |
| I7  | Keepalives on long-lived paths                                                                                                                                                                                                                     | 2            |
| I8  | `nohup` outside `sudo` is replaced by whole-operation elevation plus `setsid`; the output-capturing brace group is kept                                                                                                                            | 7            |
| I9  | Host-key preservation across stop and start                                                                                                                                                                                                        | 1            |
| I10 | Provisioning's first Tailscale join sends the auth key once, out of command lines, logs, exceptions and retained provisioning artifacts (`bootstrap_script.py:262-265`, `lima.py:181-183`, `tailscale_join.py:22-48`, `wsl2_bootstrap.py:100-120`) | 4            |
| I11 | A bash child of the payload reads no account startup file (measured in the integration report on #856: Debian bash sources `~/.bashrc` when `SSH_CLIENT` is set)                                                                                   | 2            |

### Release behaviors to preserve (R10)

The transport-improv migration strategy also lists release behaviors the cutover must keep. They are
carried here, each with the slice that moves the code carrying it.

| Source                                                                | Behavior to keep                                                                                                                                                                                 | Slice |
| --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| `capabilities/harness_integration/settings.py`                        | Replace, merge-overwrite, merge-preserve and skip-existing; JSON null kept as a value; whole-array replacement; TOML values                                                                      | 5     |
| `artifacts/publication.py`, `artifacts/sections.py`                   | Unowned and modified files left alone, generated-section surroundings, malformed-marker refusal, metadata and ACLs (D6 `preserve`), per-effect checkpoints, cleanup evidence after interruptions | 5     |
| `harness_setup/dispatch.py`, `artifacts/session.py`                   | Destination and home identity supplied through composition and bounded observations, not discovery commands                                                                                      | 5, 6  |
| `artifacts/native/probe.py`                                           | The login and interactive environment (D5 `PythonProgram`), collision checks and redaction                                                                                                       | 5     |
| `sessions/manager/_create_roll.py`, `db/models.py`                    | Durable session UUID and per-launch run ID, private artifact ownership, unknown-runtime cleanup evidence                                                                                         | 7     |
| `sessions/manager/_lifecycle.py`                                      | Running-restart confirmation and selected-session identity checks                                                                                                                                | 7     |
| `vms/initializer/driver.py`, `capabilities/vm_platform/cloud_init.py` | python3's availability, now guaranteed from provisioning (D10)                                                                                                                                   | 3     |

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

| ADR                               | Change                                                                                                                              | Slice |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----- |
| 0013 (secret injection)           | Secret environment travels in the request on stdin, not as `-o SetEnv` arguments                                                    | 2     |
| 0014 (`AcceptEnv` wildcard)       | The transport no longer sends environment through SSH; slice 8 decides whether the fragment still has a user                        | 2, 8  |
| 0017 (console pane environment)   | Superseded: console panes take their environment from the terminal handover (D8), retiring `tmux -e` and the `--preserve-env` carry | 7     |
| 0019 (command plans)              | Its `admin_target`/`agent_target` description moves to the new accessors                                                            | 5     |
| 0025 (one current Debian release) | Core attests the release over SSH as the first base check                                                                           | 3     |

ADR 0003 needs no change: its statement that access after bootstrap is SSH over Tailscale becomes
exactly true.

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
| The trailer with a background child holding stderr                                                                            | Conformance vector, slice 2                                 |
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
   starts the VM, waits for Tailscale to reconnect, and fails with a hint naming `agw vm recover`
   when it does not (D11).
4. **Local Lima on Windows** is not a supported pairing (evidence matrix). The operator first added
   it, then reversed that the same day, and issue #861 refuses it in Lima's readiness check.
5. **The launcher's name** is `agw-launch`, the Agentworks launcher.
6. **Three PRs** instead of eight (migration).
7. **Delivery.** Every request travels on stdin, agw-launch is written in Python, python3 is a
   provisioning requirement, and terminals use the two-connection handover.
8. **Stage 0** reads agw-launch from stdin, so the command line stays one short constant line.
9. **Script-bundle capabilities** are a later effort, outside this one. This design supplies the
   pieces they need: `Script` and `PythonProgram` requests that take their inputs as data, the file
   operations, the contract's check table, and the identities and elevation of `VMAccess`.

## Awaiting the operator

1. **Provisioning's auth key.** R1 leaves the first Tailscale join to each platform's provisioning,
   and R4's secrecy rule covers only the transport and recovery input. The lead proposes a
   one-sentence R1 amendment: provisioning's first join sends the auth key once and keeps it out of
   retained provisioning artifacts, command lines, logs and exceptions. Incident row I10 and slice
   4's test carry it either way.
2. **python3 at init.** From slice 3, init's python3 install converges nothing, since init cannot
   reach a VM without python3 and `agw vm recover` covers older ones. It stays, as the operator
   asked, unless the operator drops it.

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
| R1          | D10, D11                           |
| R2          | D2, D8, D11                        |
| R3          | D3, D4, D5                         |
| R4          | D2, D3, D4, D5, D8                 |
| R5          | D6                                 |
| R6          | D7                                 |
| R7          | D11                                |
| R8          | D1, D2, D9, D11                    |
| R9          | D12                                |
| R10         | D14, Migration, incident inventory |
| R11         | Evidence matrix, plan              |
