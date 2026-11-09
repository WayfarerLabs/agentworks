# Transport Take 2: Execution Core Low-Level Design (Slice 2)

- Status: Implemented in PR A (#863); live evidence in progress.
- Date: 2026-11-01
- Architecture: [hla.md](./hla.md) D1 to D5, D8, D12 to D14; requirements: R2 to R5, R8 to R11
- Plan: [plan.md](./plan.md), slice 2, delivered in PR A with slice 1
- Source baseline: `7b2209c60` (slice 1 merged into the PR A branch)

## Scope

This LLD settles the details the HLA leaves open for the execution core and the operator exec
surface:

- client resolution, the VM policy, and how the client spawns `ssh`;
- the stdin framing, stage 0, the envelope and the start and completion markers;
- agw-launch's steps and the terminal handover;
- request and result types and error mapping;
- the migrated commands, the guards and the proof modules' adoption and deletion.

Where the HLA or plan was silent, the narrowest choice is made here. The HLA and plan carry the
choices that change them.

**Left to the first consumer.** HLA D5 lands each vocabulary item with its first consumer, and no
slice 2 consumer uses these, so they are specified only where a later slice's contract depends on
them:

- `elevated` (the `sudo -n` prefix) with `PermissionDenied`, and the base checks that use them
  (slice 3, [contract-lld.md](./contract-lld.md)). A sudoers `use_pty` default has no effect when
  sudo has no terminal, and every elevated request is `ssh -T`, so the stream check (below) is not
  turned against a pty;
- `reachability` (slice 3, polling a fixed probe) and the `observation` option with the retry it
  permits (slice 7, with `detached.observe`);
- the operation log: `oplog.format_record` and `SSHLogger.record` (slice 3, with create's Phase A,
  its first caller). `ssh.LOG_DIR`, its readers and `SSHLogger` stay until slice 8;
- `FeatureUnavailable` (slice 4, with `vm shell --platform`);
- `PythonProgram` and `Script.interactive` (a later slice). Measured for that slice: `dash -i FILE`
  prints a prompt to stderr per line it reads from the file, so an interactive script needs the
  HLA's startup shell that execs the interpreter, not `-i FILE`;
- stdin and output sinks beyond the operator's stdin for `stream` (slice 5 and later), the Enroll
  trust mode (slice 3, `enrollment_access`), `attach`'s `clear_screen_on_exit` (slice 7), and three
  pieces for slice 4's root channel ([platform-lld.md](./platform-lld.md)): the host argv,
  `build_host_argv`, for Remote Lima's host connection; `PipeSession(nonce=None)`, the unfiltered
  pass-through that `root_script.run_process` uses; and `spawn`'s `ConnectivityError` naming a
  program other than the resolved client that cannot start.

## Module layout

| Module                                           | Contents                                                                                                          |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `agentworks/execution/client.py`                 | `resolve_ssh`, `SSHClient`: resolution, banner and version check                                                  |
| `agentworks/execution/ssh_process.py`            | `VMPolicy`, `build_argv`, the stage 0 string, the client log and `spawn`, the one place a client is started       |
| `agentworks/execution/pump.py`                   | `Deadline` and `PipeSession`, the argv-agnostic pump                                                              |
| `agentworks/execution/envelope.py`               | Frames, the JSON envelope, markers, the stream filters, the classifier and `launcher_source()`                    |
| `agentworks/execution/agw_launch.py`             | The guest program. Stage 0 execs it; the client imports only its two shared definitions                           |
| `agentworks/execution/requests.py`, `results.py` | The slice 2 vocabulary, with `Shell` and `AccountShell` (below)                                                   |
| `agentworks/execution/access.py`                 | `VMAccess`, `Admin`, `Agent`, `vm_access`                                                                         |
| `agentworks/execution/attach.py`                 | The two-connection attach under `guarded_terminal`                                                                |
| `agentworks/execution/forwarding.py`             | The blocking forward                                                                                              |
| `agentworks/execution/errors.py`                 | Adds `ExecutionIncomplete`, `CommandFailed`, `VMNotReady`, `TrustMismatch`, `ClientUnsupported`, `RequestRefused` |
| `agentworks/execution/root_script.py` (slice 4)  | `RawRun`, `RootScriptResult`, `run`, `run_process`, `read_host_keys` ([platform-lld.md](./platform-lld.md))       |
| `agentworks/execution/trust.py` (slice 4)        | Gains `fingerprint(key)`, OpenSSH's `SHA256:` form                                                                |
| `agentworks/vms/manager/port_forward.py`         | `port_forward_vm`, moved out of `tailscale.py` so the file can pass the static import check                       |
| `agentworks/vms/manager/platform_shell.py`       | Held `vm shell --platform`'s legacy part until slice 4 deleted it for `native_shell`                              |
| `tests/execution/conftest.py`                    | The legacy-module blocker                                                                                         |
| `tests/execution/conformance/`                   | The vectors and their lanes                                                                                       |
| `tests/vm_access_support.py`                     | `RecordingVMAccess`                                                                                               |

`trust.py` makes `_literal_path` (`trust.py:122`) public as `literal_path(path, what)`, so the VM
policy quotes the identity file by the same rules as the store path and its refusal names what it
was refusing.

## The proof modules

Citations of the proof in this LLD (`carrier.py`, `connection.py`, `_io.py` and the rest) are at
`fd1256b1f`, the last commit that has it.

| Proof module                                                    | Fate                                                                                                                                                                                                                                                                                                                                                                                    |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `carrier.py`                                                    | Deleted. `Deadline` (`:20`) moves into `pump.py`; the report types give way to `results.py`                                                                                                                                                                                                                                                                                             |
| `preparation.py`                                                | Deleted. The base64 line envelope and its 256 KiB cap are superseded by `envelope.py`; the `Command` and `Script` shapes carry over to `requests.py`. Its reserved-environment list existed for the Bash bootstrap and does not carry over                                                                                                                                              |
| `bootstrap.py`                                                  | Deleted, replaced by `agw_launch.py`. Its locale save, force and restore (`:31-33`, `:135`) was for Bash's own parsing and has no counterpart in a Python launcher                                                                                                                                                                                                                      |
| `carriers/ssh/client.py`                                        | Adopted into `client.py`: the banner regex (`:27`) and the 8.5 floor (`:61`). The probe on every execute becomes once per process; `SSHCarrier` is deleted                                                                                                                                                                                                                              |
| `carriers/ssh/connection.py`                                    | Adopted into `ssh_process.py`: the option list (`:109-153`, cut down below) and the host and user checks (`:51-58`). Its trust options are `trust.ssh_options`, so only one copy survives                                                                                                                                                                                               |
| `carriers/ssh/_io.py`                                           | Adopted: the single-threaded non-blocking pump, kill and reap and cleanup on `BaseException` (`:191`) into `pump.py`, the Windows environment strip (`:104-114`) into `ssh_process.spawn`                                                                                                                                                                                               |
| `carriers/proxmox.py`, `carriers/_proxmox_http.py`              | Deleted (R10). Slice 4 writes the QGA client fresh                                                                                                                                                                                                                                                                                                                                      |
| `tests/execution/` proof tests, `conformance.py`, their READMEs | Deleted with their modules, except the process-level cases of `carriers/ssh/test_client.py` (duplex pressure `:146`, EOF on empty input `:164`, capture limits `:174`, short and failed writes `:219` and `:234`, deadline reap `:272`, interruption `:430`, local signals `:449`, descendant handles `:459` and `:507`, the Windows environment `:543`), which are adapted to the pump |

`test_independence.py` stays (HLA D14) with its `prepare()` section replaced: after importing every
submodule it asserts that none of the legacy modules is in `sys.modules`.

## Client resolution

`client.resolve_ssh() -> SSHClient` is cached for the process and returns the absolute path and the
parsed `(major, minor)`. Every spawn in the package takes the `SSHClient`, so none of it launches a
bare name; the legacy transports find `ssh` and `scp` along `PATH` through
`programs.resolve_program` (below) until slice 8 deletes them.

- **POSIX.** The first `ssh` along `PATH`, skipping empty and relative elements, which is
  `programs.find_program`. Only that candidate is judged.
- **Windows.** `PATH` is never searched. The candidates, in order, are
  `%SystemRoot%\Sysnative\OpenSSH\ssh.exe`, `%SystemRoot%\System32\OpenSSH\ssh.exe`,
  `%ProgramW6432%\OpenSSH\ssh.exe` and `%ProgramFiles%\OpenSSH\ssh.exe`. Each is skipped when its
  variable is unset or its file is missing. `Sysnative` and `ProgramW6432` exist only for a 32-bit
  Python on 64-bit Windows, where `System32` and `ProgramFiles` are redirected away from the real
  client. Every candidate is judged and the first acceptable one wins, so a Windows 10 workstation
  with the inbox 8.1 client and the MSI install resolves to the MSI build.
- **Judging.** `ssh -V` runs once per candidate (`stdin=DEVNULL`, 10 s). The banner on stderr must
  match the proof's regex, with the `for_Windows_` form required on Windows and refused elsewhere,
  and the version must be at least 8.5.
- **Refusal.** `ClientUnsupported` lists each path tried with its reason (missing, wrong build, too
  old) and how to install a supported client, naming the Win32-OpenSSH MSI and the inbox 8.1 client
  on Windows 10. A `Popen` failure on the resolved path raises it too.
- **Before credentials.** Each migrated command calls `resolve_ssh()` first, before any database
  read or guard, so an unsupported client refuses before anything else. The existing boundaries
  (`require_vm_ssh_boundary`, `gated_vm_boundary`) stay as they are.
- **Doctor.** `_check_required_tools` (`doctor.py:526`) keeps its PATH presence rows for `ssh`,
  `scp` and `tailscale`, because the legacy paths still find `ssh` and `scp` along `PATH`, and adds
  a row with the resolver's verdict: path and version, or the refusal. Slice 7 or 8 removes the
  `ssh` and `scp` rows with the last legacy user.

Every other workstation program `agw` starts by name goes through `programs.resolve_program(name)`,
the same walk over absolute `PATH` elements (`name.exe` on Windows), which raises
`FileNotFoundError` as a missing program would: `tailscale` (the workstation preflight, the
reachability ping, doctor), PowerShell (WSL2's helper and the completion profile query), `op` (the
1Password backend), `git` (package sources), `taskkill`, VS Code's `code`, the `EDITOR` of
`config edit` and `resource edit`, and the legacy paths' `ssh` and `scp`. `ssh_process.spawn`
resolves a bare first argument the same way, which covers local Lima's `limactl`. A name with a
directory part (`EDITOR=/usr/bin/vim`) is already a path and is returned unchanged. Presence checks
(the 1Password readiness check, Lima's `limactl` and `ssh` checks, doctor's shell check) use
`programs.is_installed`, the same lookup, so a program counts as installed exactly when it would
start. Windows looks a bare name up in the current directory before `PATH`, so a bare name there
would run whatever the operator's working directory holds. Only `.exe` is appended on Windows, so VS
Code's `code.cmd` is deliberately not found (a `PATHEXT` lookup would send its arguments through
`cmd.exe`), and `workspace create --open-vscode` warns there instead of opening the editor.

## Requests and results

The slice 2 subset of D5. Strings are `str`. `Shell` is a `StrEnum` of `sh`, `bash` and
`user_default`. `AccountShell` is `Literal["sh", "dash", "bash", "zsh"]`, the template field type.

| Type         | Fields                                                               | First consumer                  |
| ------------ | -------------------------------------------------------------------- | ------------------------------- |
| `Command`    | `argv: tuple[str, ...]`                                              | `verify-connection`             |
| `Script`     | `source: str`, `shell: Shell`, `login: bool = False`                 | `vm exec`, `agent exec` (login) |
| `LoginShell` | none                                                                 | `vm shell`, `agent shell`       |
| Common       | `env: Mapping[str, str] = {}`, `cwd: str \| None`                    | all                             |
| Bounded      | `deadline: float \| None` (seconds), through `BoundedRequestOptions` | `Command`, `Script`             |

`LoginShell` is the identity's default shell as an interactive login shell. A `Script` with an empty
source would run `shell -l -i FILE` on an empty file and exit, and a name that says "shell session"
is more truthful than that. It is valid only for `attach`, and the types say so: `run` and `stream`
take `RunRequest` (`Command | Script`), `attach` takes `LoginShell`, and only `Command` and `Script`
carry a `deadline` (`BoundedRequestOptions`), so neither misuse needs a runtime refusal.

`Result` holds `completion` and `stdout` and `stderr` as bytes (both `repr=False`).
`Result.require_completed(vm_name) -> int` returns the status of a `Completed` and otherwise raises
by the error table below; `run(check=True)` is that plus `CommandFailed` for a non-zero status.

```text
Completed(status)
NotDelivered(reason)   PYTHON_MISSING | STDLIB_MISSING | LAUNCHER_FAILED | REFUSED(code)
Unknown(reason)        CONNECTION | HOST_KEY | AUTH_FAILED | TRUNCATED | DEADLINE | OUTPUT_LIMIT
```

- **`access.run(request, *, check=False) -> Result`.** The client closes ssh's stdin right after the
  request. Each stream is read up to one pump constant, 1 MiB; more is `Unknown(OUTPUT_LIMIT)`.
- **`access.stream(request, *, stdin, stdout, stderr) -> int`** and
  **`access.attach(request) -> int`** return the guest's status. They raise rather than return
  `NotDelivered` or `Unknown`, because their consumers can only report it. Neither takes the other's
  inputs: no operation combines input with a terminal, so incident row I4's refusal is structural.
- **Refusals.** `stream` maps `NotDelivered(REFUSED)` as tty mode does: `exec ENOENT` is status 127,
  any other `exec` errno is 126, and `cwd` and `handover` are 125, with one line naming the refusal
  on the stderr it was given. So `vm exec VM nosuchcmd` still exits 127. `run` returns the outcome
  as `NotDelivered(REFUSED)`, `require_completed` raises `RequestRefused(StateError)` for it, and
  `attach` raises it when the parking connection refuses, before the terminal is touched. One
  `refusal_status(code)` in `agw_launch.py` is the table for the guest and the client alike.
- **Validation** lives in the one function that turns a request into bytes (`envelope.encode`).
  Boundary: environment, arguments, working directory and script text come from operator-authored
  config and from secret backends. It refuses NUL anywhere, an environment name outside
  `[A-Za-z_][A-Za-z0-9_]*`, a relative `cwd`, an empty argv or empty `argv[0]`, a lone surrogate
  outside `U+DC80` to `U+DCFF`, and any argv element or `K=V` environment string over 131,071 bytes
  once encoded (below).

## The access object

`execution.vm_access(vm, config, identity) -> VMAccess`, with `identity` either `Admin()` or
`Agent(name, linux_user)`. Construction copies the VM name, its tailnet address, the SSH user
(`admin_username` or the agent's `linux_user`) and the identity file path, and nothing else (R9): no
key is read, no trust file is opened and nothing is spawned. It also holds the identity, which
refuses an elevated request from an agent before dispatch (`PermissionDenied`), and an optional
`log` callable that `run` calls with each request and its result. Its trust is
`TrustBinding(TrustMode.VM, vm.name)`, except the access that `execution.enrollment_access` builds
for create's first contact, which is Enroll. The operations are `run`, `stream`, `attach` and
`forward`.

Each operation runs in this order, so everything before the spawn raises with nothing dispatched:
`resolve_ssh()` (`ClientUnsupported`), encode the request (`ValidationError`), build the options
(`TrustMissing`), spawn, pump, classify. A VM with no tailnet address is a `StateError` at that
point, the first use of a persisted value.

## Policies and the client argv

`build_argv(client, policy, destination, mode, remote_command, forwards=())` returns the argument
list. `VMPolicy` holds the identity file and a `TrustBinding`; `Destination` holds host, user and a
port (22 except in tests, which run a local `sshd`). Both are validated at their boundary, the
database, with the proof's host and user patterns (`connection.py:51-58`).

**VM policy options.** `-F none` first, so no operator or system configuration applies, then these
as `-o` (the proof's list at `connection.py:109-153`, cut to what `-F none` leaves to say):

| Group    | Options                                                                                                                                                                                                                       |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identity | `IdentityFile="<ssh_private_key>"` and `IdentitiesOnly=yes` (I13). `CertificateFile="<ssh_private_key>/disabled-certificate"`, the 8.5 workaround (`:117-121`). No `IdentityAgent`: the default agent serves an encrypted key |
| Auth     | `BatchMode=yes`, `PreferredAuthentications=publickey`, `SecurityKeyProvider=none`                                                                                                                                             |
| Trust    | `trust.ssh_options(TrustBinding(VM, vm.name))`, read at spawn, so a missing record is `TrustMissing` and nothing spawns                                                                                                       |
| Liveness | `ConnectTimeout=30`, `ServerAliveInterval=15`, `ServerAliveCountMax=4` (I7; the constants are `ssh.py:288` and `:296-297`)                                                                                                    |

- **What `-F none` already settles.** Password and keyboard-interactive authentication, multiplexing
  (I12), proxies, agent and X11 forwarding, host-key DNS, canonicalization, `KnownHostsCommand`,
  `PermitLocalCommand`, `ClearAllForwardings`, `PKCS11Provider`, `HostbasedAuthentication`, `Tunnel`
  and `ConnectionAttempts` are all at their defaults with no configuration read, and no environment
  variable changes them. The proof pinned each anyway; these pins are not carried over. The escape
  character is inactive under `-T`, and the terminal half keeps ssh's default `~`, as
  `transports/ssh.py:380` does today. The CI `ssh -G` test and the live 8.5 and Windows cells guard
  those defaults.
- **Dropped from the proof:** the pins above, `EscapeChar=none`, its own `StrictHostKeyChecking`,
  `GlobalKnownHostsFile=none`, `UpdateHostKeys`, `CheckHostIP`, `UserKnownHostsFile` and
  `HostKeyAlias` (now `trust.py`'s one copy), `IdentityAgent`, `RevokedHostKeys` (no consumer) and
  the `known_hosts_file` plumbing.
- `GlobalKnownHostsFile` stays the null device `trust.ssh_options` already pins, not `none`, which
  every version accepts, so the legacy paths and the VM policy share one function.
- The liveness options apply to every connection, not only streaming, attach and forward. A dead
  peer then ends a `run` that has no deadline within a minute, instead of hanging, and one option
  list has no per-mode branches.
- **Environment.** Nothing is sent through ssh. Under `-F none` no `SendEnv` applies, and the
  request's environment is in the envelope (D3).

**Argv shapes.** `<stage0>` is `/usr/bin/python3 -I -S -c '<program>'` (below).

| Mode                              | Argv                                                                                                         |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `run`, `stream`, the parking half | `ssh -F none -T -E <client log> <options> -l <user> -- <host> <stage0>`                                      |
| Terminal half of an attach        | `ssh -F none -t <options> -l <user> -- <host> <stage0> <socket path>`                                        |
| Forward                           | `ssh -F none -N -n [-E <client log> or -v] <options> -o ExitOnForwardFailure=yes -L ... -l <user> -- <host>` |

- **No `-n` on request-bearing connections.** `-n` would replace the request's stdin with the null
  device. ADR 0020's measured alternative, a stdin pipe the client closes, was proven equivalent
  there (PR #736, 80 of 80), so "closed stdin" is the client closing its pipe after the request and
  the caller's input. A forward carries no request and uses `-n`.
- **`-T` and `-t`.** `-T` on every pipe mode (I14); it also beats an operator's `RequestTTY force`,
  and no configuration is read anyway. The terminal half uses `-t`, as `transports/ssh.py:380` does.
  `-tt` would force a pty when the operator's stdin is not a terminal, which breaks
  `echo ls | agw vm shell`. tty mode accepts a non-pty.
- **Forwarded addresses.** `-L <bind>:<local>:localhost:<remote>` as `tailscale.py` builds it today.
- **The client log.** `-E` appends ssh's own diagnostics, never the remote's output, to a file from
  `tempfile.mkstemp()` in the system temporary directory (0600), whose descriptor is closed before
  the spawn (a Windows unlink fails while a handle is open), and removed in a `finally`. The value
  goes to ssh verbatim, so it needs no quoting. stderr then carries what the guest wrote, apart from
  a login banner sshd may send before the marker, which the filter drops; the filters read nothing
  from OpenSSH, and the client's diagnostics can be shown without the risk that pre-marker stderr
  quotes request bytes. The terminal half keeps ssh's diagnostics on the operator's terminal, as
  today. A live cell confirms that `Host key verification failed` and the authentication and
  connection errors land in the file on 8.5, 9.x and Win32-OpenSSH. The client reads this log, to
  type `Host key verification failed` and `Permission denied (publickey` and to show its last 20
  lines in errors.

`build_argv` takes the client log path and a `verbose` flag. `verbose` replaces `-E` with `-v` and
is accepted only for a forward, since in a pipe mode ssh's debug lines would reach the guest's
stderr after the marker; the terminal half takes neither.

**Spawning.** One `spawn(argv, ...)` in `ssh_process.py` runs
`subprocess.Popen(argv, stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0)` (the forward passes no
pipes) with the proof's Windows environment strip (`_io.py:104-114`, at `fd1256b1f`), and maps a
failure of the resolved client to start to `ClientUnsupported`. The pump lives in `pump.py`. The
pump is the proof's single-threaded loop (`_io.py:32-205`): non-blocking pipes (supported on Windows
pipes from Python 3.12), 64 KiB chunks, a 10 ms sleep when nothing moved, the deadline, then kill
and a 0.5 s reap, and cleanup on `BaseException`. It is argv-agnostic, which is what lets the
conformance suite run it against a local shell.

- **Who uses it.** `run`, `stream` and the parking connection of an attach reuse the one pump. The
  terminal half (inherited stdio, `subprocess` wait) and the forward (no pipes) do not. The parking
  connection sets `hold_stdin_open`, so the pump sends the request and leaves the pipe open: the
  parked launcher reads end of file as the client giving up.
- **Deadline and output limit.** Either stops the pump and kills the client at once, then reaps for
  0.5 s; only a natural end waits for the client to exit.
- **Operator stdin for `stream`.** The caller passes `sys.stdin.buffer.raw`. A daemon thread reads
  `stdin.read(65536)` into a queue and the pump drains one chunk per pass into ssh's stdin, closing
  it at end of file. A thread is needed because Windows cannot poll a console or pipe handle, and
  one mechanism serves every platform. It reads the raw stream because a daemon thread blocked in
  `BufferedReader.read1` aborts the interpreter at exit (`_enter_buffered_busy`, status 134,
  reproduced on Python 3.11; the CLI's floor is 3.12, where the subprocess test covers it). A caller
  with no stdin passes `None` and the pipe closes right after the request. The thread outlives the
  call blocked in a read; `stream` is the last thing a command does. A stdout consumer that leaves
  (`| head`, quitting `less`) gets the semantics ssh gave it when ssh held the operator's stdout.
  The client discards the rest of that stream and closes its read end of it once the guest writes to
  it again, so ssh sees `EPIPE` on the write after that one, and keeps pumping stderr until the
  channel ends: a writer that keeps writing takes `SIGPIPE` and the channel closes, while a quiet
  payload runs to its end. Once ssh's write to its stdout fails, the OpenSSH client writes none of
  the stderr it receives after that, so a quiet payload's end marker must not meet a closed read
  end, and kept open it does not. A guest that writes once more after its consumer left and then
  goes quiet has closed the read end, so its end marker is ssh's failed write: the trailer survives
  only when it reaches ssh in the same batch, and otherwise the result is `Unknown(CONSUMER_LEFT)`.
  A stderr consumer that leaves, with no trailer yet, ends the request once held stdout is
  delivered, and ending the session kills the client after its one-second natural-exit wait, which
  ends the guest's writer: ssh only discards stderr after a failed write and keeps the channel up,
  and the status travels on stderr, so nothing more could be learned. With the trailer already in
  hand, stdout is pumped to its end marker and the result is `Completed(N)`. When stdout was
  abandoned and the stderr trailer still arrives, the result is the guest's own `Completed(N)`;
  otherwise it is `Unknown(CONSUMER_LEFT)`, which `stream` alone reports as 141, the status of a
  writer killed by `SIGPIPE`. `stream_to_operator` flushes both streams once as it returns, and that
  flush is how a departure it did not see earlier is detected: a stream whose consumer has gone is
  pointed at the null device (`output.tolerating_dead_sink`), so interpreter shutdown cannot fail on
  the bytes left in its buffer. That flush tries both streams and never replaces a failure already
  in flight.
- **Binary stdio.** Callers pass `sys.stdout.buffer` and `sys.stderr.buffer` (the text layer's
  `newline="\n"` at `cli/_entry.py:38` does not apply), after flushing the text layer (I2).
  `stream_to_operator(access, request)` in `access.py` is that wiring, flush, raw stdin and binary
  buffers, so the commands do not repeat it.
- **A sink that would block.** Only `BrokenPipeError`, `ConnectionResetError` and `EIO` (a terminal
  that hung up) from a sink mean the consumer has gone, plus `OSError(EINVAL)` on Windows, where
  CPython reports a pipe whose reader closed that way; `output.consumer_left` is the one copy of
  that rule, shared with the output handlers. A sink that raises `BlockingIOError`, because the
  operator's own stdout is a non-blocking pipe, keeps its unsent remainder and is retried, so output
  is never dropped while the command reports success. Any other `OSError` from a sink (`ENOSPC`)
  propagates.

## The wire

The client writes three things to ssh's stdin, in order: the launcher source frame, the envelope
frame, then the caller's input, untouched, until end of file. A frame is **ten ASCII decimal digits,
zero padded, then that many bytes**.

**Stage 0**, the `-c` program, 199 characters:

```text
import os,sys,socket as S;a=sys.argv[1:];s=a and S.socket(1);a and s.connect(a[0]);f=a and s.fileno()or 0;r=lambda n:n and(lambda c:c+r(n-len(c)))(os.read(f,n)or 1/0)or bytes();exec(g:=r(int(r(10))))
```

- **What it does.** `f` is stdin, or the connected socket on the handover form (`a` is the socket
  path). `r(n)` reads exactly `n` bytes with `os.read`, so stdin is left at the next byte and no
  buffered reader touches it. `g` is the launcher source, which `exec` then runs and a parking
  launcher serves again. End of file raises, so any short or empty input is a traceback and exit 1
  before a marker.
- **Quoting.** The program has no `'`, which would end the account shell's quoting, no `!`, which
  `csh` expands even inside single quotes, no `"`, no backslash and no newline, so Windows argument
  quoting has nothing to escape. Measured under `sh`, `dash`, `bash` and `zsh` with Python 3.11, in
  pipe and socket mode; the Windows round trip is a live cell.
- **Recursion.** `r` recurses once per partial read. That is fine for the launcher (tens of
  kilobytes, a few reads); agw-launch reads the envelope with its own loop, and receives `f` and `g`
  as `main(f, g)`.
- **Python.** Measured with 3.11 (Debian 12); Debian 13's 3.13 is a live cell. The launcher uses
  plain syntax and only the standard library: `ast.parse(..., feature_version=(3, 11))` in a test,
  and the L lane also runs under a 3.11 interpreter in CI (below).

**Envelope.** One JSON object, `json.dumps(obj, ensure_ascii=True, separators=(",", ":"))`, so the
wire is pure ASCII whatever the workstation's locale. No version field (HLA D3).

| Field      | Type                         | Meaning                                                                             |
| ---------- | ---------------------------- | ----------------------------------------------------------------------------------- |
| `nonce`    | 32 lowercase hex             | `secrets.token_hex(16)` per request, never in the payload's argv or environment     |
| `mode`     | `run`, `tty` or `park`       | Pipe mode with markers; terminal exec; park for a handover                          |
| `kind`     | `argv`, `script` or `shell`  | `Command`, `Script`, `LoginShell`                                                   |
| `argv`     | list of str                  | `argv` kind, non-empty                                                              |
| `shell`    | `sh`, `bash`, `user_default` | `script` kind                                                                       |
| `source`   | str                          | `script` kind                                                                       |
| `login`    | bool                         | `script` kind                                                                       |
| `cwd`      | str or null                  | Absolute; null means the home directory                                             |
| `env`      | object of str to str         | The request's environment                                                           |
| `term`     | str or null                  | `tty` and `park` modes: the operator's `TERM`, `xterm-256color` when unset or empty |
| `handover` | 16 lowercase hex             | `park` mode: the client's `secrets.token_hex(8)`, naming the socket directory       |

The client validates; agw-launch does not re-validate the schema. A malformed envelope is an
uncaught exception before the start marker, and one build ships both halves.

- **Bytes in JSON.** Values are `str`. Text that is not valid UTF-8 travels as the code points
  `os.fsdecode` produces (`U+DC80` to `U+DCFF`), which JSON carries as `\udcXX`. The guest encodes
  every string with `utf-8` and `surrogateescape`, so the guest sees the original bytes: `%`,
  quotes, `${...}`, CR, LF and every byte but NUL exactly.
- **Kernel limits.** Linux caps each argv element and each `K=V` environment string at
  `MAX_ARG_STRLEN`, 131,072 bytes counting the NUL (measured: 131,071 runs, 131,072 fails).
  `envelope.encode` refuses a larger element or string with a `ValidationError` naming which. A
  total over `ARG_MAX` stays `refused exec E2BIG`. Script source is unaffected, since it goes to a
  memfd. Nothing else bounds a request but memory.
- **Markers.** Every marker is `0x1E`, the nonce, a space, a word, a newline: `start`, `end`,
  `exit N`, `refused CODE`, `parked`. Payload output is the bytes between the start marker and the
  closing one, so it needs no trailing newline. The lead byte is one no text emits, so the filters'
  hold-back (below) almost never delays a real byte; a lead hex digit would hold back one in sixteen
  output endings.

## agw-launch

The guest program, run by stage 0 under `python3 -I -S` from `launcher_source()` (in `envelope.py`),
which reads `agw_launch.py` from the installed package. The file has no import-time effect: it
defines `main(f, g)` and ends with
`if __name__ == "__main__": main(globals()["f"], globals()["g"])`, so the package guard can import
it and linters see no undefined names. Stage 0's top-level names (`os`, `sys`, `S`, `a`, `s`, `f`,
`r`, `g`) are the same globals agw-launch's `exec` runs in, so the file defines none of them at
module level. Its only module-level definitions are `STDLIB_MISSING_STATUS`, `refusal_status` and
`main`; every other import sits in one `try` inside `main()`, because the client imports this module
on Windows, where `pwd` does not exist. An `ImportError` there exits with `STDLIB_MISSING_STATUS`,
69, before any marker. That is the `python3-minimal` case: Debian's `libpython3.11-minimal` ships
`os`, `sys` and `socket`, which stage 0 needs, but not `json`, which is in `libpython3.11-stdlib`.
The client's classifier imports that constant and `refusal_status` from this module, so each is
defined once. Steps, mapped to HLA D4:

1. **Read.** The envelope, with its own exact-read loop, and the passwd entry. Every failure before
   the start marker is an uncaught exception (exit 1) or the status above; neither is 127 or 255.
   The one ordering rule: argv and environment are encoded to bytes before the start marker, so a
   string that cannot be encoded fails before it.
2. **Check the streams** (`run` and `park` modes). Descriptors 1 and 2 must each be a FIFO or a
   socket, and distinct (`st_dev` and `st_ino`). A startup file that ran `exec >/dev/null` or
   `exec 2>&1` fails this and exits 1.
3. **Start marker** to descriptor 1, then 2.
4. **Reset.** Environment rebuilt in this order: `HOME`, `USER`, `LOGNAME`, `SHELL` from the passwd
   entry of `os.getuid()` (looked up in step 1, before the marker: a missing entry is an exception,
   since sshd cannot authenticate a uid without one; an empty shell is `/bin/sh`), then
   `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/games:/usr/games`,
   `LANG=C.UTF-8`, `SHLVL=1`, `TERM` and `SSH_TTY` (the terminal name) in `tty` mode when stdin is a
   terminal, `XDG_RUNTIME_DIR=/run/user/<uid>` when that directory exists and the uid owns it, and
   last the request's `env`, which may override anything. Nothing is copied from the inherited
   environment. The working directory is set with `os.chdir` in agw-launch itself, to `cwd` or home
   (`refused cwd` on failure, which `Popen` could not tell from a missing `argv[0]`), the umask is
   `022`, every signal an earlier process ignored goes back to its default, the signal mask is
   cleared, and the payload gets no descriptor above 2 other than a script's.
   - **Choices the HLA leaves open.** `PATH` is one fixed value for every identity, a superset of
     Debian's login paths so `ip` resolves for an admin and later for root. The locale is
     `LANG=C.UTF-8` and no `LC_*`: sessions used to inherit the workstation's `LANG` through
     `SendEnv`, which `-F none` stops sending, and a fixed value makes output reproducible.
5. **Run the payload as a child** (`subprocess.Popen` with bytes arguments and environment,
   `restore_signals=True`, `close_fds=True`, `umask=0o022`), not a shell:
   - `argv`: the arguments themselves; `argv[0]` is searched in the rebuilt `PATH`. A failure to
     execute is `refused exec <ERRNO>`, which covers a missing or non-executable interpreter too.
   - `script`: the source goes to an anonymous file from `os.memfd_create`, flagged
     `MFD_NOEXEC_SEAL` (`0x8`, kernel 6.3 and later) with a retry without it on `EINVAL`, passed in
     `pass_fds` and named `/dev/fd/N`, so script text is in no argv and the interpreter's stdin
     stays the caller's. The interpreter is `/bin/sh`, `/bin/bash` or the passwd shell, run as
     `[interp] [-l] /dev/fd/N`. The memfd is created and filled before the start marker, so a
     failure there (`EMFILE`, say) is a launcher failure, not an outcome that may have run. The
     interpreter's own children inherit that descriptor, the only one they inherit; it holds the
     script they came from. The payload's stdin is agw-launch's, positioned at the caller's first
     byte. There is no copy loop.
6. **Complete.** After `wait`, set descriptors 1 and 2 back to blocking, since the payload shares
   their open file descriptions and may have made them non-blocking, then write `<M>end` to
   descriptor 1 then `<M>exit N` to descriptor 2, with `N` the exit status, or 128 plus the signal
   for a payload killed by one. A refusal writes `end` then `refused CODE` instead. Codes are `cwd`,
   `exec <ERRNO>` and, for a parked launcher, `handover`.

**`tty` mode** (the terminal half of an attach) runs `LoginShell` only, since a script's memfd could
not survive the descriptor close. It runs steps 1, 4 and 5 with no check, no markers and no child:
it resets every disposition that is `SIG_IGN` to `SIG_DFL`, including Python's own `SIGPIPE` and
`SIGXFSZ` ignores (nothing else restores them before an exec, unlike `Popen`), clears the signal
mask, closes descriptors from 3 up and `os.execv`s `[shell, "-l"]`, adding `-i` only when stdin is a
terminal, so the shell is the session leader on the pty. A refusal prints one line to stderr and
exits with `refusal_status(code)`: 127 for `exec ENOENT`, 126 for any other `exec` errno, and 125
for `cwd` and `handover`. The shell convention is 127 not found and 126 found but not runnable;
`env(1)` also uses 125 for its own failure.

## Completion, on the client

`envelope.py` holds one filter per stream. Each runs three phases:

- **Seeking.** Search for the start marker, dropping everything before it and keeping only a tail of
  marker length across chunks. Nothing in the dropped bytes is read.
- **Passing.** Forward payload bytes at once, holding back only the longest suffix that is a proper
  prefix of the closing prefix (`0x1E`, nonce, space). No timer is involved. A control line on
  stdout, `<M>parked`, is removed from the stream and reported to the attach; nothing else on a
  stream is a control line before the closing one.
- **Done.** After the closing line, drop everything. stdout closes with `end`, stderr with `exit N`
  or `refused CODE`.

The classifier combines both filters with ssh's own exit status:

| Evidence                                                          | Completion                                                                                                                                                                                                         |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `end` on stdout and `exit N` on stderr                            | `Completed(N)`; ssh's own status is ignored                                                                                                                                                                        |
| stdout abandoned by a departed consumer, `exit N` on stderr       | `Completed(N)`                                                                                                                                                                                                     |
| A departed consumer and no trailer                                | `Unknown(CONSUMER_LEFT)`; `stream` reports 141                                                                                                                                                                     |
| `end` on stdout and `refused CODE` on stderr                      | `NotDelivered(REFUSED, CODE)`                                                                                                                                                                                      |
| No start marker, ssh exit 127                                     | `NotDelivered(PYTHON_MISSING)`                                                                                                                                                                                     |
| No start marker, ssh exit `STDLIB_MISSING_STATUS`                 | `NotDelivered(STDLIB_MISSING)`                                                                                                                                                                                     |
| No start marker, ssh exit 0 to 254 otherwise                      | `NotDelivered(LAUNCHER_FAILED)`                                                                                                                                                                                    |
| No start marker, ssh exit 255, a signal, or a status above 255    | `Unknown(CONNECTION)`; `Unknown(HOST_KEY)` when the client log holds `Host key verification failed` on a 255; `Unknown(AUTH_FAILED)` when it holds `Permission denied (publickey` on a 255 and no host-key failure |
| A start marker, but no `end` or no trailer at exit or end of file | `Unknown(TRUNCATED)`                                                                                                                                                                                               |
| Deadline reached, or a stream over the pump constant              | `Unknown(DEADLINE)`, `Unknown(OUTPUT_LIMIT)`                                                                                                                                                                       |

- **Ending the client.** Once both streams are done the client closes ssh's stdin, waits up to one
  second for a natural exit and kills it. It never waits for end of file or on a timer, so a
  background child holding the channel cannot stall it. If one stream is done and the other is not,
  it keeps pumping until the other completes or both reach end of file.
- **No stream text in errors.** `NotDelivered` and `Unknown` carry reasons, never stream text: with
  no start marker, the stderr may be a traceback quoting request bytes a startup file left unread.
  They do carry the last 20 lines of the client log, which holds only ssh's own diagnostics
  (`Permission denied (publickey)`, `Connection timed out`), so the operator sees why a connection
  failed.

## Terminal handover

`attach(request)` does, in order:

1. `VMAccess.attach` generates `token = secrets.token_hex(8)`, creates the client log, and builds
   both argvs before anything spawns; `attach.py` receives the framed request (`envelope.message`),
   its nonce, the two argvs and the log as data, as the forward does. The first connection gets the
   `tty` envelope for the request with `term`, as `mode: "park"` with `handover: token` (argv shape
   `run`, stage 0 with no argument). The socket path, `/tmp/agw-handover-<token>/s`, is computed
   locally.
2. agw-launch, in `park` mode, first pre-checks what the terminal half would otherwise fail on: the
   working directory (`cwd`, or home) must be enterable and the shell executable. A failure is
   `refused cwd` or `refused exec <ERRNO>`, so it comes back typed before the terminal is touched.
   It then installs `SIGHUP` and `SIGTERM` handlers that raise `SystemExit`, so the `finally` below
   runs, and calls `os.mkdir("/tmp/agw-handover-<token>", 0o700)`. An existing name is
   `refused handover`, so a pre-planted path is never reused. The path is fixed under `/tmp`, so
   `TMPDIR` plays no part. It binds a Unix stream socket `s` in the directory under umask 077,
   listens, and writes `<M>parked` to stdout.
3. The client reads that line from the parking connection within 60 seconds, its own bound for
   connecting, authenticating and running the account's startup files (the launcher's 30 second wait
   starts only after `parked`). Past that it kills the connection and raises
   `ExecutionIncomplete(Unknown(DEADLINE))` with the terminal untouched; for an attach the message
   says no shell started, and names a startup file that reads stdin as the usual cause, since such a
   file would otherwise hold the connection open forever. Then, inside
   `with guarded_terminal() as guard:`, runs the terminal half. Its stage 0 gets the socket path as
   `sys.argv[1]`, connects, reads the launcher frame and the envelope frame from the socket, and
   runs agw-launch in `tty` mode. No guest text reaches a command line.
4. The parked launcher waits with `select` on the listener and on its own stdin, for at most 30
   seconds. On a connection it sends the launcher frame (`g`) and the envelope re-serialized with
   `mode: "tty"`, closes the connection, then writes `end` and `exit 0`. On stdin end of file (the
   client closing its pipe) or the timeout it writes `end` and `refused handover`. In every case a
   `finally` removes the socket and the directory, so a failed second connection leaves nothing. The
   trailer lets the client tell a timeout from a drop.
5. After the second connection returns, still inside the guard, `guard.clean_exit = code == 0`.
   After the guard, a code of 255 prints the notice "connection ended or the command exited 255;
   terminal restored" with the existing detail line (I6), reimplemented in `attach.py` because the
   legacy helper is `transports/ssh.py:77`. The client then closes the parking connection's stdin,
   pumps it for up to two seconds and ends it.

The parking connection fails first on a missing `python3`, a trust mismatch, a refusal or a refused
connection, so `attach` raises `VMNotReady`, `TrustMismatch`, `RequestRefused` or
`ExecutionIncomplete` before the terminal is touched. While the terminal half runs, the parking
connection's pipes go unread; it writes a few lines, far below a pipe buffer. A failure of only the
second connection returns its code; with the socket gone the operator sees Python's traceback on the
terminal and code 1, which is accepted for that rare race. There is no `SO_PEERCRED` check: the
directory is 0700, and the account's own processes can already read its processes (HLA D8).

## Forward

`access.forward(forwards, *, verbose=False) -> int` blocks, over
`Forward(local_port, remote_port, bind_address="localhost")` values. It spawns the forward argv
above and waits for the client, polling `Popen.wait(timeout=0.2)` so Ctrl-C is delivered.

- **Diagnostics.** Without `verbose`, `-E` sends them to the client log and stderr is `DEVNULL`,
  because an unread stderr pipe stalls the client after about a thousand refused channels. With
  `verbose` there is no `-E` and the client inherits the operator's stderr.
- **Exit contract.** `forward` returns the client's exit status and never raises for a non-zero one.
  `ExitOnForwardFailure=yes` and `ConnectTimeout` make a setup failure the same non-zero exit, so
  there is no readiness probe, handle or context manager. On a non-zero exit it reports the log's
  last 20 lines through `agentworks.output`, and a `Host key verification failed` there raises
  `TrustMismatch` instead. Under `verbose` there is no log, so no tail and no `TrustMismatch`
  typing. The client always ends on the way out: terminate, one second, kill, and the log is
  removed.
- **Signals.** The library installs none. `port_forward_vm` holds the forward inside the shared
  `agentworks.interrupts.termination_as_interrupt`, which raises `KeyboardInterrupt` on Ctrl-C and
  `SIGTERM`, and on `SIGHUP` and `SIGBREAK` where they exist, for the duration of the block, and
  restores the previous handlers afterwards. A signal the process already ignores stays ignored (so
  a forward started under `nohup` outlives its terminal); once one signal has arrived, the
  terminations are ignored until the block exits, so a closed terminal or a second termination
  cannot cut the cleanup short, while a second Ctrl-C still interrupts; and a block inside another,
  or off the main thread, changes nothing (`agentworks/interrupts.py`). The unwind's own writes to a
  terminal that hung up, or to a pipe whose reader went with it, are dropped by
  `output.tolerating_dead_sink` rather than failing. `port_forward_vm` owns the 0 on an operator
  stop and returns the client's status for any other end. The clouds' route context opens the same
  block around every route (`public_ssh.py:272`), so a cloud's firewall allow for a root script or a
  native shell is removed when the command is ended by either signal. `create_vm` opens one too,
  first in the stack that spans provisioning and initialization, so either signal runs create's
  interrupt arms: a cloud's create rolls back its bootstrap allow and its partial VM.

## Errors

| Error                 | Kind                 | Raised for                                                                                                                                                                                   | Hint                                                                                                                                  |
| --------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `ClientUnsupported`   | `ConfigError`        | No acceptable OpenSSH, or the resolved client fails to start                                                                                                                                 | The install guidance in the message                                                                                                   |
| `VMNotReady`          | `StateError`         | `NotDelivered(PYTHON_MISSING)` (an unelevated 127) and `NotDelivered(STDLIB_MISSING)`                                                                                                        | `agw vm recover <vm>`, whose repair installs python3                                                                                  |
| `TrustMismatch`       | `StateError`         | `Unknown(HOST_KEY)`, and the forward's token                                                                                                                                                 | Verify the new key, remove the store file, `import-host-keys --known-hosts`                                                           |
| `RequestRefused`      | `StateError`         | `NotDelivered(REFUSED)` under `run` and `attach`; names the code                                                                                                                             | Fix the working directory or the command                                                                                              |
| `ExecutionIncomplete` | `ConnectivityError`  | Any other `NotDelivered` or `Unknown`; carries the `Result`, names which, and shows the client log tail. An `AUTH_FAILED` says the VM did not accept the configured key and that nothing ran | A `NotDelivered` command did not start; an `Unknown` one may have run; `CONNECTION` and `AUTH_FAILED` also name `agw vm recover NAME` |
| `PermissionDenied`    | `AuthorizationError` | An elevated request on an agent-bound access, before dispatch; names the VM and account, never the request                                                                                   | none                                                                                                                                  |
| `CommandFailed`       | `ExternalError`      | `check=True` with a non-zero status; message has the status, never stream text                                                                                                               | none                                                                                                                                  |

Errors may show the client log's tail. `TrustMismatch`'s hint names the manual path, since import
never rewrites an existing record (`trust-lld.md`) and recovery re-establishes only a missing one
(HLA D4). `TrustMissing` already exists (`errors.py`). `FeatureUnavailable` (`StateError`) lands in
slice 4 with `vm shell --platform`, its first consumer. No error message, `repr` or hint holds a
stream, an environment value or script text.

## The migrated commands

All six keep their guards, workspace and secret resolution and the gate. Each calls `resolve_ssh()`
first and builds `access = execution.vm_access(vm, config, Admin() or Agent(agent))`, called through
the package attribute so one patch reaches every manager.

| Command                                       | Request and call                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vm exec` (`exec.py:182`)                     | One argument: `Script(arg, Shell.USER_DEFAULT)`. Several: `Script(shell_command_line(args), Shell.USER_DEFAULT)`: `shlex.join` as today (`vms/manager/exec.py:271` on `main`), so builtins, functions, a leading `NAME=value` assignment and keywords keep working, except that a word starting with `=` is always quoted, since zsh would expand it to a path. `cwd=ws.workspace_path`, `env=compose_env(...)`. `access.stream(...)` with `sys.stdin.buffer.raw` and the process's binary output |
| `agent exec` (`access.py:134`)                | `Script(source, Shell.USER_DEFAULT, login=True)`, with `source` the one argument or `shell_command_line(args)`, as `$SHELL -lc` does today                                                                                                                                                                                                                                                                                                                                                        |
| `vm shell` (`exec.py:35`)                     | `LoginShell(cwd, env)` and `access.attach`                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `agent shell` (`access.py:34`)                | The same with the agent's access, after the agent probe                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `vm verify-connection` (`verification.py:28`) | `run(Command(("true",), deadline=10), check=True)`. The message keeps "via ssh" (the kind is now the constant `ssh`)                                                                                                                                                                                                                                                                                                                                                                              |
| `vm port-forward` (`tailscale.py:73`)         | Port parsing and guards as today, then `access.forward(...)` and the signal handling above                                                                                                                                                                                                                                                                                                                                                                                                        |

- **Splits for the static check.** `exec.py` keeps its shared preamble (guards, workspace, gate
  choice, environment) and both `exec_vm` and `shell_vm`. Only the legacy-touching part of the
  `--platform` branch (`exec.py:162-166`: `native_transport` creation, the `Transport` check and the
  interactive call, with the import at `:72`) moves to `platform_shell.py`, still legacy until slice
  4, so `exec.py` imports nothing retired. `vm shell --platform` uses `native_transport`, and
  `native_shell` is slice 4's. `port_forward_vm` moves from `tailscale.py`, which keeps
  `_ensure_tailscale`'s legacy imports. Slice 4 then deleted `platform_shell.py` and
  `_ensure_tailscale`: `vm shell --platform` calls the platform's `native_shell`.
- **Agent probe.** `_assert_agent_ssh_works` (`agents/manager/_common.py:237`) takes a legacy
  `Transport`, and the sessions and agent-create paths still call it. A new
  `_assert_agent_access_works` in `agents/manager/access.py`, a file under the static check, serves
  `agent exec` and `agent shell` with `true` as the agent. A `Completed` of any status passes. Only
  `Unknown(CONNECTION)` falls back to running `true` as the admin: if that completes, the agent
  predates the rollout (the existing `StateError` and hint); otherwise the VM is unreachable (the
  existing `ConnectivityError`). Every other outcome raises as it is: `VMNotReady`, `TrustMismatch`,
  `RequestRefused` or `ExecutionIncomplete`. This keeps incident row I19, so I19 is re-proved in
  slice 2 as well as 6.
- **Behavior changes** for release notes and the 0.20 guide:
  - OpenSSH 8.5 or newer is required for these six commands, and Windows needs the native build.
  - A VM without `python3` is `VMNotReady`, not a shell error.
  - The guest sees a fixed `PATH`, `LANG=C.UTF-8`, `SHLVL=1` and no `SSH_CLIENT` or
    `SSH_CONNECTION`.
  - `vm exec` still runs its command line under the account shell without login startup, as before,
    so builtins, shell functions and leading assignments keep working; only a word starting with `=`
    is now quoted, so zsh no longer expands it to a path. A missing workspace directory exits 125
    with one line.
  - A `NAME=VALUE` environment string over 131,071 bytes is refused up front; `vm exec`'s command
    line travels in the script source, which has no such limit.
  - `IdentityAgent` set only in `~/.ssh/config` (1Password, Secretive) is ignored by the six
    commands, since no configuration is read; the agent named by `SSH_AUTH_SOCK` serves the key.
  - `vm shell` and `agent shell` without `--workspace` lose the message of the day and last-login
    line, because sshd prints them only when it runs the login shell itself. Accepted by the
    operator on 2026-11-01; nothing breaks, so there is no alert.
  - An encrypted key the default agent does not hold can no longer prompt for its passphrase on
    `vm shell` and `agent shell`, since every connection runs with `BatchMode=yes`. Accepted on
    2026-11-01; a connection that fails before the start marker with a passphrase-protected key
    hints at the agent (HLA D2), whether the failure reads as a plain connection failure or as an
    authentication failure.
  - `/etc/environment` and other `pam_env` variables no longer reach the payload, which gets only
    the rebuilt environment. Accepted on 2026-11-01; reinit warns when a VM has pam_env settings
    (HLA D10).
  - `vm port-forward` exits 0 on an operator stop.
- **Template `shell` fields** (`vms/admin.py:99`, `agents/template.py:101`): the type becomes
  `AccountShell` with a before-validator that reduces a path to its basename, so `/usr/bin/zsh`
  still loads and is stored as `zsh`. Anything whose basename is not `sh`, `dash`, `bash` or `zsh`
  is a config error that names the field and the allowed values. The allowed values are in the field
  docstrings, which the generated samples render (`manifests/samples.py:57`). The dead
  `"/" in shell` branch of `agents/initializer.py:65` goes. `ResolvedAgentTemplate.shell`
  (`agents/templates.py:33`) takes the same type. Existing accounts are not re-checked until slice
  3's base check.

## Guards, fakes and tests

- **Blocker** (`tests/execution/conftest.py`). An autouse function fixture removes the legacy
  modules and their submodules from `sys.modules`, deletes the attribute on each parent package (so
  `from agentworks import ssh` also fails), and puts a `MetaPathFinder` that raises `ImportError`
  first on `sys.meta_path`. It uses `monkeypatch` so every test restores them. The session fixture
  at `tests/conftest.py:83` imports `agentworks.ssh` once before any test, which the per-test
  eviction tolerates. `test_independence.py` still proves import time in a fresh interpreter.
- **Static check** (`tests/test_execution_import_boundary.py`), modeled on
  `tests/test_registry_warning_boundary.py:19-50`. It parses each file in `MIGRATED_FILES` and fails
  on any `Import` or `ImportFrom` of a legacy module, anywhere in the file (top level, in a
  function, under `TYPE_CHECKING`), including `from agentworks import ssh`, relative forms and the
  `import agentworks.ssh as x` aliases. Slice 2's files: `vms/manager/exec.py`,
  `vms/manager/verification.py`, `vms/manager/port_forward.py` and `agents/manager/access.py`. The
  `execution/` modules are covered by the package guard. Later slices append.
- **`RecordingVMAccess`** (`tests/vm_access_support.py`) subclasses `VMAccess`, overrides the four
  operations to record `(operation, request)` and return a canned `Result` or raise, and needs no
  ssh. A fixture patches `agentworks.execution.vm_access` to return it. Manager tests run on every
  CI host with it.
- **Unit tests**, none restating implementation or prose:
  - envelope round trip over all bytes `0x01` to `0xFF` as values, and each refusal, including an
    element and a string at 131,071 bytes (accepted) and 131,072 (`ValidationError`);
  - the filters under every split of a stream and every interleaving of the two, including a payload
    ending in the closing prefix's first bytes and a `parked` line;
  - the classifier table, row by row, and the refusal-to-status table;
  - `ssh -G` against the installed client, with the policy argv: `-T` beats `RequestTTY force`,
    identity and trust options take effect, no multiplexing, keepalives on, the defaults `-F none`
    leaves are still the defaults, and quoting holds for an identity path with a space; `VM` trust
    with no record spawns nothing;
  - the resolver against temporary executables, including an empty and a relative `PATH` element and
    the Windows candidate order;
  - stage 0 is one line of under 200 characters (`< 200`) with no `'`, `"`, `!` or backslash, and
    the launcher parses at 3.11 and imports only the standard library, inside its guard;
  - passivity: `vm_access` on a VM with no address and a missing key file raises nothing and spawns
    nothing;
  - error messages and `repr` hold no stream or environment value;
  - the pump (the adapted proof cases), `@pytest.mark.windows` for binary stdio and end of file;
  - a subprocess test of `stream`: stdin held open by the parent, the guest exits, its status comes
    back, and the process exits without an interpreter abort;
  - doctor's rows; invalid template shells refuse at config load and a path loads as its basename;
    the six commands refuse an unsupported client before reading the database; the agent probe's
    four outcomes.

### Conformance vectors

One list of vectors, three lanes. **L** runs `$SHELL -c <remote command>` locally through the same
pump and envelope, with the account's startup files set per vector and `SSH_CLIENT` exported as sshd
does; its interpreter path is a parameter. It covers the shells and runs on every Linux CI host,
once with `/usr/bin/python3` and once with a 3.11 interpreter from `uv python install 3.11`, since
`ast.parse` cannot catch 3.12-only standard library calls. **S** runs the real client against a
rootless local `sshd` (`ListenAddress 127.0.0.1`, a temporary host key and `AuthorizedKeysFile`,
`UsePAM no`, `StrictModes no`) and `trust.write_records` for the test host key; it skips locally
when `sshd` is missing or cannot start, fails on CI in either case, and the CI job installs
`openssh-server` if the runner image lacks it. **V** is the live lane, `-m integration` with
`AGW_CONFORMANCE_VM`, run by the tester from each workstation. `zsh` and `dash` are required on CI
(a missing shell fails there, skips locally).

| Vector                                                                                                                                                                                          | Lanes  |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| Literal argv: empty, whitespace, shell characters, dash-led items and `--` (I5)                                                                                                                 | L S V  |
| Script versus stdin; script text absent from the interpreter's `/proc/<pid>/cmdline`                                                                                                            | L S V  |
| Interpreter and startup: `sh`, `bash`, `user_default`, each with and without login                                                                                                              | L V    |
| Environment: every byte `0x01` to `0xFF`, `%`, quotes, `${HOME}`, CR, LF                                                                                                                        | L S V  |
| Size limits: an argument and an environment value at 131,071 bytes run, one more is refused; a total over `ARG_MAX` is `exec E2BIG`                                                             | L V    |
| Working directory, and a missing one as `refused cwd` (status 125 under `stream`)                                                                                                               | L V    |
| A missing command is `exec ENOENT` and status 127; any other `exec` failure, such as a file with no execute bit or no shebang, 126                                                              | L S V  |
| Exit 0, 1 and 255; death by signal as 128 plus the signal                                                                                                                                       | L S V  |
| Stage 0 under `sh`, `dash`, `bash`, `zsh` with startup files that print and export                                                                                                              | L      |
| Output before the start marker dropped, with and without a trailing newline                                                                                                                     | L      |
| `NotDelivered` versus `Unknown`: no python3 (127), a missing standard library module (69), a startup `exit 0`, a startup `exit 255`, a killed launcher                                          | L      |
| A startup file that reads stdin, and one that redirects a standard stream, as not delivered; no stream text in the error                                                                        | L      |
| Signal dispositions and descriptors reset; a startup `TERM` or `XDG_RUNTIME_DIR` leaves the payload's values; in the shell of a handover, `SigIgn` and `SigBlk` in `/proc/self/status` are zero | L      |
| Exact stdin positioning through `stream(stdin=...)`: the payload reads the caller's first byte                                                                                                  | L S V  |
| A bash child of the payload reads no account startup file; stdin a pipe, and in L a socket (I11)                                                                                                | L S V  |
| Background child holding both streams: prompt completion, status correct                                                                                                                        | L S V  |
| Payload stdout with no trailing newline; a `refused` trailer                                                                                                                                    | L S    |
| Deadline: `Unknown(DEADLINE)` at the deadline, client reaped                                                                                                                                    | L S    |
| Handover: success, the 30 s timeout, an existing directory name, a missing `cwd`, second connection never arrives, nothing left behind                                                          | L S V  |
| The payload leaves stdout and stderr non-blocking and full, then exits 0: `Completed(0)` with both closing markers (L is the regression guard; S and V drain too fast to catch it)              | L S V  |
| A startup file that reads stdin under an attach: the parking wait expires with the terminal untouched, bounded by the vector itself                                                             | L      |
| `memfd_create` refusing `MFD_NOEXEC_SEAL` (`EINVAL`) runs the script; `EMFILE` is `NotDelivered(LAUNCHER_FAILED)` with the payload not run                                                      | L      |
| Host key mismatch is `TrustMismatch`, read from the client log                                                                                                                                  | S      |
| Binary stdin and stdout, from Windows (`windows` mark) and live                                                                                                                                 | pump V |

The stdout-after-trailer ordering is a unit test of the filters, not a vector, since pipes do not
reorder.

## Collateral

- **ADR 0013** (status block): for the migrated commands secret environment travels in the request
  on the client's stdin (HLA D3), not as `-o SetEnv`. The decision, CLI-side injection with no
  VM-side storage, stands, and the command-line exposure in its Negative 2 no longer applies there.
- **ADR 0014** (status block): the migrated commands send no environment through ssh. `AcceptEnv *`
  and the coalesced `SetEnv` remain for the legacy paths; slice 8 decides whether the fragment keeps
  a user. ADR 0020 stays until slice 8, when the `-n` path it describes goes.
- **Docs:** `cli/command-reference.md` (the six commands' behavior changes, the OpenSSH
  requirement), `docs/guides/upgrading-to-0.20.md` (each behavior change above, the shell
  restriction, `VMNotReady` and its reinit hint), troubleshooting if it names the old messages, and
  the sample field docstrings. There are no new commands or options, so no completion changes.
- **READMEs:** `cli/agentworks/execution/README.md` and `cli/tests/execution/README.md` are
  rewritten for what exists; `tests/execution/carriers/ssh/README.md` goes.
- **Commit:** a `BREAKING CHANGE:` footer for the 8.5 requirement and the template shell restriction
  (`CONTRIBUTING.md:69`).
- **Incident inventory** gains a row (the audit of these entry points' fix history found one more
  behavior to re-prove):

| Row | Behavior                                                                                                                  | Re-proved in |
| --- | ------------------------------------------------------------------------------------------------------------------------- | ------------ |
| I21 | The local terminal is sanitized on entry and restored on exit after a dropped or killed attach (`897248ead`, `80c813db4`) | 2            |

## Incident rows slice 2 re-proves

| Row | How                                                                                                                                                  |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| I1  | No `-n`; the client closes its own stdin pipe. Pump test for end of file; live Windows loop of 25 runs each of `true`, `sleep .001` and `echo -n ""` |
| I2  | Binary pipes and raw or `.buffer` streams; the all-bytes vectors; the Windows-marked pump test; live from Windows                                    |
| I3  | The V lane drives real guests from each workstation; the resolver refuses MSYS builds, so no Git for Windows tool is in the path                     |
| I4  | No operation combines input with a terminal; value-free errors tested                                                                                |
| I5  | Literal argv and the `--` before the destination; the dash-led vector                                                                                |
| I6  | `attach` tests with a fake terminal check the notice comes after the guard; live                                                                     |
| I7  | `ssh -G` shows the keepalive and connect options; one live cell drops the network under a `stream`                                                   |
| I11 | The vector, with a pipe and a socket on stdin                                                                                                        |
| I12 | `ssh -G` shows no multiplexing with the policy argv, and the live 8.5 and Windows cells repeat it                                                    |
| I13 | `ssh -G` shows the explicit identity file; live with a key named other than the default on Windows                                                   |
| I14 | `ssh -G` of the pipe-mode argv shows no pty even with `RequestTTY force` supplied                                                                    |
| I19 | The agent probe (above)                                                                                                                              |
| I21 | The terminal guard tests, and live attach killed mid-session                                                                                         |

## Live evidence

The plan's cells: all three workstations against Remote Lima, Linux and macOS against local Lima,
Windows against WSL2, and one cloud, with a Debian 12 and a Debian 13 guest and an OpenSSH 8.5
client on one workstation (else the operator's disposition). For each, the V vectors and these:

- `-F none` and the stdin protocol on 8.5 and on Win32-OpenSSH, including the stage 0 quoting round
  trip through `ssh.exe`, and the client log capturing ssh's diagnostics;
- `vm exec` from Windows with binary stdin and early exit;
- an attach from each workstation, killed mid-session, and a handover whose second connection fails;
- a forward with a busy local port, and Ctrl-C on a live forward.

## Amendments

The HLA and the plan carry the choices here that change them.
