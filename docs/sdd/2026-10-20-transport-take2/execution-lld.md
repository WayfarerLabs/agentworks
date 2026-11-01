# Transport Take 2: Execution Core Low-Level Design (Slice 2)

- Status: Draft for review. Nothing here is implemented.
- Date: 2026-11-01
- Architecture: [hla.md](./hla.md) D1 to D5, D8, D12 to D14; requirements: R2 to R5, R8 to R11
- Plan: [plan.md](./plan.md), slice 2, delivered in PR A with slice 1
- Source baseline: `7b2209c60` (slice 1 merged into the PR A branch)

## Scope

This LLD settles the details the HLA leaves open for the execution core and the operator exec
surface:

- client resolution, the VM and host policies, and how the client spawns `ssh`;
- the stdin framing, stage 0, the envelope and the start and completion markers;
- agw-launch's steps and the terminal handover;
- request and result types, error mapping and the operation log;
- the migrated commands, the guards and the proof modules' adoption and deletion.

Where the HLA is wrong or silent, the narrowest choice is made here and marked **HLA amendment
proposed**. The lead folds those into the HLA.

**Left to the first consumer.** HLA D5 lands each vocabulary item with its first consumer, and no
slice 2 consumer uses these, so they are specified only where a later slice's contract depends on
them:

- `elevated` (the `sudo -n` prefix) with `PermissionDenied`, and the base checks that use them
  (slice 3). Slice 3 must settle one interaction: a sudoers `use_pty` default would turn descriptors
  1 and 2 into a pty, which agw-launch's stream check (below) refuses;
- `observation` and retry, with `reachability` (slice 3);
- `PythonProgram`, `Script.interactive` and `FeatureUnavailable` (slice 4 and later). Measured here
  for that slice: `dash -i FILE` prints a prompt to stderr per line it reads from the file, so an
  interactive script needs the HLA's startup shell that execs the interpreter, not `-i FILE`;
- the `Discard` and `ToFile` sinks and the `FromFile` input (slice 7);
- `attach`'s `clear_screen_on_exit` (slice 7), the Enroll trust mode (slice 3) and the host policy
  (slice 4).

## Module layout

| Module                                           | Contents                                                                                                         |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `agentworks/execution/client.py`                 | `resolve_ssh`, `SSHClient`: resolution, banner and version check                                                 |
| `agentworks/execution/ssh_process.py`            | `VMPolicy`, `build_argv`, `Deadline`, and the pump that spawns a client and carries its three streams            |
| `agentworks/execution/envelope.py`               | Frames, the JSON envelope, nonce, marker bytes, the stream filters and the completion classifier                 |
| `agentworks/execution/launcher.py`               | The client's half of agw-launch: stage 0, `launcher_source()`                                                    |
| `agentworks/execution/agw_launch.py`             | The guest program. It is exec'd by stage 0, never imported in production                                         |
| `agentworks/execution/requests.py`, `results.py` | The slice 2 vocabulary (below)                                                                                   |
| `agentworks/execution/access.py`                 | `VMAccess`, `Admin`, `Agent`, `vm_access`                                                                        |
| `agentworks/execution/attach.py`                 | The two-connection attach under `guarded_terminal`                                                               |
| `agentworks/execution/forwarding.py`             | The forward context manager                                                                                      |
| `agentworks/execution/oplog.py`                  | `LOG_DIR`, `OperationLog`                                                                                        |
| `agentworks/execution/errors.py`                 | Adds `ExecutionIncomplete`, `CommandFailed`, `VMNotReady`, `TrustMismatch`, `ClientUnsupported`, `ForwardFailed` |
| `agentworks/vms/manager/port_forward.py`         | `port_forward_vm`, moved out of `tailscale.py` so the file can pass the static import check                      |
| `agentworks/vms/manager/platform_shell.py`       | `vm shell --platform`, moved out of `exec.py`; legacy until slice 4                                              |
| `tests/execution/conftest.py`                    | The legacy-module blocker                                                                                        |
| `tests/execution/conformance/`                   | The vectors and their carriers                                                                                   |
| `tests/vm_access_support.py`                     | `RecordingVMAccess`                                                                                              |

`launcher.py` and `agw_launch.py` split the HLA's one `launcher.py` row, because one half runs on
the workstation and the other is text sent to the guest. `trust.py` makes `_literal_path`
(`trust.py:122`) public as `literal_path(path, what)`, so the VM policy quotes the identity file by
the same rules as the store path and its refusal names what it was refusing.

## The proof modules

| Proof module                                                    | Fate                                                                                                                                                                                                                                                                                                                                                                                    |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `carrier.py`                                                    | Deleted. `Deadline` (`:20`) moves into `ssh_process.py`; the report types give way to `results.py`                                                                                                                                                                                                                                                                                      |
| `preparation.py`                                                | Deleted. The base64 line envelope and its 256 KiB cap are superseded by `envelope.py`; the `Command` and `Script` shapes carry over to `requests.py`. Its reserved-environment list existed for the Bash bootstrap and does not carry over                                                                                                                                              |
| `bootstrap.py`                                                  | Deleted, replaced by `agw_launch.py`. Its locale save, force and restore (`:31-33`, `:135`) was for Bash's own parsing and has no counterpart in a Python launcher                                                                                                                                                                                                                      |
| `carriers/ssh/client.py`                                        | Adopted into `client.py`: the banner regex (`:27`) and the 8.5 floor (`:61`). The probe on every execute becomes once per process; `SSHCarrier` is deleted                                                                                                                                                                                                                              |
| `carriers/ssh/connection.py`                                    | Adopted into `ssh_process.py`: the option list (`:109-153`, reconciled below) and the host and user checks (`:51-58`). Its trust options are `trust.ssh_options`, so only one copy survives                                                                                                                                                                                             |
| `carriers/ssh/_io.py`                                           | Adopted into `ssh_process.py`: the single-threaded non-blocking pump, kill and reap, the Windows environment strip (`:104-114`) and cleanup on `BaseException` (`:191`)                                                                                                                                                                                                                 |
| `carriers/proxmox.py`, `carriers/_proxmox_http.py`              | Deleted (R10). Slice 4 writes the QGA client fresh                                                                                                                                                                                                                                                                                                                                      |
| `tests/execution/` proof tests, `conformance.py`, their READMEs | Deleted with their modules, except the process-level cases of `carriers/ssh/test_client.py` (duplex pressure `:146`, EOF on empty input `:164`, capture limits `:174`, short and failed writes `:219` and `:234`, deadline reap `:272`, interruption `:430`, local signals `:449`, descendant handles `:459` and `:507`, the Windows environment `:543`), which are adapted to the pump |

`test_independence.py` stays (HLA D14) with its `prepare()` section replaced: after importing every
submodule it asserts that none of the six legacy modules is in `sys.modules`.

## Client resolution

`client.resolve_ssh() -> SSHClient` is cached for the process and returns the absolute path and the
parsed `(major, minor)`. Every spawn takes the `SSHClient`, so no caller can launch a bare name.

- **POSIX.** The first `ssh` along `PATH`, skipping empty and relative elements. Only that candidate
  is judged.
- **Windows.** `PATH` is never searched. The candidates are `%SystemRoot%\System32\OpenSSH\ssh.exe`
  and `%ProgramFiles%\OpenSSH\ssh.exe`, in that order, each skipped when its variable is unset.
  Every candidate is judged and the first acceptable one wins, so a Windows 10 workstation with the
  inbox 8.1 client and the MSI install resolves to the MSI build.
- **Judging.** `ssh -V` runs once per candidate (`stdin=DEVNULL`, 10 s). The banner on stderr must
  match the proof's regex, with the `for_Windows_` form required on Windows and refused elsewhere,
  and the version must be at least 8.5.
- **Refusal.** `ClientUnsupported` lists each path tried with its reason (missing, wrong build, too
  old) and how to install a supported client, naming the Win32-OpenSSH MSI and the inbox 8.1 client
  on Windows 10.
- **Before credentials.** `require_vm_access_boundary(db, config, vm)` in `vms/manager/boundary.py`
  calls `resolve_ssh()` and then `require_vm_ssh_boundary` (`boundary.py:65`). The six migrated
  commands call it first, before `gated_vm_boundary`, so an unsupported client refuses before any
  prompt. It does not go inside `require_vm_ssh_boundary`, because the session, workspace and backup
  commands still use the legacy `ssh`; slice 7 folds it in when the last legacy user of the gate
  moves.
- **Doctor.** `_check_required_tools` (`doctor.py:526`) reports `ssh` from `resolve_ssh()`: the path
  and version when acceptable, the refusal otherwise. **HLA amendment proposed:** it keeps its `scp`
  check, because backup and workspace copy still use `scp` until slice 7, and the check goes with
  the last user (HLA D1 and the plan's slice 2 box say it goes now).

## Requests and results

The slice 2 subset of D5. Strings are `str`; `Shell` is a `StrEnum` of `sh`, `bash` and
`user_default`.

| Type         | Fields                                                                                 | First consumer                                                 |
| ------------ | -------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `Command`    | `argv: tuple[str, ...]`                                                                | `vm exec` with several arguments, `verify-connection`          |
| `Script`     | `source: str`, `shell: Shell`, `login: bool = False`                                   | `vm exec` with one argument, `agent exec` (login)              |
| `LoginShell` | none                                                                                   | `vm shell`, `agent shell`                                      |
| Common       | `env: Mapping[str, str] = {}`, `cwd: str \| None`, `deadline: float \| None` (seconds) | all                                                            |
| Input        | `NoInput`, `Bytes(data: bytes, sensitive: bool = False)`                               | conformance vectors (R4); production callers arrive in slice 5 |
| Output       | `Capture(limit: int = 1 MiB)`                                                          | `run`                                                          |

**HLA amendment proposed:** D5 gains `LoginShell`, the identity's default shell as an interactive
login shell (`[shell, "-l", "-i"]`). A `Script` with an empty source would run `shell -l -i FILE` on
an empty file and exit, and a name that says "shell session" is more truthful than that. It is valid
only for `attach`; `run` and `stream` refuse it, and `attach` refuses a request with a `deadline`.

`Result` holds `completion`, `stdout: bytes` and `stderr: bytes` (both `repr=False`), plus `text()`
for decoded `stdout` (UTF-8, `errors="replace"`, no newline conversion).
`Result.require_completed(vm_name) -> int` returns the status of a `Completed` and otherwise raises
by the error table below; `run(check=True)` is that plus `CommandFailed` for a non-zero status.

```text
Completed(status)
NotDelivered(reason)   CLIENT_START | PYTHON_MISSING | LAUNCHER_FAILED | REFUSED(detail)
Unknown(reason)        CONNECTION | HOST_KEY | TRUNCATED | DEADLINE | OUTPUT_LIMIT
```

- **Sensitive stdin.** Both streams are read and dropped, `Result` holds empty bytes, and the
  operation log keeps no stream (D5, D13). No call combines input with a terminal: `run` and
  `stream` use `-T` and `attach` takes no input, so incident row I4's refusal is structural.
- **`access.stream(request, *, stdin, stdout, stderr) -> int`** and
  **`access.attach(request) -> int`** return the guest's status. They raise rather than return
  `NotDelivered` or `Unknown`, because their consumers can only report it.
- **Validation** lives in the one function that turns a request into bytes (`envelope.encode`).
  Boundary: environment, arguments, working directory and script text come from operator-authored
  config and from secret backends. It refuses NUL anywhere, an environment name outside
  `[A-Za-z_][A-Za-z0-9_]*`, a relative `cwd`, an empty argv or empty `argv[0]`, and a lone surrogate
  outside `U+DC80` to `U+DCFF` (below).

## The access object

`execution.vm_access(vm, config, identity, *, log=None) -> VMAccess`, with `identity` either
`Admin()` or `Agent(name, linux_user)`. Construction copies the VM name, its tailnet address, the
SSH user (`admin_username` or the agent's `linux_user`), the identity file path and the log, and
nothing else (R9): no key is read, no trust file is opened and nothing is spawned. Its trust is
always `TrustBinding(TrustMode.VM, vm.name)`; Enroll arrives with slice 3.

| Operation                                                         | Returns                   |
| ----------------------------------------------------------------- | ------------------------- |
| `run(request, *, input=NoInput(), output=Capture(), check=False)` | `Result`                  |
| `stream(request, *, stdin, stdout, stderr)`                       | the guest's status        |
| `attach(request)`                                                 | the guest's status        |
| `forward(forwards, *, verbose=False)`                             | a context manager (below) |

Each operation runs in this order, so everything before the spawn raises with nothing dispatched:
`resolve_ssh()` (`ClientUnsupported`), encode the request (`ValidationError`), build the options
(`TrustMissing`), spawn, pump, classify, log. A VM with no tailnet address is a `StateError` at that
point, the first use of a persisted value.

## Policies and the client argv

`build_argv(client, policy, destination, mode, remote_command, forwards=())` returns the argument
list. `VMPolicy` holds the identity file and a `TrustBinding`; `Destination` holds host, user and a
port (22 except in tests, which run a local `sshd`). Both are validated at their boundary, the
database, with the proof's host and user patterns (`connection.py:51-58`).

**VM policy options.** `-F none` first, so no operator or system configuration applies, then these
as `-o` (the proof's list at `connection.py:109-153`, reconciled with D2 and `trust.ssh_options`):

| Group     | Options                                                                                                                                                                                                                    |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identity  | `IdentityFile="<ssh_private_key>"`, `IdentitiesOnly=yes` (I13). `CertificateFile="<ssh_private_key>/disabled-certificate"`, the 8.5 workaround (`:117-121`). No `IdentityAgent`: the default agent serves an encrypted key |
| Auth      | `BatchMode=yes`, `PreferredAuthentications=publickey`, `PasswordAuthentication=no`, `KbdInteractiveAuthentication=no`, `SecurityKeyProvider=none`, `PKCS11Provider=none`, `HostbasedAuthentication=no`                     |
| Trust     | `trust.ssh_options(TrustBinding(VM, vm.name))`, read at spawn (so a missing record is `TrustMissing` and nothing spawns), plus `KnownHostsCommand=none` and `VerifyHostKeyDNS=no`                                          |
| Isolation | `CanonicalizeHostname=no`, `ForwardAgent=no`, `ForwardX11=no`, `Tunnel=no`, `ProxyCommand=none`, `ProxyJump=none`, `ControlMaster=no`, `ControlPath=none`, `ControlPersist=no` (I12), `PermitLocalCommand=no`              |
| Liveness  | `ConnectionAttempts=1`, `ConnectTimeout=30`, `ServerAliveInterval=15`, `ServerAliveCountMax=4` (I7; the constants are `ssh.py:288` and `:296-297`)                                                                         |
| By mode   | `ClearAllForwardings=yes` except for a forward; `EscapeChar=none` except for the terminal half of an attach                                                                                                                |

- **Dropped from the proof:** its own `StrictHostKeyChecking`, `GlobalKnownHostsFile=none`,
  `UpdateHostKeys`, `CheckHostIP`, `UserKnownHostsFile` and `HostKeyAlias` (now `trust.py`'s one
  copy), `IdentityAgent`, `RevokedHostKeys` (no consumer) and the `known_hosts_file` plumbing.
- **HLA amendment proposed:** D9 says slice 2's VM policy uses `none` values for
  `GlobalKnownHostsFile`. It keeps the null device `trust.ssh_options` already pins, which every
  version accepts, so the legacy paths and the VM policy share one function.
- **HLA amendment proposed:** the liveness options apply to every connection, not only streaming,
  attach and forward (D2). A dead peer then ends a `run` that has no deadline within a minute,
  instead of hanging, and one option list has no per-mode branches.
- **`EscapeChar`.** The proof turns the escape off. The terminal half leaves ssh's default `~`, as
  `transports/ssh.py:380` does today, so an operator can still kill a hung session with `~.`.
- **Environment.** Nothing is sent through ssh. Under `-F none` no `SendEnv` applies, and the
  request's environment is in the envelope (D3).

**Argv shapes.** `<stage0>` is `/usr/bin/python3 -I -S -c '<program>'` (below).

| Mode                              | Argv                                                                                      |
| --------------------------------- | ----------------------------------------------------------------------------------------- |
| `run`, `stream`, the parking half | `ssh -F none -T <options> -l <user> -- <host> <stage0>`                                   |
| Terminal half of an attach        | `ssh -F none -t <options> -l <user> -- <host> <stage0> <socket path>`                     |
| Forward                           | `ssh -F none -N -n <options> -o ExitOnForwardFailure=yes [-v] -L ... -l <user> -- <host>` |

- **No `-n` on request-bearing connections.** `-n` would replace the request's stdin with the null
  device. ADR 0020's measured alternative, a stdin pipe the client closes, was proven equivalent
  there (PR #736, 80 of 80), so "closed stdin" is the client closing its pipe after the request and
  the caller's input. A forward carries no request and uses `-n`.
- **`-T` and `-t`.** `-T` on every pipe mode (I14); `-T` also beats an operator's `RequestTTY force`
  because no configuration is read. **HLA amendment proposed:** the terminal half uses `-t`, not the
  HLA's `-tt`, as `transports/ssh.py:380` does. `-tt` would force a pty when the operator's stdin is
  not a terminal, which breaks `echo ls | agw vm shell`. In TTY mode agw-launch accepts a non-pty.
- **Forwarded addresses.** `-L <bind>:<local>:localhost:<remote>` as `tailscale.py` builds it today.

**The host policy** lands in slice 4 with its first callers, and nothing in `build_argv` precludes
it. It omits `-F none` and the isolation pins, honors the operator's configuration, takes
`StrictHostKeyChecking=accept-new` from `trust.ssh_options(HOST_TRUST)` or the platform's own trust
options, adds `BatchMode=yes` and the liveness options, and uses the same pump. Its destination may
be an alias with no user.

**Spawning.** `subprocess.Popen(argv, stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0)` with the
proof's Windows environment strip (`_io.py:104-114`). The pump is the proof's single-threaded loop
(`_io.py:32-205`): non-blocking pipes (supported on Windows pipes from Python 3.12), 64 KiB chunks,
a 10 ms sleep when nothing moved, the deadline, then kill and a 0.5 s reap, and cleanup on
`BaseException`. It is argv-agnostic, which is what lets the conformance suite run it against a
local shell.

- **Who uses it.** `run`, `stream` and the parking connection of an attach reuse the one pump. The
  terminal half (inherited stdio, `subprocess` wait) and the forward (stdin null, stderr captured)
  have no pipes to pump and do not.
- **Operator stdin for `stream`.** A daemon thread reads `stdin.read1(65536)` into a queue and the
  pump drains one chunk per pass into ssh's stdin, closing it at end of file. A thread is needed
  because Windows cannot poll a console or pipe handle, and one mechanism serves every platform. A
  caller with no stdin passes `None` and the pipe closes right after the request. The thread
  outlives the call blocked in a read; `stream` is the last thing a command does. A write that fails
  after the start marker is the consumer ending early, not a failure.
- **Binary stdio.** Callers pass `sys.stdout.buffer`, `sys.stderr.buffer` and `sys.stdin.buffer`
  (the text layer's `newline="\n"` at `cli/_entry.py:38` does not apply), after flushing the text
  layer (I2).

## The wire

The client writes three things to ssh's stdin, in order: the launcher source frame, the envelope
frame, then the caller's input, untouched, until end of file. A frame is **ten ASCII decimal digits,
zero padded, then that many bytes**.

**Stage 0.** The program below, as one `exec("...")` string with `\n` escapes, 209 characters:

```python
import os,sys;a=sys.argv[1:];f=0
if a:from socket import*;s=socket(1);s.connect(a[0]);f=s.fileno()
def r(n,d=bytes()):
 while n:c=os.read(f,n)or 1/0;d+=c;n-=len(c)
 return d
exec(g:=r(int(r(10))))
```

- **HLA amendment proposed:** D4 says under 200 characters. With the handover branch this is 209,
  and I could not bring it under 200 without dropping the socket branch, so the bound becomes 256.
  The reason for the bound, one line without a single quote that survives a shell and a sudo log, is
  unchanged.
- **Quoting.** The program has no single quote, which would end the account shell's quoting, and no
  `!`, which `csh` expands even inside single quotes. It has no newline. Its only `"` are the pair
  around the `exec` literal and its only backslashes are the `\n` escapes, so Windows argument
  quoting escapes one pair. Measured under `sh`, `dash`, `bash` and `zsh` with Python 3.11; the
  Windows round trip is a live cell.
- **`r(n)`** reads exactly `n` bytes with `os.read` and so leaves stdin at the next byte (no
  buffered reader may touch it). End of file raises, so any short or empty input is a traceback and
  exit 1 before a marker. agw-launch reuses `r` for the envelope. `f` is stdin, or the socket on the
  handover form. `g` keeps the launcher source for a parking launcher to serve.
- **Python.** Measured with 3.11 (Debian 12); the program uses nothing newer, and Debian 13's 3.13
  is a live cell. The launcher is plain syntax too: `ast.parse(..., feature_version=(3, 11))` in a
  test, and the conformance vectors run on whatever `/usr/bin/python3` is present.

**Envelope.** One JSON object, `json.dumps(obj, ensure_ascii=True, separators=(",", ":"))`, so the
wire is pure ASCII whatever the workstation's locale. No version field (HLA D3).

| Field    | Type                         | Meaning                                                                             |
| -------- | ---------------------------- | ----------------------------------------------------------------------------------- |
| `nonce`  | 32 lowercase hex             | `secrets.token_hex(16)` per request, never in the payload's argv or environment     |
| `mode`   | `run`, `tty` or `park`       | Pipe mode with markers; terminal exec; park for a handover                          |
| `kind`   | `argv`, `script` or `shell`  | `Command`, `Script`, `LoginShell`                                                   |
| `argv`   | list of str                  | `argv` kind, non-empty                                                              |
| `shell`  | `sh`, `bash`, `user_default` | `script` kind                                                                       |
| `source` | str                          | `script` kind                                                                       |
| `login`  | bool                         | `script` kind                                                                       |
| `cwd`    | str or null                  | Absolute; null means the home directory                                             |
| `env`    | object of str to str         | The request's environment                                                           |
| `term`   | str or null                  | `tty` and `park` modes: the operator's `TERM`, `xterm-256color` when unset or empty |

- **Bytes in JSON.** Values are `str`. Text that is not valid UTF-8 travels as the code points
  `os.fsdecode` produces (`U+DC80` to `U+DCFF`), which JSON carries as `\udcXX`. The guest encodes
  every string with `utf-8` and `surrogateescape`, so the guest sees the original bytes: `%`,
  quotes, `${...}`, CR, LF and every byte but NUL exactly. A string that cannot encode that way is
  refused by the client; the launcher refuses it too, before its start marker.
- **Markers.** Every marker is `0x1E`, the nonce, a space, a word, a newline: `start`, `end`,
  `exit N`, `refused CODE`, `parked DIR`. Payload output is the bytes between the start marker and
  the closing one, so it needs no trailing newline. The lead byte is one no text emits, so the
  filters' hold-back (below) almost never delays a real byte; a lead hex digit would hold back one
  in sixteen output endings.

## agw-launch

The guest program, run by stage 0 under `python3 -I -S` from `launcher_source()`, which reads
`agw_launch.py` from the installed package. The file has no import-time effect (its `main()` runs
only when `__name__` is `"__main__"`, as it is under stage 0's `-c`), so the package guard can
import it. Steps, mapped to HLA D4:

0. **Read.** The envelope by `r`. Malformed JSON or schema exits 70 before any marker. Every failure
   before the start marker exits 70 or, for an uncaught exception, 1; neither is 127 or 255.
1. **Check the streams** (`run` and `park` modes). Descriptors 1 and 2 must each be a FIFO or a
   socket, and distinct (`st_dev` and `st_ino`). A startup file that ran `exec >/dev/null` or
   `exec 2>&1` fails this and exits 70.
2. **Start marker** to descriptor 1, then 2.
3. **Reset.** Environment rebuilt in this order: `HOME`, `USER`, `LOGNAME`, `SHELL` from the passwd
   entry of `os.getuid()` (a missing entry is `refused passwd`; an empty shell is `/bin/sh`), then
   `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/games:/usr/games`,
   `LANG=C.UTF-8`, `SHLVL=1`, `TERM` and `SSH_TTY` (the terminal name) in `tty` mode when stdin is a
   terminal, `XDG_RUNTIME_DIR=/run/user/<uid>` when that directory exists and the uid owns it, and
   last the request's `env`, which may override anything. Nothing is copied from the inherited
   environment. The working directory is set with `os.chdir` in agw-launch itself, to `cwd` or home
   (`refused cwd` on failure, which `Popen` could not tell from a missing `argv[0]`), the umask is
   `022`, every signal an earlier process ignored goes back to its default, the signal mask is
   cleared, and the payload gets no descriptor above 2.
   - **Choices the HLA leaves open.** `PATH` is one fixed value for every identity, a superset of
     Debian's login paths so `ip` resolves for an admin and later for root. The locale is
     `LANG=C.UTF-8` and no `LC_*`: sessions used to inherit the workstation's `LANG` through
     `SendEnv`, which `-F none` stops sending, and a fixed value makes output reproducible.
4. **Run the payload as a child** (`subprocess.Popen` with bytes arguments and environment,
   `restore_signals=True`, `close_fds=True`, `umask=0o022`), not a shell:
   - `argv`: the arguments themselves; `argv[0]` is searched in the rebuilt `PATH`. A failure to
     execute is `refused exec <ERRNO>`.
   - `script`: the source goes to an anonymous file from `os.memfd_create`, passed in `pass_fds` as
     the one inherited descriptor and named `/dev/fd/N`, so script text is in no argv and the
     interpreter's stdin stays the caller's. The interpreter is `/bin/sh`, `/bin/bash` or the passwd
     shell (`refused interpreter` when it is not executable), run as `[interp] [-l] /dev/fd/N`. The
     interpreter's own children inherit the descriptor; it holds only the script they came from.
   - `shell`: `tty` mode only, below. The payload's stdin is agw-launch's, positioned at the
     caller's first byte. There is no copy loop.
5. **Complete.** After `wait`, write `<M>end` to descriptor 1 then `<M>exit N` to descriptor 2, with
   `N` the exit status, or 128 plus the signal for a payload killed by one. A refusal writes `end`
   then `refused CODE` instead. Codes are `passwd`, `interpreter`, `cwd`, `exec <ERRNO>` and, for a
   parked launcher, `handover`.

**`tty` mode** (the terminal half of an attach) runs steps 0, 3 and 4 with no check, no markers and
no child: it closes descriptors from 3 up and `os.execvpe`s the payload, so the shell is the session
leader on the pty. It takes any request kind; slice 2 sends only `LoginShell`. For `LoginShell` the
program is `[shell, "-l", "-i"]`. A refusal prints one line to stderr and exits 126 (cannot
execute), 127 (not found) or 125 (other), as `env(1)` does.

## Completion, on the client

`envelope.py` holds one filter per stream. Each runs three phases:

- **Seeking.** Search for the start marker, dropping everything before it and keeping only a tail of
  marker length across chunks. The stderr filter also notes, without retaining text, whether the
  dropped bytes contained `Host key verification failed`. This is the single OpenSSH token the
  client reads (HLA D4).
- **Passing.** Forward payload bytes at once, holding back only the longest suffix that is a proper
  prefix of the closing prefix (`0x1E`, nonce, space). No timer is involved.
- **Done.** After the closing line, drop everything. stdout closes with `end`, stderr with `exit N`
  or `refused CODE`.

The classifier combines both filters with ssh's own exit status:

| Evidence                                                          | Completion                                                                     |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `end` on stdout and `exit N` on stderr                            | `Completed(N)`; ssh's own status is ignored                                    |
| `end` on stdout and `refused CODE` on stderr                      | `NotDelivered(REFUSED, CODE)`                                                  |
| No start marker, ssh exit 127                                     | `NotDelivered(PYTHON_MISSING)`                                                 |
| No start marker, ssh exit 0 to 254 otherwise                      | `NotDelivered(LAUNCHER_FAILED)`                                                |
| No start marker, ssh exit 255, a signal, or a status above 255    | `Unknown(CONNECTION)`, or `Unknown(HOST_KEY)` when the token was seen on a 255 |
| A start marker, but no `end` or no trailer at exit or end of file | `Unknown(TRUNCATED)`                                                           |
| Deadline reached, or a stream over its capture limit              | `Unknown(DEADLINE)`, `Unknown(OUTPUT_LIMIT)`                                   |
| `Popen` raised                                                    | `NotDelivered(CLIENT_START)`                                                   |

- **Ending the client.** Once both streams are done the client closes ssh's stdin, waits up to one
  second for a natural exit and kills it. It never waits for end of file or on a timer, so a
  background child holding the channel cannot stall it. If one stream is done and the other is not,
  it keeps pumping until the other completes or both reach end of file.
- **No pre-marker text is kept.** `NotDelivered` and `Unknown` carry reasons, never stream text:
  with no start marker, the stderr may be a traceback quoting request bytes a startup file left
  unread. ssh's own diagnostics are therefore not shown for `run` and `stream`; the operation log
  records the ssh argv, which has no request data, so an operator can reproduce a failure by hand.

## Terminal handover

`attach(request)` does, in order:

1. Build the `tty` envelope for the request, with `term`, and send it to a first connection as
   `mode: "park"` (argv shape `run`, stage 0 with no argument).
2. agw-launch, in `park` mode, creates `tempfile.mkdtemp(prefix="agw-handover-", dir="/tmp")` (mode
   0700; `TMPDIR` is ignored under `-I`), binds a Unix stream socket `s` in it under umask 077, and
   listens. It writes the start markers and `<M>parked <dir>` to stdout.
3. The client reads that line from the parking connection, checks `<dir>` against
   `^/tmp/agw-handover-[A-Za-z0-9_]{8}$` (it goes into a remote command line), then, inside
   `with guarded_terminal() as guard:`, runs the terminal half. Its stage 0 gets `<dir>/s` as
   `sys.argv[1]`, connects, reads the launcher frame and the envelope frame from the socket, and
   runs agw-launch in `tty` mode.
4. The parked launcher waits with `select` on the listener and on its own stdin, for at most 30
   seconds. On a connection it sends the launcher frame and the envelope re-serialized with
   `mode: "tty"`, closes the connection, then writes `end` and `exit 0`. On stdin end of file (the
   client closing its pipe) or the timeout it writes `end` and `refused handover`. In every case a
   `finally` removes the socket and the directory, so a failed second connection leaves nothing.
5. After the second connection returns, still inside the guard, `guard.clean_exit = code == 0`.
   After the guard, a code of 255 prints the notice "connection ended or the command exited 255;
   terminal restored" with the existing detail line (I6), reimplemented in `attach.py` because the
   legacy helper is `transports/ssh.py:77`. The client then closes the parking connection's stdin,
   pumps it for up to two seconds and ends it.

The parking connection fails first on a missing `python3`, a trust mismatch or a refused connection,
so `attach` raises `VMNotReady`, `TrustMismatch` or `ExecutionIncomplete` before the terminal is
touched. While the terminal half runs, the parking connection's pipes go unread; it writes a few
lines, far below a pipe buffer. A failure of only the second connection returns its code; with the
socket gone the operator sees Python's traceback on the terminal and code 1, which is accepted for
that rare race. `socket` has no `SO_PEERCRED` check: the directory is 0700, and the account's own
processes can already read its processes (HLA D8).

## Forward

`access.forward(forwards, verbose=False)` is a context manager over
`Forward(local_port, remote_port, bind_address="localhost")`. It spawns the forward argv above with
stderr captured (inherited under `verbose`) and then waits for readiness:

- **Ready** is a TCP connect to `bind_address` (loopback for an unspecified address) on each local
  port succeeding, polled every 50 ms for up to 60 s. Each probe opens one forwarded channel that
  closes at once, a side effect the HLA accepts by choosing this signal.
- **Not ready** is the client exiting first (`ExitOnForwardFailure=yes`, a refused connection) or
  the 60 s elapsing. `ForwardFailed(ConnectivityError)` carries the client's stderr, which holds no
  request data, and a seen `Host key verification failed` raises `TrustMismatch` instead. **HLA
  amendment proposed:** D13 gains `ForwardFailed`.
- **The handle** has `wait() -> int` (polling `Popen.wait(timeout=0.2)` so Ctrl-C is delivered) and
  exit ends the client: terminate, one second, kill.
- **Signals.** The library installs none. `port_forward_vm` installs a `SIGTERM` handler that raises
  `KeyboardInterrupt` for the duration of the forward and restores the previous handler (today's
  handlers are never restored, `tailscale.py:180-181`). An operator stop returns 0; any other end
  returns the client's status. The legacy returned the terminated client's status (`-15` on POSIX).

## Errors

| Error                 | Kind                | Raised for                                                                     | PR A hint                                                                   |
| --------------------- | ------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `ClientUnsupported`   | `ConfigError`       | No acceptable OpenSSH, before dispatch                                         | The install guidance in the message                                         |
| `VMNotReady`          | `StateError`        | `NotDelivered(PYTHON_MISSING)` (an unelevated 127)                             | `agw vm reinit <vm>`, whose Phase A still runs without python3              |
| `TrustMismatch`       | `StateError`        | `Unknown(HOST_KEY)`, and the forward's token                                   | Verify the new key, remove the store file, `import-host-keys --known-hosts` |
| `ExecutionIncomplete` | `ConnectivityError` | Any other `NotDelivered` or `Unknown`; carries the `Result` and names which    | A `NotDelivered` command did not start; an `Unknown` one may have run       |
| `CommandFailed`       | `ExternalError`     | `check=True` with a non-zero status; message has the status, never stream text | none                                                                        |
| `ForwardFailed`       | `ConnectivityError` | A forward that never became ready                                              | none                                                                        |

**HLA amendment proposed:** `TrustMismatch`'s hint cannot name `agw vm recover` in PR A, which lands
in slice 4 (PR B). Until then it names the manual path, since import never rewrites an existing
record (`trust-lld.md`). The hint is one constant slice 4 changes. `TrustMissing` already exists
(`errors.py`). `PermissionDenied` and `FeatureUnavailable` land with their first consumers. No error
message, `repr` or hint holds a stream, an environment value, script text or stdin.

## The operation log

`oplog.LOG_DIR = CONFIG_DIR / "logs"` is the one constant, the same directory as today (`ssh.py:89`,
`cli/_errors.py:57`). `SSHLogger` and every reader read it through the module (`oplog.LOG_DIR` at
call time) so one test patch reaches them all.

- **Readers moved:** `doctor_state.py:124`, `vms/manager/_helpers.py:205`, `lifecycle.py:99`,
  `power.py:323` and `cli/commands/vm.py:441`. `ssh.py` drops its constant and `SSHLogger`
  (`ssh.py:99`) reads `oplog.LOG_DIR`. The session fixture at `tests/conftest.py:83` patches
  `oplog.LOG_DIR`, and so do the eight test files that patched `agentworks.ssh.LOG_DIR`.
- **`OperationLog(vm_name, stem, redactions=())`** writes `<vm>-<UTC timestamp>-<stem>.log` in that
  directory, so the existing globs (`<vm>-*.log`, `<vm>-*-vm-create.log`) keep matching. It records,
  per operation: the label the caller gives; the request shape (kind, flags, the names of the
  environment variables, `cwd`, input size, `sensitive`, deadline, the ssh argv without request
  data); the outcome (`completed N`, `not delivered: REASON`, `unknown: REASON`) with elapsed time;
  and the streams after redaction, decoded with `errors="replace"`. It never records argv values,
  environment values, script text or stdin.
- **Redaction** copies `SSHLogger`'s: each secret in its raw and `shlex.quote`d forms, longest
  first. A sensitive stdin keeps both streams out entirely.
- **Opt-in.** `vm_access(..., log=None)`; a log only goes to an operation whose caller passes one.
  Today's `vm exec`, `shell` and `verify-connection` write none, and a log from them would also
  displace the create log that the readers above show newest-first. The first writer is slice 3's
  create. Until then the only consumers are the conformance vectors. This is the one piece of slice
  2 with no production caller; the plan lists it for this slice.

## The migrated commands

All six keep their guards, workspace and secret resolution and the gate. Each gains
`require_vm_access_boundary` before the gate and builds
`access = execution.vm_access(vm, config, Admin() or Agent(agent))`, called through the package
attribute so one patch reaches every manager.

| Command                                       | Request and call                                                                                                                                                                        |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vm exec` (`exec.py:182`)                     | One argument: `Script(arg, Shell.USER_DEFAULT)`. Several: `Command(tuple(args))`. `cwd=ws.workspace_path`, `env=compose_env(...)`. `access.stream(...)` with the process's binary stdio |
| `agent exec` (`access.py:134`)                | `Script(source, Shell.USER_DEFAULT, login=True)`, with `source` the one argument or `shlex.join(args)`, as `$SHELL -lc` does today                                                      |
| `vm shell` (`exec.py:35`)                     | `LoginShell(cwd, env)` and `access.attach`                                                                                                                                              |
| `agent shell` (`access.py:34`)                | The same with the agent's access, after the agent probe                                                                                                                                 |
| `vm verify-connection` (`verification.py:28`) | `run(Command(("true",), deadline=10), check=True)`. The message keeps "via ssh" (the kind is now the constant `ssh`)                                                                    |
| `vm port-forward` (`tailscale.py:73`)         | Port parsing and guards as today, then `access.forward(...)` and the signal handling above                                                                                              |

- **Splits for the static check.** `exec.py` keeps `exec_vm` and the default `shell_vm`; the
  `--platform` branch (`exec.py:72`, `:162-165`) moves to `platform_shell.py`, still on the legacy
  `native_transport` and `gated_vm_platform_recovery_boundary` until slice 4, and `shell_vm`
  dispatches to it. `vm shell --platform` uses `native_transport`, and `native_shell` is slice 4's.
  `port_forward_vm` moves from `tailscale.py`, which keeps `_ensure_tailscale`'s legacy imports.
- **Agent probe.** `_assert_agent_ssh_works` (`agents/manager/_common.py:237`) takes a legacy
  `Transport`, and the sessions and agent-create paths still call it. A new
  `_assert_agent_access_works` beside it serves `agent exec` and `agent shell`: `Completed(any)`
  passes; `NotDelivered(PYTHON_MISSING)` is `VMNotReady`; `Unknown(HOST_KEY)` is `TrustMismatch`;
  any other `NotDelivered` or `Unknown` runs the same `true` as the admin. If that completes, the
  agent predates the rollout (the existing `StateError` and hint); otherwise the VM is unreachable
  (the existing `ConnectivityError`). This keeps incident row I19 without reading ssh diagnostics,
  so **I19 is also re-proved in slice 2** (inventory amendment proposed).
- **Behavior changes** for release notes and the 0.20 guide:
  - OpenSSH 8.5 or newer is required for these six commands, and Windows needs the native build.
  - A VM without `python3` is `VMNotReady`, not a shell error.
  - The guest sees a fixed `PATH`, `LANG=C.UTF-8`, `SHLVL=1` and no `SSH_CLIENT` or
    `SSH_CONNECTION`.
  - A literal `vm exec` command no longer inherits the account shell's startup, and a missing
    workspace directory is a not-delivered error, not a shell message.
  - `vm shell` without a command stops showing the message of the day and last-login line, because
    sshd prints them only when it runs the login shell itself.
  - `vm port-forward` exits 0 on an operator stop.
- **Template `shell` fields** (`vms/admin.py:99`, `agents/template.py:101`): the type becomes
  `Literal["sh", "dash", "bash", "zsh"]` (`ACCOUNT_SHELLS`, defined in `launcher.py` beside the line
  that needs them), with the allowed values in the field docstrings, which the generated samples
  render (`manifests/samples.py:57`). A path such as `/usr/bin/zsh` is refused, so the dead
  `"/" in shell` branch of `agents/initializer.py:65` goes. `ResolvedAgentTemplate.shell`
  (`agents/templates.py:33`) takes the same type. Existing accounts are not re-checked until slice
  3's base check.

## Guards, fakes and tests

- **Blocker** (`tests/execution/conftest.py`). An autouse function fixture removes the six legacy
  modules and their submodules from `sys.modules`, deletes the attribute on each parent package (so
  `from agentworks import ssh` also fails), and puts a `MetaPathFinder` that raises `ImportError`
  first on `sys.meta_path`. It uses `monkeypatch` so every test restores them. The session fixture
  that imported `agentworks.ssh` is gone (`oplog.LOG_DIR`), and `test_independence.py` still proves
  import time in a fresh interpreter.
- **Static check** (`tests/test_execution_import_boundary.py`), modeled on
  `tests/test_registry_warning_boundary.py:19-50`. It parses each file in `MIGRATED_FILES` and fails
  on any `Import` or `ImportFrom` of a legacy module, anywhere in the file (top level, in a
  function, under `TYPE_CHECKING`), including `from agentworks import ssh`, relative forms and the
  `import agentworks.ssh as x` aliases. Slice 2's files: `vms/manager/exec.py`,
  `vms/manager/verification.py`, `vms/manager/port_forward.py` and `agents/manager/access.py`. The
  `execution/` modules are covered by the package guard. Later slices append.
- **`RecordingVMAccess`** (`tests/vm_access_support.py`) subclasses `VMAccess`, overrides the four
  operations to record `(operation, request, input)` and return a canned `Result` or raise, and
  needs no ssh. A fixture patches `agentworks.execution.vm_access` to return it. Manager tests run
  on every CI host with it.
- **Unit tests**, none restating implementation or prose:
  - envelope round trip over all bytes `0x01` to `0xFF` as values, and each refusal;
  - the filters under every split of a stream and every interleaving of the two, including a payload
    ending in the closing prefix's first bytes;
  - the classifier table, row by row;
  - `ssh -G` against the installed client, with the policy argv: `-T` beats `RequestTTY force`,
    identity and trust options take effect, no multiplexing, keepalives on, and quoting holds for an
    identity path with a space; `VM` trust with no record spawns nothing;
  - the resolver against temporary executables, including an empty and a relative `PATH` element;
  - stage 0 is one line with no `'` and no `!` and under 256 characters, and the launcher parses at
    3.11 and imports only the standard library;
  - passivity: `vm_access` on a VM with no address and a missing key file raises nothing and spawns
    nothing;
  - the operation log: redaction, no payload fields, a sensitive stdin leaves no stream;
  - error messages and `repr` hold no stream, environment value or stdin;
  - the pump (the adapted proof cases), `@pytest.mark.windows` for binary stdio and end of file;
  - doctor's `ssh` row; invalid template shells refuse at config load; the six commands refuse an
    unsupported client before the first prompt.

### Conformance vectors

One list of vectors, three carriers. **L** runs `$SHELL -c <remote command>` locally through the
same pump and envelope, with the account's startup files set per vector and `SSH_CLIENT` exported as
sshd does; it covers the shells and runs on every Linux CI host. **S** runs the real client against
a rootless local `sshd` (`ListenAddress 127.0.0.1`, a temporary host key and `AuthorizedKeysFile`,
`UsePAM no`, `StrictModes no`) and `trust.write_records` for the test host key; it skips when no
`sshd` binary exists, and the CI job installs `openssh-server` if the runner image lacks it. **V**
is the live lane, `-m integration` with `AGW_CONFORMANCE_VM`, run by the tester from each
workstation. `zsh` and `dash` are required on CI (a missing shell fails there, skips locally).

| Vector                                                                                                                         | Lanes  |
| ------------------------------------------------------------------------------------------------------------------------------ | ------ |
| Literal argv: empty, whitespace, shell characters, dash-led items and `--` (I5)                                                | L S V  |
| Script versus stdin; script text absent from the interpreter's `/proc/<pid>/cmdline`                                           | L S V  |
| Interpreter and startup: `sh`, `bash`, `user_default`, each with and without login                                             | L V    |
| Environment: every byte `0x01` to `0xFF`, `%`, quotes, `${HOME}`, CR, LF, a 1 MiB value                                        | L S V  |
| Working directory, and a missing one as `refused cwd`                                                                          | L V    |
| Exit 0, 1 and 255; death by signal as 128 plus the signal                                                                      | L S V  |
| Stage 0 under `sh`, `dash`, `bash`, `zsh` with startup files that print and export                                             | L      |
| Output before the start marker dropped, with and without a trailing newline                                                    | L      |
| `NotDelivered` versus `Unknown`: no python3 (127 as `VMNotReady`), a startup `exit 0`, a startup `exit 255`, a killed launcher | L      |
| A startup file that reads stdin, and one that redirects a standard stream, as not delivered; no stream text in the error       | L      |
| Signal dispositions and descriptors reset; a startup `TERM` or `XDG_RUNTIME_DIR` leaves the payload's values                   | L      |
| Exact stdin positioning: the payload reads the caller's first byte                                                             | L S V  |
| A bash child of the payload reads no account startup file; stdin a pipe, and in L a socket (I11)                               | L S V  |
| Background child holding both streams: prompt completion, status correct                                                       | L S V  |
| Payload stdout with no trailing newline; a `refused` trailer                                                                   | L S    |
| Deadline: `Unknown(DEADLINE)` at the deadline, client reaped                                                                   | L S    |
| Handover: success, the 30 s timeout, second connection never arrives, nothing left behind                                      | L S V  |
| Host key mismatch is `TrustMismatch`                                                                                           | S      |
| Sensitive stdin absent from the log and exceptions                                                                             | L      |
| Binary stdin and stdout, from Windows (`windows` mark) and live                                                                | pump V |

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
| I2  | Binary pipes and `.buffer`; the all-bytes vectors; the Windows-marked pump test; live from Windows                                                   |
| I3  | The V lane drives real guests from each workstation; the resolver refuses MSYS builds, so no Git for Windows tool is in the path                     |
| I4  | No API combines input with a terminal; value-free errors tested                                                                                      |
| I5  | Literal argv and the `--` before the destination; the dash-led vector                                                                                |
| I6  | `attach` tests with a fake terminal check the notice comes after the guard; live                                                                     |
| I7  | `ssh -G` shows the keepalive and connect options; one live cell drops the network under a `stream`                                                   |
| I11 | The vector, with a pipe and a socket on stdin                                                                                                        |
| I12 | `ssh -G` shows no multiplexing under a hostile configuration                                                                                         |
| I13 | `ssh -G` shows the explicit identity file; live with a key named other than the default on Windows                                                   |
| I14 | `ssh -G` of the pipe-mode argv shows no pty even with `RequestTTY force` supplied                                                                    |
| I19 | The agent probe (above)                                                                                                                              |
| I21 | The terminal guard tests, and live attach killed mid-session                                                                                         |

## Live evidence

The plan's cells: all three workstations against Remote Lima, Linux and macOS against local Lima,
Windows against WSL2, and one cloud, with a Debian 12 and a Debian 13 guest and an OpenSSH 8.5
client on one workstation (else the operator's disposition). For each, the V vectors and these:

- `-F none` and the stdin protocol on 8.5 and on Win32-OpenSSH, including the stage 0 quoting round
  trip through `ssh.exe`;
- `vm exec` from Windows with binary stdin and early exit;
- an attach from each workstation, killed mid-session, and a handover whose second connection fails;
- a forward with a busy local port, and Ctrl-C on a live forward.
