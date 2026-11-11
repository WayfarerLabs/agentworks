# Transport Take 2: Workspaces, Sessions and Backup Low-Level Design (Slice 7)

- Status: Draft for the private design review
- Date: 2026-11-10
- Architecture: [hla.md](./hla.md) D5 (requests and results), D6 (files), D7 (detached work), D8
  (streaming, attach and the terminal handover), D13 (errors and the operation log), D14 (the
  guards), the Migration section and the incident inventory; requirements: R2, R3, R4, R6, R10, R11
- Plan: [plan.md](./plan.md), slice 7, delivered in PR C
- Source baseline: `c84250663` (the PR C branch head). Every `path:line` is at that commit. Paths
  are under `cli/agentworks/` unless they start with `cli/` or `docs/`.

## Scope

This LLD settles slice 7: `detached.py` and backup on it; the `observation` request option, the
`FromFile` input, the `ToFile` sink and the two terminal additions the consumers need
(`TerminalCommand` and `park`); every workspace, session, console and backup request that moves;
session environment in the request and the terminal handover on every console pane path; the
deletions that become possible (the environment newline refusals, the console's environment carry
across sudo and its sudoers grant, `_assert_agent_ssh_works`, the last caller of `remote_exec.py`);
and the incident audit of the entry points that move.

**Operator direction applied.** Every operation and option below has a caller in this slice.
Unsupported setups are refused with an alert rather than handled by new machinery (HLA, operator
decision 10), and related problems found on the way are fixed here (FRD, 2026-11-08).

**What exists at HEAD.**

- Workspaces already apply ACLs and remove a rolled-back tmuxinator file through `VMAccess`
  (`workspaces/backends/vm.py:195-196`, `:214-215`, `workspaces/manager/copy.py:206-207`,
  `rehome.py:210`, `:264`, `repair.py:220`); every other workspace request is on the legacy
  `transport()`.
- Sessions set up their socket directories and clean stale sockets through `VMAccess`
  (`sessions/tmux.py:274-477`, slice 6), and preflight, artifacts and group membership already run
  on it (`sessions/manager/_pids.py:91-119`, `_create_roll.py:215-216`, `:320`, `_queries.py:212`,
  `:225`). Everything else in `sessions/` and all of `sessions/multi_console/` and
  `multi_console_layout.py` is on the legacy stack.
- Backup runs on the legacy `SSHTransport`, its archive through `remote_exec.run_detached`
  (`vms/backup.py:357-379`), its only caller, and its download through a hand-built `scp`
  (`vms/backup.py:454-518`).
- The legacy transport's `retries=` is an attempt count (`ssh.py:474`), and every caller in this
  slice's modules passes 1, its default (`ssh.py:336`): `GUEST_OBSERVATION_ATTEMPTS`
  (`status_observation.py:14`) and `DEDICATED_TEARDOWN_ATTEMPTS`
  (`sessions/manager/_teardown.py:30`), from main's #776 and its predecessors. They pin "no retry"
  against a transport whose default once retried; they are not a retry policy.

**Callers outside the plan's first list.** The definition of done (no workspace, session or backup
module imports a legacy module) reaches three callers the plan does not name:

- `agents/manager/lifecycle.py:389` tears down an agent's sessions through `_teardown_session` with
  a legacy target. It moves with `_teardown_session` (batch C).
- `agents/manager/__init__.py:34` re-exports `transport` only for that call; it goes with it.
- `vms/manager/_helpers.py:306`, `vm describe`'s live resources, is a read-only probe with
  `retries=3` and no bound. It is the one remaining legacy caller outside this slice's modules, and
  an observation by definition, so this LLD proposes to move it in batch A as the option's third
  caller, leaving slice 8 nothing to migrate. It is a scope addition the lead decides (decision 1).

The session, workspace and backup modules also construct `SSHLogger`
(`sessions/manager/_create.py:138-140`, `_scope.py:124`, `_lifecycle.py:499`, `:695`,
`workspaces/realize.py:124`, `manager/copy.py:160`, `delete.py:91`, `rehome.py:208`,
`vms/backup.py:82`). They take it from `vms.manager.operation_log` instead (below, "Operation
logs"), so none of them imports `agentworks.ssh`; the logger itself and `LOG_DIR` stay slice 8's.

**Out of this slice.** The `AcceptEnv` and `env_keep` fragments of ADR 0014 (slice 8's search), the
operation log's move to `execution/oplog.py` (slice 8), and deleting the legacy modules themselves
(slice 8; `remote_exec.py` has no caller after batch A, decision 2).

## Module layout

| Module                                                                                                              | Change                                                                                                                                                                                                              |
| ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `execution/requests.py`                                                                                             | `observation` on `BoundedRequestOptions`; `FromFile`; `ToFile`; `TerminalCommand`                                                                                                                                   |
| `execution/results.py`                                                                                              | `CONNECTION_OUTCOMES`, moved from `reachability.POLLED` (`reachability.py:35`), which then imports it                                                                                                               |
| `execution/access.py`                                                                                               | `run` re-dispatches an observation, accepts `FromFile` and `ToFile`; `attach` takes `LoginShell \| TerminalCommand`; `park`; `detached()`                                                                           |
| `execution/attach.py`                                                                                               | The parking half becomes `Parked`, which `attach` and `park` share                                                                                                                                                  |
| `execution/envelope.py`, `execution/agw_launch.py`                                                                  | `TerminalCommand` in `tty` mode; a handover with no `term` takes `TERM` from the terminal's owner                                                                                                                   |
| `execution/pump.py`                                                                                                 | A file sink with a progress callback; nothing else (`FromFile` feeds the existing input thread)                                                                                                                     |
| `execution/detached.py` (new)                                                                                       | `Detached`: `launch`, `observe`, `interrupt`, `download`, `discard`                                                                                                                                                 |
| `execution/detached_program.py` (new)                                                                               | The guest program those requests run, a `PythonProgram`, as `file_program.py` is                                                                                                                                    |
| `vms/manager/_helpers.py`                                                                                           | `_operation_log` becomes public `operation_log(..., path=None)`, re-exported by `vms.manager`; `_query_live_resources` on `VMAccess` (decision 1)                                                                   |
| `vms/backup.py`                                                                                                     | On `VMAccess`, `detached` and `ToFile`; no thread, staging, `chown` or `scp`                                                                                                                                        |
| `workspaces/backends/vm.py`, `workspaces/manager/*.py`, `workspaces/realize.py`                                     | On `VMAccess`; copy is two `run`s over a tar stream; `tmuxinator.py` writes and removes through the admin's access                                                                                                  |
| `sessions/tmux.py`                                                                                                  | Every primitive takes a `RunCommand` built by `access_run_command`, which gains `observation`; `_tmux_env_flags` goes; `deploy_restricted_config` takes `Files`; the fingerprint and launch reads take `RunCommand` |
| `sessions/manager/*.py`                                                                                             | On the owner's, the admin's or the elevated admin's access; `_BoundedStatusTarget` goes                                                                                                                             |
| `sessions/multi_console/*.py`, `sessions/multi_console_layout.py`                                                   | On the admin's access; `_split_shell_pane` parks a handover per pane; the `--preserve-env` probe, its memo and `PreserveEnvMemo` go                                                                                 |
| `agents/manager/lifecycle.py`, `agents/manager/__init__.py`, `agents/manager/_common.py`                            | Agent delete's session teardown on the admin's access; the `transport` re-export and `_assert_agent_ssh_works` go                                                                                                   |
| `plugins/codex/recorder.py`, `plugins/codex/harness_integration.py`                                                 | The recorder is published with `Files.write` in `start`; `provision_fragment` goes                                                                                                                                  |
| `vms/initializer/shell_env.py`, `driver.py`                                                                         | `_write_sudoers_console_setenv` becomes `_remove_sudoers_console_setenv`                                                                                                                                            |
| `manifests/decode.py`, `env/compose.py`, `env/show.py`, `secrets/orchestration.py`, `resolver.py`, `line_safety.py` | The environment newline refusals go (below, "Deletions")                                                                                                                                                            |
| `status_observation.py`                                                                                             | `GUEST_OBSERVATION_ATTEMPTS` goes                                                                                                                                                                                   |

## Requests this slice adds

### The `observation` option

`BoundedRequestOptions` gains `observation: bool = False`: the caller's declaration that the request
only reads, so it may be dispatched again. It is the only way a request is ever dispatched twice
(HLA D5, R4).

- **The rule.** `run` dispatches an observation again only when its completion is in
  `CONNECTION_OUTCOMES` (`Unknown(CONNECTION)`, `Unknown(TRUNCATED)` and `Unknown(DEADLINE)`): the
  outcomes that waiting can fix, the set `reachability.wait` already polls (`reachability.py:35`).
  It pauses `OBSERVATION_PAUSE_SECONDS = 1` between dispatches and gives each one the time left of
  the request's `deadline`, which bounds the whole observation, not each dispatch. Once less than
  the pause remains, the last result stands. Every other `NotDelivered` and `Unknown` is returned at
  once: a missing python3, a launcher failure, a refusal, a host-key mismatch, an authentication
  failure and an output limit are the same on the next try, and repeating an authentication failure
  draws Debian 13's `PerSourcePenalties` (`reachability.py:4-8`).
- **Who may declare it.** Any caller of `run`, for a request whose payload changes nothing on the
  guest. An observation must carry a `deadline`; `run` refuses one without (`ValidationError`,
  before anything resolves), since an unbounded observation would repeat forever. `stream` refuses
  `observation=True`, since it delivers output as it arrives and cannot take a second copy back.
  Each dispatch is logged as its own record, so the log shows every attempt.
- **What it is not.** It is not a retry of failure: a `Completed` with any status is final, as is a
  completion outside the set. `VMAccess` still never re-dispatches a request whose caller did not
  declare it.
- **First callers.** `detached.observe` (below); the session and console status probes and the
  pid-repair reads, each bounded by `GUEST_OBSERVATION_TIMEOUT_SECONDS` (10,
  `status_observation.py:13`) through `access_run_command(..., observation=True)`; and
  `vm describe`'s live resources, bounded at 30 seconds (decision 1).

**The legacy `retries=` arguments.** Each is absorbed or retired, never ported:

| Today                                                                                                                | Becomes                                                                                                                                                                                                                                                                                                                                                                             |
| -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GUEST_OBSERVATION_ATTEMPTS` (`sessions/manager/_status.py:52-53`, `:243-244`, `multi_console/_status.py:59`)        | Absorbed: those reads are observations within the same 10-second deadline, so a dispatch that fails fast is tried again inside the bound #776 chose. The constant goes                                                                                                                                                                                                              |
| `DEDICATED_TEARDOWN_ATTEMPTS` and `transport(vm, config, default_timeout=10, retries=1)` (`_lifecycle.py:1053-1057`) | Retired: `VMAccess` never re-dispatches, which is what 1 meant. Teardown's requests are not observations either, deliberately: `68c837c1f` rules out repeating a kill or an uncertain observation there, and its own bounded absence poll (`_teardown.py:256-265`) is the only repetition. The constant goes; `DEDICATED_TEARDOWN_TIMEOUT_SECONDS` stays as each request's deadline |
| `retries=3` with no bound (`vms/manager/_helpers.py:332`)                                                            | Absorbed: one observation with a 30-second deadline (decision 1)                                                                                                                                                                                                                                                                                                                    |
| The legacy transport's `retries` parameter itself (`transports/base.py:66`, `ssh.py:416`)                            | Deleted with the module in slice 8; after this slice nothing passes it                                                                                                                                                                                                                                                                                                              |

### `FromFile`

`FromFile(path: Path)` joins `NoInput | Bytes` as a `stdin` option. The client opens the file in
binary mode at dispatch and hands it to `PipeSession` as its input stream, which the existing input
thread already copies to ssh's stdin until end of file (`pump.py:121-160`, `:372-400`); the pump
needs no change. Nothing bounds it but the file. `run` checks it is a `Path` before anything
resolves (a plugin may pass one, `access.py:303-316`), and a file that cannot be opened is an
`OSError` before dispatch. The content, like `Bytes`, never reaches a log or an error. First
consumer: workspace copy.

### `ToFile`

`ToFile(path: Path, on_progress: Callable[[int], None] | None = None)` joins `CaptureOutput` and
`DiscardOutput` as a `run` sink.

- `run` creates the file with `O_CREAT | O_EXCL` and mode 0600 before dispatch, so an existing path
  is a `ValidationError` with nothing sent, and writes each payload chunk to it as the pump delivers
  it. `on_progress` is called with the running byte count after each write; callers throttle their
  own reporting.
- **The file exists afterwards only for `Completed(0)`.** For any other completion, and for any
  exception raised while the request runs (an operator's Ctrl-C included), `run` closes and removes
  it before returning or re-raising. That is incident row I20's rule, moved from backup's own `scp`
  wrapper (`vms/backup.py:508-518`) into the sink, so no caller rewrites it.
- The `Result` field for that stream is empty, as for `DiscardOutput`; the bytes are in the file.
  `log_streams` is irrelevant to it, since the log never sees a sink's bytes.

First consumers: backup's download and workspace copy's source read.

### `TerminalCommand`

`TerminalCommand(argv: tuple[str, ...])`, with `env` and `cwd`, is a literal argv run on the
operator's terminal: valid only for `attach`, as `LoginShell` is. `attach` takes
`LoginShell | TerminalCommand`. In `tty` mode agw-launch execs it with its `argv[0]` searched in the
rebuilt `PATH`, after the same resets as a login shell (`agw_launch.py:163-183`), and refuses as it
does (127 for `exec ENOENT`, 126 for any other `exec` errno, 125 for `cwd`); there is no memfd, so
the descriptor close is harmless. The envelope accepts a handover token and `term` for it as for
`LoginShell` (`envelope.py:102-110`). First consumers: `session attach`
(`sessions/manager/_queries.py:961`) and `console attach` (`multi_console/attach.py:830`), which
today hand a command string to `Transport.interactive`.

### `park`: a handover for a pane

A console pane is a terminal agw does not hold: tmux creates it on the VM. It needs the parking half
of an attach without the terminal half.

```text
with access.park(LoginShell(cwd=workspace, env=pane_env), term=None) as handover:
    admin.run(Command(("tmux", "split-window", ..., handover.command(run_as="agent-x"))))
# leaving the block waits for the pane to collect its request
```

- **Shared shape.** `attach.py`'s parking connection (`attach.py:65-70`) becomes `Parked`, a context
  manager over the `PipeSession` that waits for `parked` within `PARKING_WAIT_SECONDS` and raises
  the typed error, before anything else happens, when it does not come. `attach` is `Parked` plus
  the terminal half. `park` is `Parked` plus a `Handover`.
- **`Handover.command(run_as: str | None = None) -> str`** is the pane's command line: the handover
  form of stage 0, `stage0_command(socket_path(token))` (`ssh_process.py:73-86`), and, when the
  pane's server belongs to another account, `sudo -n -u <run_as>` in front. Its only values are the
  token and an account name; the environment is in the parked request.
- **Leaving the block.** On a normal exit the client keeps pumping the parking connection until its
  trailer, for at most `HANDOVER_COLLECT_SECONDS = 35` (the launcher's 30-second wait,
  `agw_launch.py:60`, and a margin), and sets `handover.collected` from it: `exit 0` is collected,
  `refused handover` is not. On an exception (the split failed) it closes the parking stdin at once,
  which the launcher treats as the client leaving and cleans up (`agw_launch.py:290-299`). Either
  way the socket directory is gone afterwards.
- **`TERM`.** An attach sends the operator's `TERM`, because the workstation owns that terminal. A
  pane's terminal belongs to tmux, which sets `TERM` in the pane's environment from its
  `default-terminal`, and sudo keeps `TERM` through its `env_reset`. So `park` takes `term=None`,
  the envelope carries no `term`, and in `tty` mode agw-launch takes `TERM` from its inherited
  environment when, and only when, the envelope has none. Today's panes get exactly that value,
  since `sudo --login` keeps it too. HLA D4 says `TERM` comes from the envelope; that sentence needs
  the pane case (recommended HLA edit 1).

`park` is valid for `LoginShell` only, since every pane runs a shell. An agent-bound access parks
for an agent pane, so the socket directory belongs to the agent and the pane's `sudo -n -u <agent>`
stage 0 is the only process that can connect to it.

## Detached work: `detached.py`

### API

`VMAccess.detached() -> Detached`, passive like `files()`. Its operations, each one `run` of
`detached_program.py` as an elevated `PythonProgram` with the operation as its first argument:

| Operation                                    | Returns                                                | Request                                                                 | Deadline | Observation |
| -------------------------------------------- | ------------------------------------------------------ | ----------------------------------------------------------------------- | -------- | ----------- |
| `launch(command: Command) -> Launch`         | `Launch(vm_name, id)` once the payload has started     | `("launch", id)`; stdin: the payload's spec, then its own `stdin` bytes | 60       | no          |
| `observe(launch, offset) -> Observation`     | state, status, new output from `offset`, `work/` sizes | `("observe", id, offset)`                                               | 30       | yes         |
| `interrupt(launch) -> Observation`           | the state after stopping                               | `("interrupt", id)`                                                     | 30       | no          |
| `download(launch, name, to: ToFile) -> None` | the file, through the sink                             | `("download", id, name)`; stdout to `to`                                | none     | no          |
| `discard(launch) -> None`                    |                                                        | `("discard", id)`                                                       | 60       | no          |

`Observation(state, status, output: bytes, offset, work: Mapping[str, int])`, with `state` one of
`RUNNING`, `EXITED`, `LOST` (no status was recorded and the process is gone, as a reboot leaves it)
and `ABSENT` (no such launch). A non-zero program status raises a typed error: `DetachedRefused` (a
`StateError`) for an existing id at launch, a running launch at discard, or a `work/` name that is
not a regular file; `CommandFailed` otherwise.

`launch` takes a `Command` with `elevated=True` and no `deadline`: detached work runs as root, under
the root-owned base, and is bounded by its observer, not by a deadline. Anything else is a
`ValidationError` before dispatch, since a plugin holding an admin access can reach it. Only
`Command` is accepted because backup, the only caller, runs one argv; a `Script` would need its
source staged on disk, which waits for a caller that needs one. An agent's access refuses the
elevated request with `PermissionDenied`, as for any request.

### The base directory and its modes

Every launch lives in `/var/lib/agentworks/detached/<id>/`. The program creates the base lazily on
first launch, as the file program creates its lock (HLA D6): `/var/lib/agentworks` 0755 and
`detached` 0700, both root's, each created with `mkdir` and then checked through a descriptor opened
with `O_DIRECTORY | O_NOFOLLOW`: owner uid 0, a directory, and no group or other write bit (for
`detached`, no group or other bit at all). A base that fails the check is refused
(`DetachedRefused`), never repaired, since only root can have made it so. Nothing under it is
world-writable, and no world-writable directory is on the path, which replaces today's predictable
admin-owned `/var/tmp/_aw_detached_*` and `/var/tmp/agentworks-backup-*` staging
(`vms/backup.py:288`, `:339`, `:352`).

A launch directory holds:

| Entry     | Mode | Written by                     | Content                                                                                        |
| --------- | ---- | ------------------------------ | ---------------------------------------------------------------------------------------------- |
| `stdin`   | 0600 | `launch`, before the payload   | the payload's `stdin` bytes, or empty                                                          |
| `output`  | 0600 | the payload's descriptors 1, 2 | everything the payload writes, both streams in one file                                        |
| `process` | 0600 | the supervisor, by rename      | JSON: `pid`, `boot_id`, `start_ticks`                                                          |
| `status`  | 0600 | the supervisor, by rename      | JSON: `status` (128 plus the signal for a signalled payload)                                   |
| `work/`   | 0700 | `launch`                       | the payload's working directory, unless the command names its own `cwd`; `download` reads here |

### Launch identity

The client chooses the id, `secrets.token_hex(16)`, before dispatch, and the program creates the
launch directory with `mkdir`, which fails on an existing name: an existing directory is
`DetachedRefused`, never adopted. So an independent new launch never matches an earlier one,
whatever its command or paths (R6), which is what `run_detached`'s reuse of a leftover status file
broke (`remote_exec.py:96-103`, `fd220164a`). The guest validates the id as 32 lowercase hex digits,
because a later invocation could supply a recorded one.

A client-chosen id also makes a lost acknowledgement recoverable. `launch` is a mutation and is
never dispatched again; if its outcome is not `Completed`, `launch` makes one `observe` of the id
(an observation, which may be): `RUNNING` or `EXITED` means it did launch, and `launch` returns the
`Launch`; `ABSENT` raises the launch's own typed error, naming the id. The edge it cannot close is a
launch still in flight on the guest when the client gave up, which then starts a payload no one
observes; the prune below removes its directory once it has finished.

### How a launch runs

1. Read the spec (a length-prefixed JSON object: `argv`, `env`, `cwd`) and copy the rest of stdin to
   `stdin`. The environment stays in memory: it reaches the payload's environment and no file.
2. Create the base and the launch directory, and `work/`.
3. Prune: remove every sibling launch directory that has a `status` file older than seven days, or
   no `status` and a `process` whose recorded process is gone and whose directory is older than
   seven days. A killed `agw` leaves a multi-gigabyte archive behind; today's `/var/tmp` staging is
   aged out by Debian 13's 30-day `/var/tmp` cleanup (recalled from its release notes, not measured
   on a VM), and nothing ages `/var/lib`. A running launch is never touched.
4. Fork. The child calls `setsid` and forks again, so the supervisor belongs to no session and no
   terminal and is not agw-launch's child, then points descriptors 0, 1 and 2 at `/dev/null` and
   closes every other one, so it holds nothing of the connection.
5. The supervisor starts the payload with `subprocess.Popen` and `start_new_session=True`,
   `restore_signals=True`, the working directory, `stdin` as its input, `output` as both its
   streams, and the inherited environment plus the spec's. The inherited environment is the one
   agw-launch rebuilt for root (`agw_launch.py:116-138`). The payload's own new session makes its
   pid its process-group id, which `interrupt` signals. It writes `process`, reports `started` (or
   the `exec` errno) to the launching process over a pipe, and waits; on exit it writes `status`.
6. The launching process reads that report and exits: 0 with the `Launch`, or a refusal status that
   names the `exec` failure (`RequestRefused`'s codes). So `launch` returns only once the payload
   has started.

This is incident row I8 as the HLA restates it: no `nohup` and no `sudo` inside the wrapper, because
the whole operation is elevated (`sudo -n` is stage 0's prefix) and `setsid` with no controlling
terminal leaves nothing to deliver `SIGHUP`; the old `nohup` ordering (`remote_exec.py:117-126`) has
nothing left to order. The brace group's purpose, capturing the output of everything the command
runs (`remote_exec.py:34-44`, `fd220164a`), holds at the descriptor level: every process the payload
starts inherits `output` as both streams.

### Observation, and resuming after a dropped connection

`observe(launch, offset)` reads the state from `process` and `status` (the process counts as alive
only while `/proc/<pid>/stat`'s start ticks and the boot id match the recorded ones, so a reused pid
never reads as running), up to `OBSERVE_CHUNK = 262_144` bytes of `output` from `offset`, and the
size of each regular file in `work/`. Its stdout is one JSON line with the state, status, sizes and
the output's length, then the output bytes. It is an observation with a 30-second deadline, so a
connection that drops mid-read is dispatched again within the bound.

Resumption is the caller's polling loop, not a mechanism: the observation is idempotent and the
offset is the caller's, so a poll that fails with `ExecutionIncomplete` changes nothing, and the
next poll after the connection returns reads from where the last good one stopped. Backup's loop
(below) reports the loss once, a reminder every 30 seconds and the recovery, as today
(`remote_exec.py:256-264`), and polls until the launch leaves `RUNNING` or the operator interrupts;
there is no give-up bound, as there is none today. Because the id names the launch, a later
invocation can observe it the same way (R6); no command does, since no caller needs one.

### Interrupt

`interrupt(launch)` signals the payload's process group with `SIGTERM` once the recorded identity
still matches, waits up to `INTERRUPT_GRACE_SECONDS = 10` on the guest for the payload to exit,
sends `SIGKILL` to the group if it has not, and returns the observation. The supervisor is in its
own session, outside the group, so it survives and records the status (`128 + 15`, or `128 + 9`). A
launch that already ended, or whose identity no longer matches, is returned as it is with nothing
signalled. It is never dispatched again. Backup calls it from its `KeyboardInterrupt` handler, as it
calls `kill -TERM -<pid>` today (`vms/backup.py:389-399`).

### Download and discard

`download(launch, name, to)` sends the regular file `work/<name>` to stdout, opened with
`O_NOFOLLOW`; `name` must be one path component. The bytes go to the `ToFile` sink, which reports
progress and removes a partial file (I20). It has no deadline: the archive can take as long as the
link needs, the keepalives end a dead connection (I7), and Ctrl-C ends it.

`discard(launch)` removes the launch directory, refusing one whose payload is still running.

## Session environment and the terminal handover

### Sessions: environment in the request

Each session starts its own tmux server (dedicated socket), and tmux copies the server's environment
into its global environment at start, which every pane inherits. So the session's environment
travels as the launch request's `env`: `create_session` passes `launch_env`
(`sessions/tmux.py:910-917`) to `run_command(cmd, env=launch_env)` (`:990`), which
`access_run_command` makes a `Script` whose `env` agw-launch puts in the tmux client's environment,
from which the server starts. `_tmux_env_flags` (`:630-648`) and its use (`:918`, `:975`) go, so no
`tmux` argument carries a value (HLA D8). Neither does the SSH command line: `SetEnv` is gone with
the legacy transport.

**The launch identity read-back.** `_matches_launch_attempt` reads `_AGW_SESSION_UUID` and
`_AGW_RUN_ID` with `show-environment -t <session>` (`sessions/tmux.py:1310-1311`), the session
environment that `-e` set. Without `-e` they are in the global environment, so the read becomes
`show-environment -g`, falling back to `-t` for a session launched by an earlier release, which
still holds them there. The expected payload is unchanged. A test runs both shapes against a real
tmux; until it passes, this is the one tmux behavior the design assumes rather than has measured.

The session's account startup is unchanged in substance: the pane command is still
`$SHELL -lic 'cd <path> && exec <command>'` (`sessions/tmux.py:651-675`), now run by a server whose
`SHELL` agw-launch took from the passwd entry.

### Console panes: a fresh handover for every pane

Every console pane that runs a shell is created by one of two places, and each parks a fresh
handover for each pane it creates:

- **`_split_shell_pane`** (`multi_console/tmux_build.py:158-318`), which every path that creates or
  recreates a shell pane reaches: console build and restart (`_build_console_tmux` through
  `_add_session_window`, `:456`), `console add-sessions` (`crud.py:358`), `console add-shell`
  (`crud.py:654`), and `console restore-session` for missing panes (`restore.py:407`) and for a
  missing window (`restore.py:204` through `_add_session_window`). The last is the recovery after a
  killed attach pane (`977d32c7c`). It composes `pane_env` as today (`_resolve_pane_env`,
  `secrets_env.py:279-366`), parks `LoginShell(cwd=<workspace>, env=pane_env)` on the pane
  identity's access (the admin's, or the agent's for an agent pane), runs
  `tmux split-window ... <handover.command(run_as=agent or None)>` on the admin's access inside the
  block, tags the pane, and reports a pane whose handover was not collected as a failed split, which
  its callers already warn about or raise for (`restore.py:215-238`, `:404-424`).
- **The admin-shell window** (`tmux_build.py:529`), today `exec $SHELL -l` with no environment,
  which `secrets_env.py:152-160` records as a known gap: its environment is resolved and never
  delivered. It now parks the admin pane's environment the same way and runs
  `new-session -d ... <handover.command()>`, closing the gap.

Windows that run the attach loop (`multi_console/attach.py:151-198`) carry no environment and keep
their command. No pane path passes `-e`, and none passes `env=` to the admin's `run`.

**What replaces the sudo carry.** Today an agent pane runs
`exec sudo --login --preserve-env=<keys> -u <agent> bash -c '<bootstrap>'`
(`tmux_build.py:268-280`), which needs the `-e` values in its environment, `Defaults:<admin> setenv`
to let sudo keep them (`vms/initializer/shell_env.py:545-574`), and a per-operation probe that sudo
allows it (`tmux_build.py:80-155`). With the handover, the environment never crosses sudo: the
agent's own connection parks it, the pane runs `sudo -n -u <agent>` with stage 0 and the socket
path, and stage 0, now the agent, fetches the request and execs the shell with it (HLA D8).
`sudo -n -u` needs only the admin's existing `NOPASSWD: ALL`. The `--preserve-env` path, the probe,
`PreserveEnvMemo` and its threading (`tmux_build.py:77`, `:549`, `crud.py:354`, `:671`,
`restore.py:213`, `:405`), and the sudoers grant all go (below, "Deletions").

**Working directory.** Today a pane whose workspace directory is missing starts in the home with a
message (`cd <path> || echo ...`, `tmux_build.py:242`, `:248`). The parked request names the
workspace as its `cwd`, which agw-launch enters before parking (`agw_launch.py:255-258`), so such a
pane is refused before it is created, with `RequestRefused(cwd)`; the split is reported as failed.
Console restore and add-shell already refuse a missing workspace before they get here
(`restore.py:169`, `:363-364`), so this is reached only in a race.

## The requests that move

### Rules the tables follow

- **Identity.** "admin" is the admin's access, "root" the admin's with `elevated=True`, "agent" the
  agent's access, "owner" the session's owner (the admin's access for an admin session, the agent's
  for an agent session). Today's `sudo=True` is `sudo -n bash -c` (`transports/ssh.py:271-272`), so
  it becomes root, not the agent.
- **Shape.** As slice 6 did (`init-lld.md`, "Shapes"): a simple command is a literal-argv `Command`,
  which also removes today's unquoted interpolations (`workspaces/manager/copy.py:124`, `:176-178`,
  `:189`, `:234`, and `sessions/tmux.py:551`'s `$(dirname ...)`); a compound tmux line stays one
  `Script(Shell.SH)` through `access_run_command`. stdin is `NoInput` unless the table says
  otherwise.
- **Bounds.** A request keeps today's timeout as its `deadline`. A request unbounded today stays
  unbounded, its dead connection ended by the keepalives (I7), except where it becomes an
  observation, which must have a deadline: the session reads below take
  `GUEST_OBSERVATION_TIMEOUT_SECONDS`, as the bounded ones beside them already do.
- **Logging.** Every access is built with the operation's log, the agent's included: today every
  `agent_transport` in `sessions/` is built with no log, so agent-session launches and probes now
  reach the log, with environment names only (HLA D13).
- **Errors.** A handler that caught `SSHError` catches what that covered: `ExecutionIncomplete`,
  `CommandFailed` and `RequestRefused`, and `PathRefused`, `FileConflict` and `FileLockTimeout`
  around a file operation. `TrustMissing`, `TrustMismatch`, `VMNotReady` and `PermissionDenied`
  propagate, as in slice 6 (`init-lld.md`, "What each step catches"). A warning uses
  `vms/initializer/steps.describe_failure`, so a stderr tail is redacted before it is shown.

### Operation logs

`vms/manager/_helpers._operation_log` (`:228-241`) becomes
`operation_log(vm_name, command_stem, *, redactions=(), path=None)`, re-exported by `vms.manager`,
typed by `OperationLog`, and the one place outside slice 8 that constructs `SSHLogger`. `path` is
backup's (`vms/backup.py:83`). Every session, workspace and backup module takes its log from it,
with today's redactions, and types its log parameter as `phase_a.OperationLog` or `steps.InitLog`
instead of `SSHLogger`.

### Workspaces

| Operation                     | Today                                                                                                                                                                                                       | Request                                                                                                                                                       | As                      | stdin   | Deadline                  |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- | ------- | ------------------------- |
| Path free                     | `test -d \|\| test -e \|\| test -L`, `workspaces/backends/vm.py:70`                                                                                                                                         | the same line as a `Script(Shell.SH)` with the path as `$1`                                                                                                   | admin                   |         | 10                        |
| Parent and directory          | `mkdir -p`, `mkdir`, `:86`, `:88`                                                                                                                                                                           | `Command(("mkdir", "-p", "--", parent))`, `Command(("mkdir", "--", ws))`                                                                                      | root                    |         | none                      |
| Group                         | `sh -c 'getent ... \|\| groupadd'`, `usermod`, `chown`, `chmod 2770`, `:100-103`                                                                                                                            | `Script(Shell.SH)` for the group line; `Command`s for the rest                                                                                                | root                    |         | none                      |
| Clone attempt                 | `mkdir -m 700`, `chown`, `:115`, `:123`                                                                                                                                                                     | `Command`s                                                                                                                                                    | root                    |         | none                      |
| Clone                         | `git clone -- <repo> <attempt>/checkout`, `:128`                                                                                                                                                            | `Command(("git", "clone", "--", repo, checkout))`; a failure re-raised value-free as today (`:132-149`)                                                       | admin                   |         | 300                       |
| Clone permissions and publish | `chgrp -R`, `find -exec chmod g+s`, `chmod`, `mv -T`, `rmdir`, `:150-158`                                                                                                                                   | `Command`s                                                                                                                                                    | root                    |         | 120 for `find`, else none |
| Git identity                  | `git -C ws config --local`, `:175`                                                                                                                                                                          | `Command`                                                                                                                                                     | admin                   |         | none                      |
| tmuxinator file and link      | `write_file` (scp), `mkdir -p`, `ln -s`, `:181-186`                                                                                                                                                         | `files.write(cfg, data, mode=0o600, parent_mode=0o755)`; `files.ensure_directory(~/.config/tmuxinator, mode=0o755)`; `Command(("ln", "-s", "--", cfg, link))` | admin                   | content | 10                        |
| Rollback                      | `rm -rf -- <attempt>`, `:203`                                                                                                                                                                               | `Command`                                                                                                                                                     | root                    |         | 30                        |
| Delete                        | `rmdir` if present, `rm -rf`, link check and `rm`, `groupdel`, `:273-300`                                                                                                                                   | `Script(Shell.SH)` for the two conditional lines; `Command`s                                                                                                  | root, admin             |         | 30, 10                    |
| Rehome probes                 | `test -d` old and new, `which rsync`, `test -d`, `rehome.py:180`, `:189`, `:235`, `:242`                                                                                                                    | `Command`s                                                                                                                                                    | admin                   |         | 10                        |
| Rehome copy                   | `mkdir -p`, `chown`, `rsync -a` or `cp -a`, `:227-239`                                                                                                                                                      | `Command`s                                                                                                                                                    | root, admin for `rsync` |         | 600 for the copy          |
| Rehome permissions            | `chown`, `chmod`, `find`, the traversal loop, `:253-277`                                                                                                                                                    | `Command`s; the loop a `Script(Shell.SH)` with the path as `$1`                                                                                               | root                    |         | 120 for `find`            |
| Rehome tmuxinator             | `:292-302`                                                                                                                                                                                                  | as create; `remove_config` below                                                                                                                              | admin                   | content | 10                        |
| Rehome remove old             | `rm -rf`, `:321`                                                                                                                                                                                            | `Command`                                                                                                                                                     | root                    |         | 60                        |
| Repair                        | `which setfacl`, `apt-get install acl`, `getent`, `groupadd`, `id -nG`, `usermod`, `chown -R -c`, `chmod -c`, `find`, `getfacl -R` twice, the traversal loop, git identity, grant sync, `repair.py:130-433` | `Command`s, `env={"DEBIAN_FRONTEND": "noninteractive"}` for `apt-get`; the group and loop lines `Script(Shell.SH)`                                            | root, admin for git     |         | today's: 60, 120          |
| `remove_config`               | `rm -f <ws>/.tmuxinator.yml ~/.config/tmuxinator/<s>.yml`, `workspaces/tmuxinator.py:37`                                                                                                                    | `Command(("rm", "-f", "--", cfg, link))` on the admin's access; it takes `access: VMAccess`                                                                   | admin                   |         | 10                        |

The tmuxinator mode is 0600 because `write_file` copies a 0600 local temporary file with `scp`
(`transports/base.py:296-300`), which keeps that mode without `-p`; the batch confirms it on a VM
before relying on it. The `ln -s` is a `Command`, since a link is not a file operation. No caller
here needs an unconditional `remove`, which HLA D6 lists for workspaces (recommended HLA edit 2).

`workspace delete` (`workspaces/manager/delete.py:153-213`) passes the admin's access to session
teardown and to `kill_session_windows`, and `workspaces/realize.py` and
`materialize_grant_all_agents` type their log as `InitLog`.

### Workspace copy

Today the tree goes VM, workstation, VM: a raw `ssh ... tar czf - -C <src> .` into a local temporary
file with no bound, port or keepalive and an unquoted path (`copy.py:120-127`), then `scp` to the
destination's `/var/tmp` (`:181-182`), then `tar xzf` as root (`:183`) and the staging file's
removal (`:242`). It becomes two `run`s over an opaque stream, never extracted on the workstation
(HLA D6):

1. **Source.** `src.run(Command(("tar", "czf", "-", "-C", src_path, ".")), stdout=ToFile(local))` on
   the source VM's admin access, unelevated as today, no deadline. `local` is a path in a new
   `tempfile.TemporaryDirectory`, since `ToFile` creates its file. A completion other than
   `Completed(0)` raises; the sink has already removed the partial file.
2. **Destination.**
   `dest.run(Command(("tar", "xzf", "-", "-C", ws), elevated=True, stdin=FromFile(local)), check=True)`,
   no deadline: nothing is staged on the destination VM, so the `mktemp`, the upload and the `rm -f`
   go.

The group, directory, `chown -R` and `find` steps (`:171-193`) are `Command`s as in the workspaces
table, and the tmuxinator write (`:230-233`) is written only when the template enables tmuxinator,
as rehome already checks (`rehome.py:286-292`), where today copy writes it unconditionally. The
operation log is the destination's, closed in a `finally` as today (`fbf3ed505`); the source read is
recorded in it too. Copying within one VM still goes through the workstation, as today.

### Sessions: the tmux primitives

`RunCommand` keeps its shape (`command, *, check, env`). Every caller builds it with
`access_run_command(access, *, deadline, elevated, observation=False)` (`sessions/tmux.py:104-118`),
so a primitive does not know its identity or bound. The four that take a `Transport` directly
(`probe_tmux_server_pid`, `probe_process_start_ticks`, `read_boot_id`, `_matches_launch_attempt`,
`:1185-1318`) take a `RunCommand` and lose their `sudo` parameter, which the caller binds as
`elevated`. `tmux_cmd`'s `sudo` (`:555`), which nothing passes, goes.

| Primitive                                                                                    | Today                                        | Read-only | Notes                                                                                                                    |
| -------------------------------------------------------------------------------------------- | -------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------ |
| `deploy_restricted_config(files)`                                                            | `sudo mkdir`, `sudo tee` heredoc, `:551-552` | no        | `files.write(RESTRICTED_CONFIG_PATH, data, mode=0o644, parent_mode=0o755, elevated=True)`; one request, content on stdin |
| `require_socket_path`, `probe_tmux_session`, `probe_tmux_server`, the `_after_teardown` pair | `:602-611`, `:1080-1131`                     | yes       | Observations when the caller's bound is the status bound; not in teardown (68c837c1f)                                    |
| `_probe_pane_liveness`, `startup_protection_enabled`, `capture_output`                       | `:710-753`, `:1042-1057`, `:1132-1166`       | yes       | Unbounded as today; their own two-try loop (`834463f4a`) stays                                                           |
| `create_session`                                                                             | `:937-990`                                   | no        | `env=launch_env` in the request; no `-e`                                                                                 |
| `finalize_session_start`, `_grant_server_access`                                             | `:1001-1040`, `:613-628`                     | no        | Owner                                                                                                                    |
| `kill_session`, `kill_server`, `_raise_workload_died`'s `kill-server`                        | `:1059-1078`, `:788`                         | no        | Never observations                                                                                                       |
| Fingerprint, boot id, start ticks, launch identity                                           | `:1185-1318`                                 | yes       | Observations with the status bound in status and pid repair; plain in teardown                                           |

### Sessions: the manager

| Operation                                     | Today                                                                                                        | Access and elevation                                                                                           | Deadline, observation                                                                                                      | Replaces                                                                                                          |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Create: the agent probe                       | `_assert_agent_ssh_works` on an ephemeral `agent_transport`, `_create.py:195-196`, `_create_roll.py:210-211` | `_assert_agent_access_works(agent, admin, row)` (`agents/manager/access.py:44`)                                | its own (`access.py:60`, `:67`)                                                                                            | the legacy probe                                                                                                  |
| Create: config, launch, fingerprint, finalize | `_create_roll.py:398-471`                                                                                    | admin for the config; owner for the rest                                                                       | none, as today                                                                                                             | `deploy_restricted_config(run_command)`, `session_run_command`, `fingerprint_target`                              |
| Create: rollback absence proof                | `_create_roll.py:486-487`                                                                                    | owner                                                                                                          | none                                                                                                                       | the same with `target=`                                                                                           |
| Start, restart                                | `_lifecycle.py:502-942`                                                                                      | admin (logged from the start, where today the first requests are not logged, `:502-503`), owner for the launch | none, except the status reads                                                                                              | the log-free `transport(vm, config)` and `_build_session_target`                                                  |
| Status of one session                         | `check_session_status`, `_status.py:139-170`, unbounded                                                      | root for an agent session, admin for an admin session                                                          | 10, observation                                                                                                            | `partial(target.run, sudo=agent)`                                                                                 |
| Status, bounded                               | `_BoundedStatusTarget`, `_status.py:50-56`, `:196-216`, `_queries.py:478-481`                                | as above                                                                                                       | 10, observation                                                                                                            | `_BoundedStatusTarget`, which goes                                                                                |
| Batch status                                  | `batch_check_status`, `_status.py:240-247`, `input_data=` lines, an inner `sudo -n` per agent row            | one root `Script(Shell.SH)` per VM, the rows on stdin as `Bytes`, no inner `sudo`                              | 10, observation                                                                                                            | the script's own `sudo -n tmux` and `sudo -n sh -c`                                                               |
| Legacy rows in a status fan-out               | `_status.py:476`, `:498-500`, an unbounded probe on the main thread                                          | owner, through `_assert_agent_access_works` for an agent                                                       | 10, observation                                                                                                            | `_build_session_target`'s `true`                                                                                  |
| Pid repair                                    | `_repair_session_pid`, `_pids.py:122-422`, unbounded                                                         | admin, root for an agent session                                                                               | 10, observation for every read                                                                                             | `target.run`, `partial(target.run, sudo=agent)`                                                                   |
| The agent probe in `_build_session_target`    | `_pids.py:86-87`                                                                                             | `_assert_agent_access_works`                                                                                   | its own                                                                                                                    | `_assert_agent_ssh_works`                                                                                         |
| Stop, delete, teardown                        | `_teardown.py:208-414`, `_lifecycle.py:117-153`, `:288-305`                                                  | owner; root when the caller does not own the session (agent and workspace delete, stop-all's agent rows)       | none for single stops; `DEDICATED_TEARDOWN_TIMEOUT_SECONDS` per request in stop-all's concurrent plans; never observations | `DedicatedTeardownPlan.target` and `sudo` become `access` and `elevated`                                          |
| Stop-all                                      | `_lifecycle.py:1053-1078`                                                                                    | admin, root for agent rows                                                                                     | 10 per request in the concurrent plans                                                                                     | `transport(..., default_timeout=10, retries=1)`; the plans' accesses keep no log, as today (`_lifecycle.py:1056`) |
| Attach                                        | `target.interactive("tmux -S S attach -t '=N'")`, `_queries.py:961`                                          | admin; `TerminalCommand(("tmux", "-S", socket, "attach", "-t", "=" + name))`, or without `-S` for a legacy row | none                                                                                                                       | `Transport.interactive`                                                                                           |
| Logs                                          | `capture_output`, `_logs.py:80`                                                                              | admin                                                                                                          | none                                                                                                                       | the admin `run`                                                                                                   |
| tmuxinator regeneration                       | `_scope.py:150-165`                                                                                          | admin, `files.write` and `remove_config` as for workspaces                                                     | 10                                                                                                                         | `write_file` and `remove_config(target)`                                                                          |
| `_scope._prepare_vm`                          | `_scope.py:124-128`: a log with no redactions, and a `run_as_root` no caller uses                            | yields the admin's access; the log takes the operation's redactions                                            |                                                                                                                            | `run_command`, `run_as_root`                                                                                      |
| Agent delete's teardown                       | `agents/manager/lifecycle.py:389-398`                                                                        | root (`target_owns_session=False`)                                                                             | none                                                                                                                       | `_mgr.transport`                                                                                                  |

**Two tighter bounds.** `check_session_status` and pid repair are unbounded today, so a VM that
stops answering stalls `session stop`, `session delete`, `session attach` and every status listing's
repair pass. They become observations with the 10-second bound their bounded siblings already have
(`_pending_launch_is_owned`, `session_description`); an observation that runs out reads as unknown,
which every caller already treats as fail-closed (`d193e48fd`). The batch status script loses its
inner `sudo -n` and runs elevated as a whole (HLA D4: elevation is the whole operation). That rests
on root being allowed to connect to the admin's tmux server: tmux 3.3 added `server-access`, and
this design reads its manual as always allowing the server owner and root. That is not measured yet,
so a test runs the script as root against an admin-owned server under the sudo lane first; if root
is refused, the batch is two requests, the admin's rows unelevated and the agents' elevated.

### Consoles

Every console request runs on the admin's access (the console's tmux server is the admin's), except
an agent pane's parking connection, which runs on the agent's. `_prepare_vm_target` and
`_live_target` (`multi_console/attach.py:345-408`) yield the admin's access; the gate and the SSH
boundary are as today.

| Operation                                 | Today                                                                                                               | Request                                                                                      | Deadline, observation                  |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------- |
| Presence, the post-teardown probe         | `has-session`, `multi_console/attach.py:207`, `:251`, `:287`                                                        | `Script(Shell.SH)`                                                                           | none (10 in status)                    |
| Teardown, staging teardown, kill a window | `kill-session`, `kill-window`, `:244`, `:279`, `:332`                                                               | `Script(Shell.SH)`                                                                           | none                                   |
| Attach                                    | `target.interactive("tmux attach -t '=aw-console-<c>'")`, `:830-833`                                                | `attach(TerminalCommand(("tmux", "attach", "-t", "=aw-console-" + name)))`                   | none                                   |
| Build                                     | `new-session` for the admin-shell window, the placeholder, `kill-window`, `rename-session`, `tmux_build.py:529-579` | the admin-shell window through `park` (above); the rest `Script(Shell.SH)`, `check=True`     | none                                   |
| Session window                            | `new-window ... '<attach loop>'`, `tmux_build.py:426`                                                               | `Script(Shell.SH)`                                                                           | none                                   |
| Shell pane                                | `split-window -e K=V ...` with `env=pane_env`, `:282`; tag, `:302`                                                  | `park` on the pane identity's access, then `split-window ... <handover command>` and the tag | parking: 60 to `parked`, 35 to collect |
| The `--preserve-env` probe                | `tmux_build.py:134`                                                                                                 | none: deleted                                                                                |                                        |
| Layout, focus, panes, reorder             | `multi_console_layout.py:76-444`, `tmux_build.py:334`, `restore.py:140`                                             | `Script(Shell.SH)`                                                                           | none                                   |
| Status                                    | `list-sessions`, `multi_console/_status.py:54`                                                                      | `Script(Shell.SH)`                                                                           | 10, observation                        |

### Backup

| Operation         | Today                                                                                         | Request                                                                                                                                                                                           | As    | stdin         | Deadline, observation |
| ----------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- | ------------- | --------------------- |
| Agent uid         | `id -u <user>`, `vms/backup.py:137`                                                           | `Command(("id", "-u", user))`                                                                                                                                                                     | admin |               | none                  |
| Workspace gid     | `getent group <g>`, `:157`                                                                    | `Command(("getent", "group", g))`                                                                                                                                                                 | admin |               | none                  |
| Path present      | `test -d`, `:300`                                                                             | `Command(("test", "-d", path))`                                                                                                                                                                   | root  |               | none                  |
| zstd present      | `command -v zstd`, `:310`                                                                     | `Script("command -v zstd", Shell.SH)`                                                                                                                                                             | admin |               | none                  |
| Size              | `du -sb ... \| awk`, `:318`                                                                   | `Command(("du", "-sbc", "--", *paths))`; the `total` line                                                                                                                                         | root  |               | none                  |
| Archive           | `mktemp -d`, a staged path list, a second `mktemp -d`, `run_detached` on a thread, `:288-379` | `detached.launch(Command(("tar", "--zstd", "--null", "-T", "-", "-cf", "workspaces.tar.zst", "-C", "/"), env={"ZSTD_CLEVEL": "15"}, elevated=True, stdin=Bytes(paths)))`, the paths NUL-separated | root  | the path list | 60                    |
| Progress and wait | the thread's join loop and `_report_size` every 30 s, `:381-388`, `:532-539`                  | `observe` every 5 s; the archive size from `work`, reported every 30 s                                                                                                                            | root  |               | 30, observation       |
| Interrupt         | `cat <pid>`, `kill -TERM -<pid>`, `:389-399`                                                  | `detached.interrupt`                                                                                                                                                                              | root  |               | 30                    |
| Download          | `chown` the archive to the admin, `stat`, `scp` with progress, `:426-433`, `:454-518`         | `detached.download(launch, "workspaces.tar.zst", ToFile(local, on_progress=...))`                                                                                                                 | root  |               | none                  |
| Cleanup           | three `rm -rf` and `rm -f`, `:435-449`                                                        | `detached.discard`                                                                                                                                                                                | root  |               | 60                    |

The probes keep today's handling: a probe that does not complete stores `None`, as `SSHError` did
(`:143-144`, `:164-165`), catching `ExecutionIncomplete` only. The tar output is the launch's
`output`; a non-zero status raises `BackupError` with its tail, and a clean one shows its last ten
lines as warnings (`:407-421`). On every path backup discards the launch in a `finally`; a discard
that fails (the VM unreachable, or the payload still running after a failed interrupt) warns naming
the launch id, and the prune removes the directory later. The `--null` list is the one change in the
command: a NUL cannot occur in a path, so no workspace path can split the list. The `chown`, the
staged path list and the thread go, and so does the assertion that the target is an `SSHTransport`
(`:88`).

### Other callers

| Caller                                                             | Today                                                                                                                   | Becomes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The codex recorder (`plugins/codex/recorder.py:161-181`)           | A `mkdir`, `printf`, `chmod` and `mv` fragment in the pane command, as the session user (`harness_integration.py:1019`) | `start` publishes it with `access.files().write(home + "/" + _RECORDER_TAIL, lines, mode=0o755, parent_mode=0o755)` on the session's own access, `home` from the integration's artifact context (`artifact_context.home`, `harness_integration.py:691`), or from `HOME_PROBE` (`artifacts/session.py:51`) when the session has none. A failure warns and the launch goes on, as today a failed fragment did (`recorder.py:171-173`); `provision_fragment` and `_RECORDER_STAGE_TAIL` go |
| `vm describe`'s live resources (`vms/manager/_helpers.py:306-360`) | `retries=3`, no bound                                                                                                   | One `Script(Shell.SH)` on the admin's access, `observation=True`, deadline 30 (decision 1)                                                                                                                                                                                                                                                                                                                                                                                              |

`Files.write` refuses a link at the recorder's path or a parent. Today's `mkdir -p` and `mv` follow
one; a stow-style linked `~/.agentworks` now makes the recorder absent with a warning, and codex
falls back to discovery, which is how a failed install already behaves.

## Deletions

In the order they land, each with its test:

1. **`remote_exec.py`'s callers** (batch A). Its only caller is backup (`vms/backup.py:357`), which
   moves to `detached`. The module and its tests (`cli/tests/test_remote_exec*.py`) are then
   unreachable; the plan deletes the module in slice 8 (decision 2). Test: the static check covers
   `vms/backup.py`.
2. **`_assert_agent_ssh_works`** (batch C). Its three callers
   (`sessions/manager/_create.py:191-196`, `_create_roll.py:207-211`, `_pids.py:83-87`) call
   `_assert_agent_access_works`; the function (`agents/manager/_common.py:245-294`), its export
   (`agents/manager/__init__.py:42`, `:71`) and its tests (`cli/tests/test_agents.py:74-178`) go.
   The session tests that patch it (`cli/tests/test_session_transport.py:376-618`,
   `test_error_wrapper.py:758`) patch `_assert_agent_access_works`;
   `test_execution_migrated_commands.py:95` already proves its three outcomes.
3. **The console's environment carry and its sudoers grant** (batch D):
   - `_sudo_can_preserve_env`, `PreserveEnvMemo`, `_SUDO_PRESERVE_PROBE_VAR` and the
     `--preserve-env` branch (`tmux_build.py:77-155`, `:268-280`, `secrets_env.py:42`), and every
     `preserve_memo` parameter.
   - `_write_sudoers_console_setenv` (`vms/initializer/shell_env.py:545-574`) and its call
     (`driver.py:387-388`) become `_remove_sudoers_console_setenv(files, log, admin_user)`: an
     elevated `files.read` of `/etc/sudoers.d/51-agentworks-console-setenv`, and when it holds the
     managed header and `Defaults:<admin> setenv`,
     `files.remove_if(path, expected=<digest of what it read>)`. Absent is done; a file without the
     managed header is an operator's and is left with a warning; a failure warns as a step does. So
     create writes nothing and reinit removes the fragment any earlier release installed, whatever
     its comment text. Tests: over `local_files`, the managed fragment is removed, an edited one is
     kept with a warning, and an absent one is no request beyond the read.
   - `AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH` stays, as the path reinit removes.
   - Test that no pane path passes a value: a recording test of console build, add-shell and both
     restore paths asserts that no `tmux` argv or script line contains any `pane_env` value and that
     no admin `run` carries `env`, and that each shell pane parked one handover on its identity's
     access.
4. **The environment newline refusals** (batch E, once no request sends environment by `SetEnv`,
   which is true when batch D lands: the console panes were the last):
   - the newline branch of `_env_entry_issues` (`manifests/decode.py:254-261`);
   - `compose_env`'s `require_line_safe_secret` (`env/compose.py:76-85`), which becomes
     `user_env[key] = values[entry.secret]`;
   - `validate_target_values` (`secrets/orchestration.py:203-240`) and its calls
     (`secrets/resolver.py:187-189`, `:199-201`, `orchestration.py:299`), whose only job is that
     refusal;
   - `LineOrientedSecretUse.ENVIRONMENT` (`secrets/line_safety.py:13`).
   - Tests: the refusal tests in `cli/tests/manifests/test_spec_workspace_template.py`,
     `test_secrets_orchestration.py`, `test_secrets_resolver.py` and `secrets/test_line_safety.py`
     go; one test resolves a secret holding a newline and a `%` and checks that the composed
     environment holds it exactly, and the conformance vector that delivers environment values
     byte-exact (slice 2's) already covers the guest side.
   - `ENVIRONMENT_REVEAL` (`env/show.py:469-484`) guards a different sink, `env show`'s table, where
     a newline would forge a row; and once config may hold a plaintext value with a newline, that
     table can show one too. So `_print_table` renders every value with control characters escaped
     (`\n`, `\r`, `\t`, `\xNN`), and `ENVIRONMENT_REVEAL` goes as well, so a revealed multi-line
     secret is shown escaped rather than refused (decision 3).

`GUEST_OBSERVATION_ATTEMPTS`, `DEDICATED_TEARDOWN_ATTEMPTS`, `_BoundedStatusTarget`,
`_tmux_env_flags`, `provision_fragment`, `agents/manager/__init__.py`'s `transport` re-export and
the `RunCommand` docstring's legacy half go in the batch that removes their last user.

## Behavior changes for the upgrade guide

- Session and console environment reaches the guest in the request: no value is on a `tmux` or
  `sudo` command line or in an SSH option, and environment values may now hold newlines.
- A console's admin-shell window now has the console's admin environment.
- An agent's console shell no longer needs `vm reinit` for its environment; reinit removes the
  `51-agentworks-console-setenv` sudoers fragment.
- A console shell pane whose workspace directory is missing is reported as a failed split instead of
  opening in the home directory.
- Agent sessions' requests are written to the operation log, with environment names only.
- `session stop`, `delete`, `attach` and the status listings bound each read of a session's state at
  10 seconds, and try a read that lost its connection again within that bound.
- `vm backup` stages nothing in `/var/tmp`: its archive is under `/var/lib/agentworks/detached`,
  removed when the backup ends, and pruned a week after a backup that was killed.
- `workspace copy` stages nothing on the destination VM, and writes the tmuxinator file only when
  the template enables it.

## Tests

None restates the implementation or our prose (`no-prose-policing-tests`).

**The plan's Tests box:**

| Plan item                                       | Test                                                                                                                                                                                                                                                                                                                                                                      |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A fresh launch never matches an earlier one     | `test_detached_program.py` runs `main` in-process against a temporary base: two launches of one command get two directories and each observation reports only its own output and status; a launch whose id already exists is refused and the earlier launch's files are unchanged; a malformed id is refused                                                              |
| Observation resumes across a dropped connection | Unit, over `RecordingVMAccess`: `observe` results of `Unknown(CONNECTION)` twice, then data, make backup's loop report the loss once and read from the last good offset, with no `launch` repeated; and `run` re-dispatches an `observation=True` request after `Unknown(TRUNCATED)` and not after `AUTH_FAILED` or `Completed(1)`. Live: backup with a forced disconnect |
| Interrupt stops the work                        | In-process: a launch of `("sh", "-c", "sleep 600 & sleep 600")`; `interrupt` leaves no process in its group and the state `EXITED` with 143, and the supervisor recorded it. Live: backup interrupted on WSL2 and Remote Lima                                                                                                                                             |
| The detached base is never world-writable       | In-process, under the sudo lane: the created base is root's with mode 0700 and its parent 0755; a pre-made base with mode 0777, another owner or a link in its place is refused and left as it is                                                                                                                                                                         |

**Unit, every CI host:**

- `run` with `observation=True` and no deadline is a `ValidationError`; the whole observation stays
  within its deadline; each dispatch is logged; `stream` refuses an observation.
- `ToFile`: an existing path is refused before dispatch; the file exists after `Completed(0)` only,
  and is gone after `Completed(1)`, `Unknown` and a `KeyboardInterrupt` raised by `on_progress`
  (I20).
- `launch` after an `Unknown` returns the launch when the follow-up observation sees it, and raises
  naming the id when it is `ABSENT`.
- `park`: a split that raises closes the parking stdin at once; an uncollected handover reports
  `collected=False`; `Handover.command` puts `sudo -n -u` in front only for another account.

**The guest programs.** `test_detached_program.py` beside `test_file_program.py`, on Linux CI, both
interpreters. agw-launch's `TerminalCommand` and the inherited `TERM` join the conformance suite as
vectors T1 (an argv on the terminal, with its refusals) and T2 (a pane handover through `sudo -n -u`
under the sudo lane, with `TERM` from the environment).

**tmux.** A test against a real tmux (skipped where none is installed) launches a session with its
identity in the request's environment and reads it back through `_matches_launch_attempt`, and does
the same with the identity in the session environment as an earlier release set it; and the batch
status script runs as root against an admin-owned server.

**Callers.** Session, console, workspace and backup tests that build MagicMock transports and assert
command strings move to `RecordingVMAccess` and `local_files` (`cli/tests/vm_access_support.py`),
keyed by request builder, as slice 6's did (`init-lld.md`, "Tests"). The slice 6 bridges
`route_vm_access_to_target` and `files_over_target`, and `tmux.CommandOutcome`'s mirror of
`SSHResult`, go with their last legacy target. Every migrated file joins the static check
(`cli/tests/test_execution_import_boundary.py`).

## Incident audit

The entry points this slice moves are `workspaces/`, `sessions/` (tmux, the managers, status, pid
repair and teardown, consoles and layout), `vms/backup.py`, `remote_exec.py`, the codex recorder,
the console's environment carry and the newline refusals. The audit read the subjects of their fix
history (`git log` on each) and the bodies of the incident-bearing commits it cites, ADRs 0017 and
0019 (and 0014 for the fragment slice 8 owns), and the predecessor's migration strategy
(`docs/sdd/2026-09-12-transport-improv/migration-strategy.md`, its incident-derived inventory). The
session history since September is dense with fencing fixes; rows I84 and I85 cover the ones whose
behavior rides on a request's outcome, and the rest are domain logic this slice only repoints.

**Existing rows this slice re-proves:**

- **I6a** (console recovery after a killed `ssh -t`). Two halves. A killed console attach leaves the
  console's tmux session running and nothing to repair: the attach is `attach(TerminalCommand)`,
  whose 255 notice (I6) says so, and the next `console attach` re-attaches; `console start` tears
  down a staging session a killed build left (`multi_console/attach.py:696-698`). And a killed
  attach pane inside a console is rebuilt by `console restore-session`, which finds the session pane
  structurally (`977d32c7c`, `multi_console_layout.py:258`) and recreates the window, every shell
  pane parking a fresh handover. Re-proved by a recording test of `restore_session` on a window with
  no untagged pane (one park per configured shell, each on its identity's access) and live: kill the
  attach's `ssh` and re-attach; kill a console's attach pane and restore it.
- **I8** (whole-operation elevation and `setsid` replace `nohup` outside `sudo`; the output of
  everything the command runs is captured). Re-proved by `test_detached_program.py`: a payload that
  writes to both streams from itself and a child has all of it in `output`, and the payload is in no
  session the launching process belongs to; live by backup surviving a forced disconnect. The HLA
  row's "the output-capturing brace group is kept" becomes "the payload's descriptors capture
  everything it runs" (recommended HLA edit 3).
- **I20** (the download stops and removes the partial file on Ctrl-C): now the `ToFile` sink's rule,
  re-proved by its unit tests and live by an interrupted download.
- **I57** (a dead workload's capture and cleanup are best effort): `_raise_workload_died`
  (`sessions/tmux.py:755-801`) keeps its structure; re-proved by its tests moved to
  `RecordingVMAccess`, with `capture-pane` and `kill-server` returning `Unknown` and
  `CommandFailed`.
- **I21** and **I6** (terminal restore and the attach notice): session and console attach now go
  through `VMAccess.attach`, which slice 2 proved; re-proved live by the attach cells.
- **I67** (workspace ACLs): unchanged, `apply_workspace_acls` is already on `Files` (slice 6).
- **I75** (stale-socket cleanup): unchanged (slice 6).

**New rows, I80 to I89**, for the HLA's inventory, all re-proved in slice 7:

| Row | Behavior                                                                                                                                                                                                                                                                                                                                                                             |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| I80 | A detached launch never adopts an earlier launch's result or files: each launch has a fresh id and the guest refuses an existing directory (`fd220164a`; `remote_exec.py:96-103`; R6)                                                                                                                                                                                                |
| I81 | Detached observation survives a dropped connection: a failed poll is reported and polling goes on, and the status and output are read once the connection returns (`ae9df9af6`; `remote_exec.py:147-154`, `:222-281`)                                                                                                                                                                |
| I82 | Interrupting a backup stops the remote archive's whole process group, and the backup's guest staging is removed on every path (`97e11a168`; `vms/backup.py:389-399`, `:435-449`)                                                                                                                                                                                                     |
| I83 | Backup's guest staging is root-owned and private, never a predictable path in a world-writable directory (`97e11a168`; `vms/backup.py:287-288`, `:337-353`)                                                                                                                                                                                                                          |
| I84 | A session or console probe that could not run reads as unknown, never as absent; teardown signals a tmux server only when its pid, boot id and start ticks match the stored fingerprint, and never repeats a kill or an uncertain observation (`d99835dac`, `d193e48fd`, `2fd330832`, `2713928c6`, `68c837c1f`; `sessions/tmux.py:155-245`, `sessions/manager/_teardown.py:208-414`) |
| I85 | Runtime recovery binds an unacknowledged launch to its exact session through the launch identity it carried, `_AGW_SESSION_UUID` and `_AGW_RUN_ID` (`b985953a5`; `sessions/tmux.py:1294-1318`)                                                                                                                                                                                       |
| I86 | A session launch's pane capture and failure text have the session's secret values removed before they reach an error or the operation log (`9ff646d14`; `sessions/tmux.py:755-801`, `sessions/manager/_create.py:138-140`, `_create_roll.py:638-642`, `_lifecycle.py:38-48`)                                                                                                         |
| I87 | A failed workspace clone raises a value-free error, since the guest's stderr and the command can hold credentials in the repository address (`66f0651d0`; `workspaces/backends/vm.py:132-149`)                                                                                                                                                                                       |
| I88 | Session and console status is bounded per VM, so a VM that does not answer reads as unknown without stalling the listing (`8475c8a80`, `3edd3f8d1`, `c666b6184`; `status_observation.py:13-14`, `sessions/manager/_status.py:240-247`)                                                                                                                                               |
| I89 | The codex recorder is published atomically and never fails the launch (`plugins/codex/recorder.py:161-181`)                                                                                                                                                                                                                                                                          |

**How each is re-proved, by test.** I80, I81 and I8 by `test_detached_program.py` and the `detached`
unit tests above; I82 by a backup unit test that raises `KeyboardInterrupt` from the observe loop
and records `interrupt` then `discard`, and live; I83 by the base test; I84 by the teardown and
status tests moved to `RecordingVMAccess`, with a recording assertion that no teardown request is an
observation and no `kill` is sent twice; I85 by the tmux test above; I86 by
`cli/tests/sessions/test_logging_and_capture.py` moved; I87 by a backend test whose clone fails with
the URL in its stderr; I88 by a status test whose VM returns `Unknown(DEADLINE)` and completes
within the bound; I89 by a codex test that the recorder is one `write` with mode 0755 and that a
`PathRefused` warns and the launch goes on.

**Not rows.** The console's `--preserve-env` probe (`dc0573ac9`, ADR 0017) is deleted, not
preserved; its failure mode cannot occur without the sudo crossing. The unbounded legacy calls this
slice bounds are a deliberate change (above). The tmux target quoting for zsh (`211edd0b2`,
`exact_tmux_target`) is unchanged and still applies, since the console's server runs pane commands
through the admin's shell.

### ADR 0017

ADR 0017 is superseded in batch D. Its status line becomes "Superseded by the terminal handover
(transport take 2, HLA D8; `sessions-lld.md`)", and a short section says what replaced each
decision: the environment travels in the pane's parked request, so nothing crosses sudo; the
`setenv` grant is removed by reinit; the probe and memo are gone; and its first Negative, values on
the `tmux -e` argv, no longer applies. Its own text stays as the record. ADR 0014's `env_keep`
fragment is untouched here: slice 8 searches for its remaining users.

## Collateral

- **`cli/agentworks/execution/README.md`:** `observation`, `FromFile`, `ToFile`, `TerminalCommand`,
  `park` and `detached`, with the base directory's layout and modes.
- **`docs/guides/upgrading-to-0.20.md`:** the "older SSH path (sessions, workspaces and backups)"
  mentions (`:143`, `:244-245`) go; `:113-114` lists sessions, consoles, workspaces and backup as on
  the new transport; the behavior changes above.
- **`docs/guides/idempotency.md:91`:** the "sudoers console setenv" row says reinit removes the
  fragment.
- **ADR 0017** superseded (above); **ADR 0019**'s secrets section if the operator item below changes
  the console's resolve.
- **Docstrings that describe the old mechanics:** `sessions/tmux.py:66-70` (the admin socket
  comment's "SetEnv-delivered env"), `:74-80` (`RunCommand`), `:651-675` (`_pane_command`'s env
  paragraph), `multi_console/tmux_build.py:183-203`, `secrets_env.py:152-160`,
  `multi_console/attach.py:362-366`, `vms/backup.py:268-285`, `plugins/codex/recorder.py:13-16` and
  `:50-55`, and the `harness_integration.py:1002-1009` pane-command docstring.
- **Completions and sample config:** no command, option or setting changes.
- **Contracts.** `ToFile`, `FromFile`, `observation`, `TerminalCommand`, `park` and `detached()` are
  additive on `VMAccess`, which plugins reach through their run context; every kind was bumped once
  for this release already (`init-lld.md`, "Collateral"). No bump and no contract footer.

## Live evidence

The plan's cells:

| Check                                                                                                                                                      | Cells                                           | Rows                 |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | -------------------- |
| Session create, attach, detach, stop, start and delete, admin and agent, with a secret holding a newline in the session environment                        | Remote Lima from Linux, macOS and Windows; WSL2 | I21, I85, decision 3 |
| Console create, attach, detach, restore-session for a missing pane and a killed attach pane, add-shell for an agent, with agent-scope secrets in that pane | Remote Lima from all three; WSL2                | I6a, I6              |
| Kill a console attach's `ssh`, then re-attach                                                                                                              | Remote Lima from one workstation                | I6a                  |
| `session list` with one VM stopped returns within the bound                                                                                                | one cell                                        | I88                  |
| Backup with a forced disconnect during the archive and during the download                                                                                 | WSL2 (with idle shutdown) and Remote Lima       | I8, I81, I25         |
| Backup interrupted during the archive and during the download                                                                                              | WSL2 and Remote Lima                            | I82, I20             |
| Workspace create with a clone, copy between two VMs, rehome and repair                                                                                     | one cell                                        | I67, I87             |
| `vm reinit` on a VM that has the setenv fragment removes it                                                                                                | one cell                                        | deletion 3           |

I57, I80, I83, I84, I86 and I89 depend on failures or races a live run cannot provoke safely, so
they have tests and no live cell.

## Delivery

Five batches, each green on its own, in the order they land in PR C. The rule from slice 6 holds:
**a batch that changes a shared helper moves every caller of it**, and a bridge one batch leaves is
named with the batch that removes it.

| Batch                            | Contents                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Size                                                                       |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| **A. Detached work and backup**  | `observation` and `CONNECTION_OUTCOMES`; `ToFile`; `detached.py` and `detached_program.py` with the plan's four tests; `operation_log`; backup on them; `_query_live_resources` (decision 1); `remote_exec.py` left with no caller; I8, I20, I80 to I83                                                                                                                                                                                                       | L: about 700 lines of code, 800 of tests                                   |
| **B. Workspaces**                | `FromFile`; workspace backends, copy, rehome, repair and delete's own requests; `tmuxinator.py` with its session caller (`_scope._regenerate_tmuxinator`, building an admin access beside its target); I87. Bridge: `delete_workspace` keeps a legacy target for session teardown and `kill_session_windows`, gone in C                                                                                                                                       | M: 400 and 500                                                             |
| **C. Sessions**                  | `TerminalCommand`; every tmux primitive on `access_run_command`; create, start, restart, status, pid repair, teardown, stop-all, attach and logs; `kill_session_windows` with all four callers; agent delete's teardown; `_assert_agent_ssh_works` deleted; the codex recorder; I84 to I86, I88, I89. Bridges: the console modules pass their legacy `target.run` as a `RunCommand` and build an admin access beside it for `kill_session_windows`, gone in D | XL: 1,200 and 1,500; can split at status and pid repair if review wants it |
| **D. Consoles and the handover** | `park` and `Parked`; the pane and admin-window handovers; every console request; console attach; the `--preserve-env` carry, its probe and memo deleted; `_remove_sudoers_console_setenv`; ADR 0017 superseded; I6a                                                                                                                                                                                                                                           | L: 600 and 700                                                             |
| **E. Closure**                   | The newline refusals and `ENVIRONMENT_REVEAL` (decision 3); every migrated file on the static check; the inventory rows in the HLA; the collateral not already carried                                                                                                                                                                                                                                                                                        | S: 150 and 200                                                             |

Each batch carries the collateral it makes true.

## The operator item

The plan's box asks two things.

**"The commented post-bind resolve in `sessions/multi_console/restore.py`."** Nothing is commented
out: `restore.py:177-207` and `:372-399` are live resolves, each with a comment. What the box means
is the comment's subject. Each calls itself a "conditional-need exception to the
one-boundary-resolve contract": ADR 0019 resolves a command's secrets in one boundary pass before
anything mutates, and names the gate's secrets as the one sanctioned resolution outside it. Until PR
B the comment's justification was a precedent, "same class as the Tailscale rejoin"; PR B deleted
the rejoin and its sanctioned late resolve, removed that clause from the comment (`2cc0553da`) and
amended ADR 0019 to say no gate resolves anything after its boundary
(`docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md:11-15`). The two follow-ups
in PR B's description became this box (`42babeb61`). So the restore resolves are now an exception
with no precedent that ADR 0019 does not record, and they are not alone: every console operation
that creates panes resolves after its gate opens, on what `_prepare_vm_target`'s docstring calls
"the documented conditional-need path" (`multi_console/attach.py:362-366`): `_realize_console`
(`:702`), `console create` (`crud.py:158`), `add-sessions` (`:306`) and `add-shell` (`:620`), as
well as the two restore paths. Each resolves before any pane is opened (though `console start` first
removes a staging session a killed build left, `multi_console/attach.py:696-698`); what they break
is "one boundary pass per composition root".

This slice does not change what they resolve: the handover needs the same values to park each pane.

> **Question for the operator.** Console operations resolve their panes' secrets after the VM gate
> opens, in a second pass ADR 0019 no longer sanctions. Which shape should they take?
>
> 1. **Keep the late resolve and record it.** Amend ADR 0019 to sanction "console pane secrets,
>    resolved after the gate and before the first pane is created, scoped to the panes the live tmux
>    state says will be created". Cost: a second exception to one-pass resolution, and a second
>    place that prompts. Benefit: a restore or start that creates no pane asks for nothing.
> 2. **Resolve in the boundary pass.** Each console command declares every secret its configured
>    panes could use (all of a member's shells for restore-session, every pane for start), so the
>    boundary resolves them once, and the late resolves go. Cost: a restore that turns out to need
>    one pane, or none, still resolves or prompts for all of them, and a non-interactive run fails
>    on a secret it would not have used, which `restore.py:376-384` avoids today.
> 3. **Probe before the boundary.** Read the live tmux state inside the gate, before the boundary
>    pass, and declare exactly the panes it shows are missing. Cost: a plan whose secret set depends
>    on live VM state, a shape ADR 0019 does not have, and a race between the probe and the build.
>
> **Recommendation: option 2.** It removes the only remaining exception, restores the property ADR
> 0019 promises, and costs at most a prompt for a secret the console is already configured to use,
> on commands an operator runs rarely. Option 1 is the right choice if the operator values "no
> prompt for an unused pane" over one resolution pass.

**"Whether the refusals of a VM with no recorded SSH identity should hint at `agw vm reinit`."**
They already do. `require_vm_ssh_identity` raises `VMSSHIdentityPolicyError` with the hint "Run 'agw
vm reinit <vm>' to establish SSH identity evidence." for `NOT_RECORDED`
(`vms/applied_state.py:262-273`, since `68961b788`), every workspace, agent and session command
reaches it through `require_vm_ssh_boundary` (`vms/manager/boundary.py:87-104`), and `vm reinit`
itself accepts that state (`vms/manager/reinit.py:231`), so the hint leads somewhere. The status
listings catch the error and show the state as unknown (`sessions/manager/_status.py:463`,
`_queries.py:465`, `multi_console/_status.py:111`), which is right for a listing. That half needs no
operator decision and can close.

## Decisions for the lead

1. **`vm describe`'s live resources move in this slice** (`vms/manager/_helpers.py:306`) as the
   `observation` option's third caller, so slice 8 has no legacy caller left to migrate. The
   alternative is to leave it for slice 8, which then migrates one function before it can delete the
   transports.
2. **`remote_exec.py` has no caller after batch A.** The plan deletes it in slice 8 with the other
   legacy modules. Deleting it and its tests in batch A instead is principle 8's push, and costs
   slice 8 one line; recommended.
3. **`ENVIRONMENT_REVEAL` goes too**, replaced by escaping control characters in `env show`'s table,
   since retiring the config refusal lets plaintext values with newlines reach the same table. The
   alternative keeps the reveal refusal and leaves plaintext values unescaped, so a value with a
   newline forges a table row.
4. **Pane `TERM` from the terminal's owner** (above), which amends one sentence of HLA D4.
5. **Two tighter bounds**: the session status and pid-repair reads bounded at 10 seconds, and the
   batch status script elevated as a whole.

## Open questions

1. The operator question above.
2. Whether `vm backup` should record its launch id where a later command could find it. R6 says a
   later invocation can observe a launch by its recorded id; `observe` takes the id as data, and the
   id is in the operation log, but no command observes or resumes a backup, and none is proposed
   here, since no caller needs one.
3. The seven-day prune of finished launches is this LLD's choice, matched to the disk risk of
   today's staging, which Debian 13 ages out of `/var/tmp` after 30 days if the release notes are
   recalled correctly. A shorter bound frees disk sooner; a longer one keeps a finished launch
   observable longer, which nothing uses.
