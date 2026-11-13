# Transport Take 2: Workspaces, Sessions and Backup Low-Level Design (Slice 7)

- Status: Revised after the project review and the complexity review; the operator's rulings of
  2026-11-12 and the lead's rulings on both reviews applied.
- Date: 2026-11-12
- Architecture: [hla.md](./hla.md) D5 (requests and results), D6 (files), D7 (detached work), D8
  (streaming, attach and the terminal handover), D13 (errors and the operation log), D14 (the
  guards), the Migration section and the incident inventory; requirements: R2, R3, R4, R6, R10, R11
- Plan: [plan.md](./plan.md), slice 7, delivered in PR C
- Source baseline: `723657d03` (the PR C branch head); no code changed since `c84250663`. Every
  `path:line` is at that commit. Paths are under `cli/agentworks/` unless they start with `cli/` or
  `docs/`.

## Scope

This LLD settles slice 7: `detached.py` and backup on it; the `FromFile` input, the `ToFile` sink
and the two terminal additions the consumers need (`TerminalCommand` and `park`); every workspace,
session, console and backup request that moves, each with its bound; session environment in the
request and the terminal handover on every console pane path; the console's pane secrets in its one
boundary pass (operator ruling, 2026-11-12); the deletions that become possible (the environment
newline refusals, the console's environment carry across sudo and its sudoers grant,
`_assert_agent_ssh_works`, `remote_exec.py`); and the incident audit of the entry points that move.

**Operator direction applied.** Every operation below has a caller in this slice. Unsupported setups
are refused with an alert rather than handled by new machinery (HLA, operator decision 10), and
related problems found on the way are fixed here (FRD, 2026-11-08).

**What exists at HEAD.**

- Workspaces already apply ACLs and remove a rolled-back tmuxinator file through `VMAccess`
  (`workspaces/backends/vm.py:195-196`, `:214-215`, `workspaces/manager/copy.py:206-207`,
  `rehome.py:210`, `:264`, `repair.py:220`); every other workspace request is on the legacy
  `transport()`.
- Sessions set up their socket directories and clean stale sockets through `VMAccess`
  (`sessions/tmux.py:274-477`, slice 6), and preflight, artifacts and group membership already run
  on it (`sessions/manager/_pids.py:91-119`, `_create_roll.py:215-216`, `:320`, `_queries.py:212`,
  `:225`). Everything else in `sessions/`, all of `sessions/multi_console/` and
  `multi_console_layout.py` is on the legacy stack.
- Backup runs on the legacy `SSHTransport`, its archive through `remote_exec.run_detached`
  (`vms/backup.py:357-379`), that module's only caller, with `quiet=True` (`:372`), so it reports
  nothing about a lost connection today; its download is a hand-built `scp` (`:454-518`).
- The legacy transport's `retries=` is an attempt count (`ssh.py:474`). Every caller in this slice's
  modules passes 1, its default (`ssh.py:336`): `GUEST_OBSERVATION_ATTEMPTS`
  (`status_observation.py:14`) and `DEDICATED_TEARDOWN_ATTEMPTS`
  (`sessions/manager/_teardown.py:30`). They pin one attempt, as the runnable-status FRD requires of
  session and console guest calls (`docs/sdd/2026-09-03-runnable-status-inspection/frd.md:95-96`);
  they are not a retry policy. `VMAccess` makes one attempt by construction (HLA D5), so both
  constants retire.

**Callers outside the plan's first list.** The definition of done (no workspace, session or backup
module imports a legacy module) reaches callers the plan does not name:

- Agent delete tears down an agent's sessions through `_teardown_session` and removes their console
  windows through `kill_session_windows`, both with a legacy target
  (`agents/manager/lifecycle.py:389-398`, `:423-425`). Both move with those helpers (batch C).
- `agents/manager/__init__.py:34` and `sessions/manager/__init__.py:19` re-export `transport` so
  tests can patch it; both re-exports and their patches go (batch C).
- `vms/manager/_helpers.py:306`, `vm describe`'s live resources, is the one remaining legacy caller
  outside this slice's modules. It moves in batch A as one bounded run (decision 1), so slice 8 has
  nothing left to migrate.

The session, workspace and backup modules also construct `SSHLogger`
(`sessions/manager/_create.py:138-140`, `_scope.py:124`, `_lifecycle.py:499`, `:695`,
`workspaces/realize.py:124`, `manager/copy.py:160`, `delete.py:91`, `rehome.py:208`,
`vms/backup.py:82`). They take it from `vms.manager.operation_log` instead (below, "Operation
logs"), so none of them imports `agentworks.ssh`; the logger itself and `LOG_DIR` stay slice 8's.

**Out of this slice.** The `AcceptEnv` and `env_keep` fragments of ADR 0014 (slice 8's search), the
operation log's move to `execution/oplog.py` (slice 8), and the other legacy modules.

## Module layout

| Module                                                                                                                   | Change                                                                                                                                                                     |
| ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `execution/requests.py`                                                                                                  | `FromFile`; `ToFile`; `TerminalCommand`                                                                                                                                    |
| `execution/access.py`                                                                                                    | `run` accepts `FromFile` and `ToFile`; `attach` takes `LoginShell \| TerminalCommand`; `park`; `detached()`                                                                |
| `execution/attach.py`                                                                                                    | The parking half becomes `Parked`, which `attach` and `park` share                                                                                                         |
| `execution/envelope.py`, `execution/agw_launch.py`                                                                       | `TerminalCommand` in `tty` mode; a `park` envelope carries no `TERM`, which `tty` mode then takes from its environment                                                     |
| `execution/pump.py`                                                                                                      | A file sink with a progress callback; nothing else (`FromFile` feeds the existing input thread)                                                                            |
| `execution/detached.py` (new)                                                                                            | `Launch`, `Detached`: `new_launch`, `launch`, `observe`, `interrupt`, `download`, `discard`                                                                                |
| `execution/detached_program.py` (new)                                                                                    | The guest program `launch`, `observe`, `interrupt` and `discard` run, a `PythonProgram`, as `file_program.py` is                                                           |
| `vms/manager/_helpers.py`                                                                                                | `_operation_log` becomes public `operation_log(..., path=None)`, re-exported by `vms.manager`; `_query_live_resources` on `VMAccess`                                       |
| `vms/backup.py`                                                                                                          | On `VMAccess`, `detached` and `ToFile`; no thread, staging, `chown` or `scp`                                                                                               |
| `remote_exec.py`, `cli/tests/test_remote_exec.py`                                                                        | Deleted (decision 2)                                                                                                                                                       |
| `workspaces/backends/vm.py`, `workspaces/manager/*.py`, `workspaces/realize.py`, `workspaces/tmuxinator.py`              | On `VMAccess`; copy is two `run`s over a tar stream                                                                                                                        |
| `sessions/tmux.py`                                                                                                       | Every primitive takes a `RunCommand` built by `access_run_command`; `_tmux_env_flags` goes; `deploy_restricted_config` takes `Files`; the launch line runs `/usr/bin/tmux` |
| `sessions/manager/*.py`                                                                                                  | On the owner's, the admin's or the elevated admin's access; `_BoundedStatusTarget` goes                                                                                    |
| `sessions/multi_console/*.py`, `sessions/multi_console_layout.py`                                                        | On the admin's access; pane secrets in the boundary; `_split_shell_pane` parks a handover per pane; the `--preserve-env` probe, `PreserveEnvMemo` and its threading go     |
| `agents/manager/lifecycle.py`, `agents/manager/__init__.py`, `agents/manager/_common.py`, `sessions/manager/__init__.py` | Agent delete's teardown and window cleanup on the admin's access; both `transport` re-exports and `_assert_agent_ssh_works` go                                             |
| `plugins/codex/recorder.py`, `plugins/codex/harness_integration.py`                                                      | The recorder is published with `Files.write` in `start`; `provision_fragment` goes                                                                                         |
| `vms/initializer/shell_env.py`, `driver.py`                                                                              | `_write_sudoers_console_setenv` becomes `_remove_sudoers_console_setenv`                                                                                                   |
| `manifests/decode.py`, `env/compose.py`, `env/show.py`, `secrets/orchestration.py`, `resolver.py`, `line_safety.py`      | The environment newline refusals go (below, "Deletions")                                                                                                                   |
| `status_observation.py`                                                                                                  | `GUEST_OBSERVATION_ATTEMPTS` goes                                                                                                                                          |

## Requests this slice adds

### No re-dispatch

HLA D5 planned an `observation` option, a caller's declaration that a read may be dispatched again
after `NotDelivered` or `Unknown`, with `detached.observe` and the status probes as its first
callers. Neither needs it, so it is not added (amendment A2):

- The session and console status reads keep the runnable-status FRD's bound, one attempt within 10
  seconds (`frd.md:95-96` there). A re-dispatch inside that bound would add nothing the FRD asks
  for.
- `detached.observe` is called from a polling loop, so a failed poll is simply followed by the next
  one; a re-dispatch inside it would be a second retry layer under the first.

So every request in this slice is one dispatch with its own deadline: 10 seconds
(`GUEST_OBSERVATION_TIMEOUT_SECONDS`, `status_observation.py:13`) for session and console status and
for pid repair, 30 seconds for `vm describe`'s live resources, and the bounds in the tables below.
`reachability.wait` stays the one loop that repeats a request, as today.

| Today                                                                                                                | Becomes                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GUEST_OBSERVATION_ATTEMPTS` (`sessions/manager/_status.py:52-53`, `:243-244`, `multi_console/_status.py:59`)        | Retired: one dispatch is what 1 meant. `GUEST_OBSERVATION_TIMEOUT_SECONDS` stays as the deadline                                                                                                                                                                       |
| `DEDICATED_TEARDOWN_ATTEMPTS` and `transport(vm, config, default_timeout=10, retries=1)` (`_lifecycle.py:1053-1057`) | Retired. Teardown never repeats a request (`68c837c1f` rules out repeating a kill or an uncertain observation); its own bounded absence poll (`_teardown.py:256-265`) stays the only repetition. `DEDICATED_TEARDOWN_TIMEOUT_SECONDS` stays as each request's deadline |
| `retries=3` with no bound (`vms/manager/_helpers.py:332`)                                                            | One run with a 30-second deadline. A VM that drops the one attempt shows no live resources, as one whose three attempts all failed does today                                                                                                                          |
| The legacy transport's `retries` parameter (`transports/base.py:66`, `ssh.py:416`)                                   | Deleted with the module in slice 8; after this slice nothing passes it                                                                                                                                                                                                 |

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

- `run` creates the file before dispatch with
  `os.open(path, O_WRONLY | O_CREAT | O_EXCL | O_BINARY, 0o600)` (`O_BINARY` where the platform has
  it, so Windows never translates newlines) and wraps the descriptor in a binary stream, so an
  existing path is a `ValidationError` with nothing sent. It writes each payload chunk as the pump
  delivers it, and calls `on_progress` with the running byte count after each write; callers
  throttle their own reporting.
- **The file exists afterwards only for `Completed(0)`.** For any other completion, and for any
  exception raised while the request runs (an operator's Ctrl-C included), `run` closes and removes
  it before returning or re-raising. That is incident row I20's rule, moved from backup's own `scp`
  wrapper (`vms/backup.py:508-518`) into the sink, so no caller rewrites it.
- The `Result` field for that stream is empty, as for `DiscardOutput`; the bytes are in the file,
  and the log never sees them.

First consumers: backup's download and workspace copy's source read.

### `TerminalCommand`

`TerminalCommand(argv: tuple[str, ...])`, with `env` and `cwd`, is a literal argv run on the
operator's terminal: valid only for `attach`, as `LoginShell` is. `attach` takes
`LoginShell | TerminalCommand`. In `tty` mode agw-launch execs it with its `argv[0]` searched in the
rebuilt `PATH`, after the same resets as a login shell (`agw_launch.py:163-183`), and refuses as it
does (127 for `exec ENOENT`, 126 for any other `exec` errno, 125 for `cwd`); there is no memfd, so
the descriptor close is harmless. The envelope accepts a handover token and `TERM` for it as for
`LoginShell` (`envelope.py:102-110`). First consumers: `session attach`
(`sessions/manager/_queries.py:961`) and `console attach` (`multi_console/attach.py:830`), which
today hand a command string to `Transport.interactive`.

### `park`: a handover for a pane

A console pane is a terminal agw does not hold: tmux creates it on the VM. It needs the parking half
of an attach without the terminal half.

```text
with access.park(LoginShell(cwd=full_path, env=pane_env)) as handover:
    admin.run(Command(("tmux", "split-window", ..., handover.command(run_as="agent-x"))))
# leaving the block waits for the pane to collect its request
```

- **Shared shape.** `attach.py`'s parking connection (`attach.py:65-70`) becomes `Parked`, a context
  manager over the `PipeSession` that waits for `parked` within `PARKING_WAIT_SECONDS` and raises
  the typed error, before anything else happens, when it does not come. `attach` is `Parked` plus
  the terminal half; `park` is `Parked` plus a `Handover`.
- **`Handover.command(run_as: str | None = None) -> str`** is the pane's command line: the handover
  form of stage 0, `stage0_command(socket_path(token))` (`ssh_process.py:73-86`), with
  `sudo -n -u <run_as>` in front when the pane's server belongs to another account. Its only values
  are the token and an account name; the environment is in the parked request.
- **Leaving the block.** On a normal exit the client keeps pumping the parking connection until its
  trailer, for at most `HANDOVER_COLLECT_SECONDS = 35` (the launcher's 30-second wait,
  `agw_launch.py:60`, and a margin), and sets `handover.collected` from it: `exit 0` is collected,
  `refused handover` is not. On an exception (the split failed) it closes the parking stdin at once,
  which the launcher treats as the client leaving and cleans up (`agw_launch.py:290-299`). Either
  way the socket directory is gone afterwards.
- **`TERM`.** An attach sends the operator's `TERM`, because the workstation owns that terminal. A
  pane's terminal belongs to tmux, which sets `TERM` in the pane's environment from its
  `default-terminal`, and sudo keeps `TERM` through its `env_reset`. So a `park` envelope carries no
  `TERM`, and `tty` mode takes `TERM` from its inherited environment for such an envelope, falling
  back to `DEFAULT_TERM` (`envelope.py:53`) when there is none. `park` has no `term` parameter: the
  rule belongs to the operation, not the caller. Today's panes get the same value, since
  `sudo --login` keeps it too (amendment A1).
- **sudo's pty.** sudo 1.9.14 and later, Debian 13's, defaults to `use_pty`, so an agent pane's
  shell runs on sudo's relayed pty, as today's `sudo --login` pane does; vector T2 runs on Debian 13
  to show the shell is interactive there.

`park` is valid for `LoginShell` only, since every pane runs a shell. An agent pane parks on the
agent's access, so the socket directory belongs to the agent: the pane's `sudo -n -u <agent>` stage
0 can reach it, and so can any of that agent's own processes, which HLA D8 accepts, since they can
already read the agent's processes.

## Detached work: `detached.py`

### API

`VMAccess.detached() -> Detached`, passive like `files()`.

| Operation                                    | What it does                                                                     | Request                                                                           | Deadline |
| -------------------------------------------- | -------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | -------- |
| `new_launch() -> Launch`                     | Chooses a fresh id; dispatches nothing                                           | none                                                                              |          |
| `launch(launch, command: Command) -> None`   | Starts `command` under the launch, returning once the payload has started        | `PythonProgram(DETACHED, ("launch", id))`, elevated; the spec on stdin            | 60       |
| `observe(launch) -> Observation`             | State, status, `work/` sizes, and the output tail once the launch is not running | `PythonProgram(DETACHED, ("observe", id))`, elevated                              | 30       |
| `interrupt(launch) -> Observation`           | Stops the payload's process group and returns the state after                    | `PythonProgram(DETACHED, ("interrupt", id))`, elevated                            | 30       |
| `download(launch, name, to: ToFile) -> None` | Copies `work/<name>` to the workstation                                          | `Command(("cat", "--", f"{launch_dir}/work/{name}"), elevated=True)`, `stdout=to` | none     |
| `discard(launch) -> None`                    | Removes the launch directory; an absent one is done                              | `PythonProgram(DETACHED, ("discard", id))`, elevated                              | 60       |

`Launch(vm_name, id)` is plain data. `new_launch` and `launch` are separate so the caller holds the
id before anything is dispatched, and its `finally` can discard by id whatever `launch` did.

`Observation(state, status, work: Mapping[str, int], output_tail: bytes)`, with `state` one of the
states below, `status` set only for `EXITED`, and `output_tail` (at most
`OUTPUT_TAIL_BYTES = 65_536` of the end of `output`) set only once the launch is not `RUNNING`. A
non-zero program status raises: `DetachedRefused` (a `StateError`) for an existing id at launch or a
running launch at discard, and `CommandFailed` otherwise.

`launch` takes a `Command` with `elevated=True`, `stdin=NoInput()`, no `cwd` and no `deadline`;
anything else is a `ValidationError` before dispatch, since a plugin holding an admin access can
reach it, and so are an argv or environment the envelope would refuse (`envelope.checked_argv` and
`checked_env`), since the spec travels on stdin where no envelope checks it. Detached work runs as
root under the root-only base, in its launch's `work/` directory, and is bounded by its observer,
not by a deadline. It takes no input: backup passes its paths as arguments (below), so nothing is
staged on the guest for it. Only `Command` is accepted because backup, the only caller, runs one
argv. An agent's access refuses the elevated request with `PermissionDenied`, as for any request.

### The base directory

Every launch lives in `/var/lib/agentworks/detached/<id>/`. The program creates the base on first
launch with explicit modes, `mkdir("/var/lib/agentworks", 0o755)` and
`mkdir("/var/lib/agentworks/detached", 0o700)`, accepting `EEXIST` for each: Lima's and GCE's
bootstraps already create `/var/lib/agentworks` root-owned 0755
(`capabilities/vm_platform/bootstrap_script.py:326`), and only root can create either directory, so
an existing one is root's own. agw-launch's umask is 022 (`agw_launch.py:254`), so the modes hold as
given. Nothing on the path is world-writable, which replaces today's predictable admin-owned
`/var/tmp/_aw_detached_*` and `/var/tmp/agentworks-backup-*` staging (`vms/backup.py:288`, `:339`,
`:352`).

A launch directory, mode 0700, holds:

| Entry     | Mode | Written by                                   | Content                                                                      |
| --------- | ---- | -------------------------------------------- | ---------------------------------------------------------------------------- |
| `process` | 0600 | `launch`, created locked and then renamed in | the payload's pid; its `flock` is held while the launch or the payload lives |
| `output`  | 0600 | the payload's descriptors 1 and 2            | everything the payload writes, both streams in one file                      |
| `status`  | 0600 | the supervisor, by rename                    | the payload's exit status (128 plus the signal for a signalled payload)      |
| `work/`   | 0700 | `launch`                                     | the payload's working directory; `download` reads here                       |

### States

`observe` reads the directory as one of four states. The lock is the process identity: a held
`flock` on `process` is held only through the launch's own descriptors, by the launching process
until it exits (step 7), the supervisor, and the payload's process tree, every descendant of which
inherits the descriptor (tar's `zstd` child included). So no pid is compared and a reused pid cannot
read as running. A descendant that left the payload's process group and outlived it would keep the
launch `RUNNING`; tar starts no such process, so this is accepted for backup.

**Read order.** `observe` tests the lock first and reads `status` only after finding the lock free.
The supervisor writes `status` (by rename) before it exits and releases its hold, so a free lock
with no `status` really is `LOST`; reading `status` first could straddle the supervisor's write and
exit and report a complete launch as `LOST`.

| State     | Evidence                                               | Means                                                                                                           |
| --------- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `ABSENT`  | no launch directory                                    | never launched, or discarded                                                                                    |
| `RUNNING` | `process` locked (a non-blocking shared `flock` fails) | the payload's process tree or its supervisor still lives                                                        |
| `EXITED`  | `process` unlocked, `status` present                   | the payload ended and its status was recorded                                                                   |
| `LOST`    | `process` unlocked or absent, no `status`              | nothing of the launch lives and no status was recorded: a reboot, both killed, or a launch that died setting up |

### Launch identity

`new_launch` chooses the id, `secrets.token_hex(16)`, on the workstation, and the program creates
the launch directory with `mkdir`, which fails on an existing name: an existing directory is
`DetachedRefused`, never adopted. So an independent new launch never matches an earlier one,
whatever its command or paths (R6), which is what `run_detached`'s reuse of a leftover status file
broke (`remote_exec.py:96-103`, `fd220164a`). The client checks the id as 32 lowercase hex digits
before anything is sent, since a plugin can build a `Launch`; the guest trusts what it is sent.

`launch` is a mutation and is never dispatched again. When its outcome is one of the connection
outcomes (`Unknown(CONNECTION)`, `Unknown(TRUNCATED)`, `Unknown(DEADLINE)`, the set
`reachability.POLLED` names, `reachability.py:35`), the request may or may not have reached the
guest, so `launch` makes one `observe` of the id: any state but `ABSENT` means it did launch, and
`launch` returns. `ABSENT`, or a follow-up `observe` that does not complete either, raises the
launch's own `ExecutionIncomplete`, with a hint naming the launch id ("it may still start on the
VM"). Every other outcome raises its typed error at once, since the payload never started
(`NotDelivered`) or the cause is not the connection.

### How a launch runs

1. Read the spec, a JSON object of `argv` and `env`, from stdin. The environment stays in memory: it
   reaches the payload's environment and no file.
2. Create the base, then the launch directory and `work/`.
3. Prune: remove every sibling launch directory, other than this one, that is not `RUNNING` and
   whose directory is older than seven days: `EXITED` and `LOST`, a directory a launch that died
   setting up left included. A killed `agw` can leave a multi-gigabyte archive behind, and nothing
   ages `/var/lib`; today's `/var/tmp` staging is aged out only on Debian 13 (its 30-day `/var/tmp`
   cleanup, recalled from its release notes, not measured), and never on Debian 12.
4. Create `process.new`, take `flock(LOCK_EX)` on it, and rename it to `process`, all before the
   fork, so `process` is never visible unlocked while anything of the launch lives. Until the rename
   the directory reads as `LOST`; a discard that removes it then makes the rename fail, and the
   launch ends with a failure before anything forks.
5. Fork once. The child calls `setsid`, so it belongs to no session and no terminal, points its
   descriptors 0, 1 and 2 at `/dev/null` and closes every other one but the lock and a report pipe,
   so it holds nothing of the connection. It is the supervisor.
6. The supervisor starts the payload with `subprocess.Popen`: `cwd` is `work/`, stdin `/dev/null`,
   stdout and stderr `output`, the environment the inherited one plus the spec's, with
   `start_new_session=True`, `restore_signals=True` and `pass_fds=(lock,)`. The inherited
   environment is the one agw-launch rebuilt for root (`agw_launch.py:116-138`). The payload's own
   session makes its pid its process-group id, which `interrupt` signals. Because the payload holds
   the lock's descriptor too, a supervisor killed while tar runs does not make the launch read as
   `LOST`. The supervisor writes the payload's pid into `process`, reports `started` over the pipe,
   and waits; on exit it writes `status` by rename. When the payload cannot be executed it writes
   the refusal's status and reports the `exec` errno, and the launch reads `EXITED`.
7. The launching process reads the report and exits: 0, or a refusal status naming the `exec`
   failure (`RequestRefused`'s codes). So `launch` returns only once the payload has started.

This is incident row I8 in its new form. There is no `nohup` and no `sudo` inside the wrapper,
because the whole operation is elevated (`sudo -n` is stage 0's prefix), and `setsid` with no
controlling terminal leaves nothing to deliver `SIGHUP`; the old `nohup` ordering
(`remote_exec.py:117-126`) has nothing left to order. The brace group's purpose, capturing the
output of everything the command runs (`remote_exec.py:34-44`, `fd220164a`), holds at the descriptor
level: every process the payload starts inherits `output` as both streams (amendment A5).

### Observing, and resuming after a dropped connection

Resuming is the caller's polling loop, not a mechanism: `observe` reads state and changes nothing,
so a poll that fails with `ExecutionIncomplete` costs nothing and the next one after the connection
returns reads the current state. Progress is `work/`'s file sizes; output is read once, as a tail,
when the launch has ended. Backup's loop (below) now reports a lost connection once, a reminder
every 30 seconds and the recovery; today it reports nothing (`quiet=True`, `vms/backup.py:372`), so
this is new behavior. It polls until the launch leaves `RUNNING` or the operator interrupts, with no
give-up bound, as today. Because the id names the launch, a later invocation could observe it the
same way (R6); no command does, since no caller needs one.

### Interrupt

`interrupt(launch)`, while `process` is locked, reads the payload's pid from it, sends `SIGTERM` to
that process group, waits up to `INTERRUPT_GRACE_SECONDS = 10` on the guest for the lock to be
released, sends `SIGKILL` to the group if it has not been and waits up to
`INTERRUPT_KILL_WAIT_SECONDS = 5` more, so both waits fit the request's 30-second deadline, and
returns the observation, which is `EXITED` unless a descendant outside the group still holds the
lock (above). The supervisor is in its own session, outside the group, so it survives and records
`128 + 15` or `128 + 9`. A launch that is not `RUNNING` is returned as it is with nothing signalled.
The pid is read only while the lock shows the launch alive, and again on each poll until the
supervisor has written it, so a launch interrupted before its payload started is signalled once it
does; the one residual window, a payload that exited while its supervisor writes `status`, signals a
group that no longer exists. It is never dispatched again. Backup calls it from its
`KeyboardInterrupt` handler, as it calls `kill -TERM -<pid>` today (`vms/backup.py:389-399`).

### Download and discard

`download(launch, name, to)` is a plain elevated run of `cat -- <launch dir>/work/<name>` into the
`ToFile` sink, which reports progress and removes a partial file (I20); a completion other than
`Completed(0)` raises its typed error. `name` must be one path component. Backup downloads only
after `observe` reported `EXITED` with status 0, so the file is complete and nothing writes it. It
has no deadline: the archive takes as long as the link needs, the keepalives end a dead connection
(I7), and Ctrl-C ends it.

`discard(launch)` removes the launch directory, refusing a `RUNNING` one; an absent one is done.

## Session environment and the terminal handover

### Sessions: environment in the request

Each session starts its own tmux server (dedicated socket), and tmux copies the server's environment
into its global environment at start, which every pane inherits. So the session's environment
travels as the launch request's `env`: `create_session` passes `launch_env`
(`sessions/tmux.py:910-917`) to `run_command(cmd, env=launch_env)` (`:990`), which
`access_run_command` makes a `Script` whose `env` agw-launch puts in the environment of the shell
that runs tmux, from which the server starts. `_tmux_env_flags` (`:630-648`) and its use (`:918`,
`:975`) go, so no `tmux` argument carries a value (HLA D8); neither does the SSH command line.

That environment now reaches the shell that runs the launch line and the tmux server itself, not
only the panes, so a session environment that sets `PATH` would change which `tmux` the launch line
finds. The launch line therefore runs `/usr/bin/tmux`, Debian's path (`tmux_cmd`, `:555`, takes the
binary for that one call); no other request carries a session's environment.

**The launch identity read-back.** `_matches_launch_attempt` reads `_AGW_SESSION_UUID` and
`_AGW_RUN_ID` with `show-environment -t <session>` (`sessions/tmux.py:1310-1311`), the session
environment `-e` set. They are now in the global environment, so the read becomes
`show-environment -g` only: no earlier release launched a session without `-e`, so there is nothing
to fall back for. The complexity review measured the `-g` read on tmux 3.3a. The expected payload is
unchanged.

The pane command is still `$SHELL -lic 'cd <path> && exec <command>'` (`sessions/tmux.py:651-675`),
now run by a server whose `SHELL` agw-launch took from the passwd entry.

### Console panes: a fresh handover for every pane

Every console pane that runs a shell is created by one of two places, and each parks a fresh
handover for each pane it creates:

- **`_split_shell_pane`** (`multi_console/tmux_build.py:158-318`), which every path that creates or
  recreates a shell pane reaches: console build and restart (`_build_console_tmux` through
  `_add_session_window`, `:456`), `console add-sessions` (`crud.py:358`), `console add-shell`
  (`crud.py:654`), and `console restore-session` for missing panes (`restore.py:407`) and for a
  missing window (`restore.py:204` through `_add_session_window`). The last is the recovery after a
  killed attach pane (`977d32c7c`). It composes `pane_env` as today (`_resolve_pane_env`,
  `sessions/multi_console/secrets_env.py:279-366`), parks `LoginShell(cwd=full_path, env=pane_env)`
  on the pane identity's access (the admin's, or the agent's for an agent pane), runs
  `tmux split-window ... <handover.command(run_as=agent or None)>` on the admin's access inside the
  block, and tags the pane.
- **The admin-shell window** (`tmux_build.py:529`), today `exec $SHELL -l` with no environment,
  which `sessions/multi_console/secrets_env.py:152-160` records as a known gap: its environment is
  resolved and never delivered. It now parks the admin pane's environment the same way and runs
  `new-session -d ... <handover.command()>`, closing the gap.

Windows that run the attach loop (`multi_console/attach.py:151-198`) carry no environment and keep
their command. No pane path passes `-e`, and none passes `env=` to the admin's `run`.

**A missing workspace directory** keeps today's behavior. Today the pane runs
`cd <path> || echo <diagnostic>; exec "$SHELL" -l` (`tmux_build.py:242`, `:248`), so it opens in the
home with a message. Now `park` with `cwd=full_path` is refused with `RequestRefused(cwd)` before
any pane exists (agw-launch enters the directory before parking, `agw_launch.py:255-258`);
`_split_shell_pane` then warns naming the path and parks again with no `cwd`, the home.

**When `park` raises.** Today a failed split warns and returns `None`, and callers decide whether
that is fatal (`restore.py:215-238`, `:404-424`; `crud.py:654-680`). An `ExecutionIncomplete`,
`CommandFailed` or `RequestRefused` (other than the `cwd` case above) from `park`, the split or the
tag is treated the same way, and so is an uncollected handover. `TrustMissing`, `TrustMismatch`,
`VMNotReady` and `PermissionDenied` propagate, since every later pane would fail the same way.

**Agent access first.** An agent pane now connects as the agent over SSH, which needs the agent's
`authorized_keys`, as an agent session already does. So each console operation calls
`_assert_agent_access_works(access, admin, agent)` (`agents/manager/access.py:44-89`) once for every
agent it will park for, before the operation's first mutation, and an agent that predates the
direct-SSH rollout gets that function's reinit hint instead of a failed pane. Today's `sudo --login`
panes did not need it; the upgrade guide says so.

**What replaces the sudo carry.** Today an agent pane runs
`exec sudo --login --preserve-env=<keys> -u <agent> bash -c '<bootstrap>'`
(`tmux_build.py:268-280`), which needs the `-e` values in its environment, `Defaults:<admin> setenv`
to let sudo keep them (`vms/initializer/shell_env.py:545-574`), and a per-operation probe that sudo
allows it (`tmux_build.py:80-155`). With the handover the environment never crosses sudo: the
agent's own connection parks it, the pane runs `sudo -n -u <agent>` with stage 0 and the socket
path, and stage 0, now the agent, fetches the request and execs the shell with it (HLA D8).
`sudo -n -u` needs only the admin's existing `NOPASSWD: ALL`. The `--preserve-env` path, the probe,
`PreserveEnvMemo` and its threading (`tmux_build.py:77`, `:549`, `crud.py:354`, `:671`,
`restore.py:213`, `:405`), and the sudoers grant all go (below, "Deletions").

### Console pane secrets in the boundary

The operator ruled on 2026-11-12 to resolve all pane secrets up front
([FRD, 2026-11-12](./frd.md#2026-11-12)). Console operations resolved their panes' secrets after the
VM gate, in a second pass ADR 0019 no longer sanctions now that PR B removed the Tailscale rejoin it
was likened to (`2cc0553da`;
`docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md:11-15`). The two live resolves
in `restore.py` (`:177-207`, `:372-399`) are the "commented post-bind resolve" of the plan's box:
live code, each justified by a comment as a conditional-need exception. Per command:

| Command                                   | Today                                                                                 | Becomes                                                                                                                                         |
| ----------------------------------------- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `console create`                          | resolves every pane's secrets after the gate, always (`crud.py:151-158`)              | `_console_build_secret_targets` (`sessions/multi_console/secrets_env.py:178`) passed as the boundary's `targets`; values from `resolver.values` |
| `console restart`                         | the same, always (`multi_console/attach.py:702`)                                      | the same                                                                                                                                        |
| `console start`                           | the same, but only when the console is not running (`attach.py:699-702`)              | the same, always: a start of a running console now resolves every pane's secrets and creates nothing                                            |
| `console restore-session`, window missing | every configured shell's secrets after the probes (`restore.py:186-203`)              | `_restore_session_secret_targets(..., indices=all)` (`secrets_env.py:237`) in the boundary; the late resolve goes                               |
| `console restore-session`, panes missing  | only the missing indices' secrets, after the validation guards (`restore.py:385-399`) | the same targets as above, every configured shell; the late resolve goes                                                                        |
| `console add-sessions`, `add-shell`       | resolve once, with no gate (`crud.py:306`, `:620`)                                    | unchanged                                                                                                                                       |

`_prepare_vm_target` (`multi_console/attach.py:345-389`) takes `targets` as a function of the
`VMRow`, since both builders need the row (`sessions/multi_console/secrets_env.py:178`, `:237`) and
`_prepare_vm_target` is where it is fetched (`attach.py:378`). So the not-found and
Tailscale-address refusals stay in one place before the gate, with nothing resolved, and it calls
the function and passes the result to `gated_vm_boundary`, whose `targets` parameter already exists
(`vms/manager/boundary.py:196`), then yields `resolver.values` beside the access. Its docstring's
"documented conditional-need path" (`:362-366`) goes. Every console resolve is then the one boundary
pass, before the first mutation, as ADR 0019 promises.

**The cost, stated plainly.** `console start` of a running console, which today asks for nothing,
resolves every pane secret and may prompt for each; a non-interactive `console start` of a running
console now fails unless every pane secret resolves. `restore-session` resolves every configured
shell's secrets even when one pane, or none, is missing, so a non-interactive restore fails on a
secret it would not have used, which `restore.py:376-384` avoided; and it resolves them before the
tag validation, so an operator with corrupt tags may be prompted before learning of it. The upgrade
guide states each.

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
- **Bounds.** A request keeps today's timeout as its `deadline`, and a request unbounded today stays
  unbounded, its dead connection ended by the keepalives (I7), except the session status and
  pid-repair reads, which take `GUEST_OBSERVATION_TIMEOUT_SECONDS` (decision 5). Every request is
  one dispatch.
- **Logging.** Every access is built with the operation's log, the agent's included: today every
  `agent_transport` in `sessions/` is built with no log, so agent-session launches and probes now
  reach the log, with environment names only (HLA D13). Stop-all, which today keeps no log at all,
  gets one per VM (`session-stop-all`); its concurrent teardown plans share it, and
  `SSHLogger._write` opens a handler per write and keeps it in an attribute (`ssh.py:297-320`), so
  the record callable `operation_log` hands out takes a lock around each write, which a test drives
  from several threads.
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
| Repair                        | `which setfacl`, `apt-get install acl`, `getent`, `groupadd`, `id -nG`, `usermod`, `chown -R -c`, `chmod -c`, `find`, `getfacl -R` twice, the traversal loop, git identity, grant sync, `repair.py:130-433` | `Command`s, `env={"DEBIAN_FRONTEND": "noninteractive"}` for `apt-get` as `packages.py` sets it; the group and loop lines `Script(Shell.SH)`                   | root, admin for git     |         | today's: 60, 120          |
| `remove_config`               | `rm -f <ws>/.tmuxinator.yml ~/.config/tmuxinator/<s>.yml`, `workspaces/tmuxinator.py:37`                                                                                                                    | `Command(("rm", "-f", "--", cfg, link))` on the admin's access; it takes `access: VMAccess`                                                                   | admin                   |         | 10                        |

The tmuxinator mode is 0600 because `write_file` copies a 0600 local temporary file with `scp`
(`transports/base.py:296-300`), which keeps that mode without `-p`; batch B confirms it on a VM
before relying on it. The `ln -s` is a `Command`, since a link is not a file operation. No caller
here needs an unconditional `remove`, which HLA D6 lists for workspaces (amendment A4).

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
2. **Destination.** `Command(("tar", "xzf", "-", "--no-same-owner", "-C", ws))`, elevated, with
   `stdin=FromFile(local)` and `check=True`, on the destination's admin access, no deadline. Nothing
   is staged on the destination VM, so the `mktemp`, the upload and the `rm -f` go.

**Extraction safety.** The archive comes from another VM, so the extraction relies on GNU tar's
defaults and never passes `-P` (`--absolute-names`): member names lose a leading `/`, members with
`..` are refused, and a link whose target is absolute or climbs with `..` is created as a
placeholder until the end of the extraction, so no later member is written through it.
`--no-same-owner` makes every extracted file root's rather than whatever owner the source recorded,
until the `chown -R` that follows; today root's extraction kept the source's owners in between.
While it runs, the destination directory is root-made 2770 for the admin and the workspace group
(`:171-178`), and the agents' grants are applied only afterwards (`:206-223`), so no agent can reach
the partial tree. The follow-ups do not follow links: `chown -R -P` and
`find -P ... -type d -exec chmod g+s` state the physical walk both already default to.

**An interrupt or a failure during extraction** leaves the partial tree in the new workspace
directory and no workspace row, since the row is inserted after the extraction (`copy.py:209`), as
today; the operator removes the directory before retrying.

The group, directory, `chown -R` and `find` steps (`:171-193`) are `Command`s as in the workspaces
table, and the tmuxinator write (`:230-233`) is as create's. The operation log is the destination's,
closed in a `finally` as today (`fbf3ed505`); the source read is recorded in it too. Copying within
one VM still goes through the workstation, as today.

### Sessions: the tmux primitives

`RunCommand` keeps its shape (`command, *, check, env`). Every caller builds it with
`access_run_command(access, *, deadline, elevated)` (`sessions/tmux.py:104-118`), so a primitive
does not know its identity or bound. The five that take a `Transport` directly
(`probe_tmux_server_pid`, `probe_process_start_ticks`, `read_boot_id`,
`capture_tmux_server_fingerprint` and `_matches_launch_attempt`, `:1185-1318`) take a `RunCommand`;
the four with a `sudo` parameter lose it, which the caller binds as `elevated` (`read_boot_id` never
elevates). `tmux_cmd`'s `sudo` (`:555`), which nothing passes, goes. Once every caller is on
`access_run_command` (batch D), `RunCommand` returns `CommandOutcome` rather than `object`, and its
docstring's `Transport.run` half goes.

| Primitive                                                                                    | Today                                        | Read-only | Notes                                                                                                                    |
| -------------------------------------------------------------------------------------------- | -------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------ |
| `deploy_restricted_config(files)`                                                            | `sudo mkdir`, `sudo tee` heredoc, `:551-552` | no        | `files.write(RESTRICTED_CONFIG_PATH, data, mode=0o644, parent_mode=0o755, elevated=True)`; one request, content on stdin |
| `require_socket_path`, `probe_tmux_session`, `probe_tmux_server`, the `_after_teardown` pair | `:602-611`, `:1080-1131`                     | yes       | One dispatch each; the `_after_teardown` pair's own second probe of an unknown result (`:1109-1112`) stays               |
| `_probe_pane_liveness`                                                                       | `:710-753`                                   | yes       | Its own two-try loop over blank or malformed replies (`:739`, `ab958a502`) stays                                         |
| `startup_protection_enabled`, `capture_output`                                               | `:1042-1057`, `:1132-1166`                   | yes       | Unbounded as today                                                                                                       |
| `create_session`                                                                             | `:937-990`                                   | no        | `env=launch_env` in the request; no `-e`; `/usr/bin/tmux`                                                                |
| `finalize_session_start`, `_grant_server_access`                                             | `:1001-1040`, `:613-628`                     | no        | Owner                                                                                                                    |
| `kill_session`, `kill_server`, `_raise_workload_died`'s `kill-server`                        | `:1059-1078`, `:788`                         | no        | One dispatch, never repeated                                                                                             |
| Fingerprint, boot id, start ticks, launch identity                                           | `:1185-1318`                                 | yes       | 10 seconds in status and pid repair; unbounded in teardown, as today                                                     |

### Sessions: the manager

| Operation                                     | Today                                                                                                                   | Access and elevation                                                                                           | Deadline                                                                                   | Replaces                                                                                           |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| Create: the agent probe                       | `_assert_agent_ssh_works` on an ephemeral `agent_transport`, `_create.py:195-196`, `_create_roll.py:210-211`            | `_assert_agent_access_works(access, admin, agent)` (`agents/manager/access.py:44`)                             | its own (`access.py:60`, `:67`)                                                            | the legacy probe                                                                                   |
| Create: config, launch, fingerprint, finalize | `_create_roll.py:398-471`                                                                                               | admin for the config; owner for the rest                                                                       | none, as today                                                                             | `deploy_restricted_config(run_command)`, `session_run_command`, `fingerprint_target`               |
| Create: rollback absence proof                | `_create_roll.py:486-487`                                                                                               | owner                                                                                                          | none                                                                                       | the same with `target=`                                                                            |
| Start, restart                                | `_lifecycle.py:502-942`                                                                                                 | admin (logged from the start, where today the first requests are not logged, `:502-503`), owner for the launch | none, except the status reads                                                              | the log-free `transport(vm, config)` and `_build_session_target`                                   |
| Status of one session                         | `check_session_status`, `_status.py:89-112`, and `_check_dedicated_session`, `:139-170`, unbounded                      | root for an agent session, admin for an admin session                                                          | 10                                                                                         | `partial(target.run, sudo=agent)`                                                                  |
| Status, bounded                               | `_BoundedStatusTarget`, `_status.py:50-56`, `:196-216`, `_queries.py:478-481`                                           | as above                                                                                                       | 10                                                                                         | `_BoundedStatusTarget`, which goes                                                                 |
| Batch status                                  | `batch_check_status`, `_status.py:240-247`, `input_data=` lines, an inner `sudo -n` per agent row                       | one root `Script(Shell.SH)` per VM, the rows on stdin as `Bytes`, no inner `sudo` (decision 5)                 | 10                                                                                         | the script's own `sudo -n tmux` and `sudo -n sh -c`                                                |
| Legacy rows in a status fan-out               | `_status.py:476`, `:498-500`, an unbounded probe on the main thread                                                     | owner, through `_assert_agent_access_works` for an agent                                                       | 10                                                                                         | `_build_session_target`'s `true`                                                                   |
| Pid repair                                    | `_repair_session_pid`, `_pids.py:122-422`, unbounded                                                                    | admin, root for an agent session                                                                               | 10 for every read                                                                          | `target.run`, `partial(target.run, sudo=agent)`                                                    |
| The agent probe in `_build_session_target`    | `_pids.py:86-87`                                                                                                        | `_assert_agent_access_works`                                                                                   | its own                                                                                    | `_assert_agent_ssh_works`                                                                          |
| Stop, delete, teardown                        | `_teardown.py:208-414`, `_lifecycle.py:117-153`, `:288-305`                                                             | owner; root when the caller does not own the session (agent and workspace delete, stop-all's agent rows)       | none for single stops; `DEDICATED_TEARDOWN_TIMEOUT_SECONDS` in stop-all's concurrent plans | `DedicatedTeardownPlan.target` and `sudo` become `access` and `elevated`                           |
| Stop-all                                      | `_lifecycle.py:1053-1078`                                                                                               | admin, root for agent rows                                                                                     | 10 in the concurrent plans                                                                 | `transport(..., default_timeout=10, retries=1)`; logged to a `session-stop-all` log per VM (above) |
| Attach                                        | `target.interactive("tmux -S S attach -t '=N'")`, `_queries.py:961`                                                     | admin; `TerminalCommand(("tmux", "-S", socket, "attach", "-t", "=" + name))`, or without `-S` for a legacy row | none                                                                                       | `Transport.interactive`                                                                            |
| Logs                                          | `capture_output`, `_logs.py:80`                                                                                         | admin                                                                                                          | none                                                                                       | the admin `run`                                                                                    |
| tmuxinator regeneration                       | `_scope.py:150-165`                                                                                                     | admin, `files.write` and `remove_config` as for workspaces                                                     | 10                                                                                         | `write_file` and `remove_config(target)`                                                           |
| `_scope._prepare_vm`                          | `_scope.py:124-128`: a log with no redactions, and a `run_as_root` no caller uses                                       | yields the admin's access; the log takes the operation's redactions                                            |                                                                                            | `run_command`, `run_as_root`                                                                       |
| Agent delete                                  | `_mgr.transport` into `_teardown_session` and `kill_session_windows`, `agents/manager/lifecycle.py:389-398`, `:423-425` | root for the teardown (`target_owns_session=False`), admin for the windows                                     | none                                                                                       | `_mgr.transport`                                                                                   |

**Two tighter bounds** (decision 5). `check_session_status` and pid repair are unbounded today, so a
VM that stops answering stalls `session stop`, `session delete`, `session attach` and every status
listing's repair pass. Their reads take the 10-second bound their bounded siblings already have
(`_pending_launch_is_owned`, `session_description`); a read that runs out reads as unknown, which
every caller already treats as fail-closed (`d193e48fd`). The batch status script loses its inner
`sudo -n` and runs elevated as a whole (HLA D4: elevation is the whole operation). That rests on
root being allowed to connect to an admin-owned tmux server, which tmux 3.3's `server-access` manual
reads as always allowed for the owner and root; it is not measured yet, so batch C's first test runs
the script as root against a real admin-owned tmux server, and if root is refused the batch is two
requests, the admin's rows unelevated and the agents' elevated.

### Consoles

Every console request runs on the admin's access (the console's tmux server is the admin's), except
an agent pane's parking connection and its access check, which run on the agent's.
`_prepare_vm_target` and `_live_target` (`multi_console/attach.py:345-408`) yield the admin's
access; the gate and the SSH boundary are as today, with the pane secrets as the boundary's targets
(above).

| Operation                                 | Today                                                                                                               | Request                                                                                      | Deadline                               |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------- |
| Presence, the post-teardown probe         | `has-session`, `multi_console/attach.py:207`, `:251`, `:287`                                                        | `Script(Shell.SH)`                                                                           | none                                   |
| Teardown, staging teardown, kill a window | `kill-session`, `kill-window`, `:244`, `:279`, `:332`                                                               | `Script(Shell.SH)`                                                                           | none                                   |
| Attach                                    | `target.interactive("tmux attach -t '=aw-console-<c>'")`, `:830-833`                                                | `attach(TerminalCommand(("tmux", "attach", "-t", "=aw-console-" + name)))`                   | none                                   |
| Build                                     | `new-session` for the admin-shell window, the placeholder, `kill-window`, `rename-session`, `tmux_build.py:529-579` | the admin-shell window through `park` (above); the rest `Script(Shell.SH)`, `check=True`     | none                                   |
| Session window                            | `new-window ... '<attach loop>'`, `tmux_build.py:426`                                                               | `Script(Shell.SH)`                                                                           | none                                   |
| Agent access check                        | none                                                                                                                | `_assert_agent_access_works`, once per agent, before the first mutation                      | its own                                |
| Shell pane                                | `split-window -e K=V ...` with `env=pane_env`, `:282`; tag, `:302`                                                  | `park` on the pane identity's access, then `split-window ... <handover command>` and the tag | parking: 60 to `parked`, 35 to collect |
| The `--preserve-env` probe                | `tmux_build.py:134`                                                                                                 | none: deleted                                                                                |                                        |
| Layout, focus, panes, reorder             | `multi_console_layout.py:76-444`, `tmux_build.py:334`, `restore.py:140`                                             | `Script(Shell.SH)`                                                                           | none                                   |
| Status                                    | `list-sessions`, `multi_console/_status.py:54`                                                                      | `Script(Shell.SH)`                                                                           | 10                                     |

### Backup

| Operation         | Today                                                                                         | Request                                                                                                                                                   | As    | Deadline |
| ----------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- | -------- |
| Agent uid         | `id -u <user>`, `vms/backup.py:137`                                                           | `Command(("id", "-u", user))`                                                                                                                             | admin | none     |
| Workspace gid     | `getent group <g>`, `:157`                                                                    | `Command(("getent", "group", g))`                                                                                                                         | admin | none     |
| Path present      | `test -d`, `:300`                                                                             | `Command(("test", "-d", path))`                                                                                                                           | root  | none     |
| zstd present      | `command -v zstd`, `:310`                                                                     | `Script("command -v zstd", Shell.SH)`                                                                                                                     | admin | none     |
| Size              | `du -sb ... \| awk`, `:318`                                                                   | `Command(("du", "-sbc", "--", *paths))`; the `total` line                                                                                                 | root  | none     |
| Archive           | `mktemp -d`, a staged path list, a second `mktemp -d`, `run_detached` on a thread, `:288-379` | `detached.launch(launch, Command(("tar", "--zstd", "-cf", "workspaces.tar.zst", "-C", "/", "--", *rel_paths), env={"ZSTD_CLEVEL": "15"}, elevated=True))` | root  | 60       |
| Progress and wait | the thread's join loop and `_report_size` every 30 s, `:381-388`, `:532-539`                  | `observe` every 5 s; the archive size from `work`, reported every 30 s                                                                                    | root  | 30       |
| Interrupt         | `cat <pid>`, `kill -TERM -<pid>`, `:389-399`                                                  | `detached.interrupt`                                                                                                                                      | root  | 30       |
| Download          | `chown` the archive to the admin, `stat`, `scp` with progress, `:426-433`, `:454-518`         | `detached.download(launch, "workspaces.tar.zst", ToFile(local, on_progress=...))`, after `EXITED` with status 0                                           | root  | none     |
| Cleanup           | three `rm -rf` and `rm -f`, `:435-449`                                                        | `detached.discard(launch)` in a `finally`                                                                                                                 | root  | 60       |

- **The archive's arguments.** `rel_paths` are the workspace paths without their leading `/`, as the
  staged list held them (`:335`), after `--` and after every option, so tar reads each as a member
  name and none as an option. Passing them as arguments removes the staged list and its `mktemp` and
  `mv` (`:337-342`), the risk of a truncated list on stdin, and any copy of the list left on the
  guest. Workspace paths are few and short, far below one argument's 131,071-byte limit (HLA D3).
- **Probes.** A probe that does not complete stores `None`, as `SSHError` did (`:143-144`,
  `:164-165`), catching `ExecutionIncomplete` only.
- **Outcomes.** `EXITED` with status 0 downloads, and a non-empty `output_tail` shows its last ten
  lines as warnings, as today (`:415-421`). `EXITED` with another status raises `BackupError` with
  the tail (`:407-411`). `LOST` raises `BackupError` saying the archive's process ended without
  recording a status, as the VM restarting would leave it; today a missing status reads as exit 1
  and raises the same error (`remote_exec.py:293-299`).
- **Cleanup.** The `finally` discards by the launch's id, whatever `launch` did, since `new_launch`
  chose it first: an `ABSENT` launch is done, and a discard that fails (the VM unreachable, or a
  payload still running after an interrupt that could not reach it) warns naming the id, and the
  prune removes the directory at the next launch on that VM seven days or more later.

The `chown`, the staged path list, the thread and the assertion that the target is an `SSHTransport`
(`:88`) go.

### Other callers

| Caller                                                             | Today                                                                                                                   | Becomes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The codex recorder (`plugins/codex/recorder.py:161-181`)           | A `mkdir`, `printf`, `chmod` and `mv` fragment in the pane command, as the session user (`harness_integration.py:1019`) | `start` publishes it with `access.files().write(home + "/" + _RECORDER_TAIL, lines, mode=0o755, parent_mode=0o755)` on the session's own access, `home` from the integration's artifact context (`artifact_context.home`, `harness_integration.py:691`), or from `HOME_PROBE` (`artifacts/session.py:51`) when the session has none. A failure warns and the launch goes on, as a failed fragment does today (`recorder.py:171-173`); `provision_fragment` and `_RECORDER_STAGE_TAIL` go |
| `vm describe`'s live resources (`vms/manager/_helpers.py:306-360`) | `retries=3`, no bound                                                                                                   | One `Script(Shell.SH)` on the admin's access, deadline 30 (decision 1)                                                                                                                                                                                                                                                                                                                                                                                                                   |

`Files.write` refuses a link at the recorder's path or a parent. Today's `mkdir -p` and `mv` follow
one; a linked `~/.agentworks` now makes the recorder absent with a warning, and codex falls back to
discovery, which is how a failed install already behaves.

## Deletions

In the order they land, each with its tests:

1. **`remote_exec.py`** (batch A, decision 2). Its only caller is backup (`vms/backup.py:357`),
   which moves to `detached`. The module and `cli/tests/test_remote_exec.py` go, and so do its names
   in `cli/tests/execution/legacy_modules.py:8` and `cli/agentworks/execution/README.md:277`; the
   static check covers `vms/backup.py`.
2. **`_assert_agent_ssh_works`** (batch C). Its three callers
   (`sessions/manager/_create.py:191-196`, `_create_roll.py:207-211`, `_pids.py:83-87`) call
   `_assert_agent_access_works(access, admin, agent)`; the function
   (`agents/manager/_common.py:245-294`) and its export (`agents/manager/__init__.py:42`, `:71`) go.
   Tests:
   - its own tests go: `cli/tests/test_agents.py:74-178` and
     `cli/tests/transports/test_ssh_trust.py:178-183`;
   - the patches of it move to `_assert_agent_access_works`:
     `cli/tests/test_session_transport.py:376-620`, `test_error_wrapper.py:758`,
     `artifacts/test_home_skill_lifecycle.py:96`, `artifacts/test_session_lifecycle.py:114`, `:316`,
     `:414`, `:537` and `sessions/test_create_start_restart_orchestrated.py:922`;
   - `test_execution_migrated_commands.py:95` already proves the replacement's three outcomes.
3. **Both `transport` re-exports** (batch C): `agents/manager/__init__.py:34` and
   `sessions/manager/__init__.py:19`, with the patches of them (`cli/tests/conftest.py:679-688`,
   `cli/tests/agents/test_delete_grant_revoke_orchestrated.py:153`), which patch
   `execution.vm_access` instead.
4. **`_BoundedStatusTarget` and `DEDICATED_TEARDOWN_ATTEMPTS`, and the sessions' use of
   `GUEST_OBSERVATION_ATTEMPTS`** (batch C). Tests: those that build the bounded target
   (`cli/tests/test_session_liveness.py:29`, `:226`), and the retry assertions in
   `cli/tests/sessions/test_round8_startup.py:21`, `:97` and
   `cli/tests/sessions/test_session_batch_stop_parallelism.py:22`, `:461-462`, which assert the
   deadline and one dispatch instead. The constant itself stays until console status stops using it
   (`sessions/multi_console/_status.py:12-13`, `:59`), in deletion 6.
5. **Session creation's `-e` flags** (batch C): `create_session` stops calling `_tmux_env_flags`
   (`sessions/tmux.py:918`, `:975`). Its tests in `cli/tests/test_sessions_tmux_create.py`,
   `secrets/test_quoted_logging.py` and `sessions/test_startup_recovery.py` assert instead that no
   launch request's argv or script holds an environment value. The function stays for the console
   panes until deletion 6.
6. **The console's environment carry and its sudoers grant** (batch D):
   - `_tmux_env_flags` (`sessions/tmux.py:630-648`), whose last callers are `_split_shell_pane`'s
     (`sessions/multi_console/tmux_build.py:213`, `:231`), and `GUEST_OBSERVATION_ATTEMPTS`
     (`status_observation.py:14`), whose last user is console status.
   - `_sudo_can_preserve_env`, `PreserveEnvMemo` and the `--preserve-env` branch
     (`sessions/multi_console/tmux_build.py:77-155`, `:268-280`), `_SUDO_PRESERVE_PROBE_VAR`
     (`sessions/multi_console/secrets_env.py:42`), and every `preserve_memo` parameter. Tests:
     `cli/tests/test_consoles_shell_panes.py`, `cli/tests/_tmux_model.py` and
     `cli/tests/test_tmux_model.py` lose their preserve-env cases and model.
   - `_write_sudoers_console_setenv` (`vms/initializer/shell_env.py:545-574`) and its call
     (`driver.py:387-388`) become `_remove_sudoers_console_setenv(files, log)`: one elevated
     `Command(("rm", "-f", "--", AGENTWORKS_SUDOERS_CONSOLE_SETENV_PATH))`. The installer owns that
     path outright, so removing it whole is right on create (nothing there) and on reinit (any
     release's fragment). A failure warns as a step does. Test: its writer test
     (`cli/tests/test_initializer_system_fragments.py:449`) becomes a recording test of that one
     request.
   - A recording test of console build, add-shell and both restore paths asserts that no `tmux` argv
     or script line holds any `pane_env` value, that no admin `run` carries `env`, and that each
     shell pane parked one handover on its identity's access.
7. **The environment newline refusals** (batch E, once no request sends environment by `SetEnv`,
   which holds once batch D lands, the console panes being the last):
   - the newline branch of `_env_entry_issues` (`manifests/decode.py:254-261`);
   - `compose_env`'s `require_line_safe_secret` (`env/compose.py:76-85`), which becomes
     `user_env[key] = values[entry.secret]`;
   - `validate_target_values` (`secrets/orchestration.py:203-240`) and its calls
     (`secrets/resolver.py:187-189`, `:199-201`, `orchestration.py:299`), whose only job is that
     refusal;
   - `LineOrientedSecretUse.ENVIRONMENT` and `ENVIRONMENT_REVEAL` (`secrets/line_safety.py:13-14`;
     decision 3). `env show`'s table (`env/show.py:500-534`) renders every value with `\` as `\\`
     and control characters escaped (`\n`, `\r`, `\t`, `\xNN`), so no value forges a row, and its
     reveal (`env/show.py:469-484`) shows a multi-line secret escaped rather than refusing it.
   - Tests: the refusal tests in `cli/tests/manifests/test_spec_workspace_template.py`,
     `test_secrets_orchestration.py`, `test_secrets_resolver.py`, `secrets/test_line_safety.py` and
     `test_env_show.py:536` go. One test resolves a secret holding a newline and a `%` and checks
     the composed environment holds it exactly; one `env show` test checks a value with a newline, a
     backslash and a control character renders on one row; and slice 2's conformance vector already
     delivers environment values byte-exact on the guest.
   - **NUL stays refused.** Secret resolution rejects it (`sample-config.toml:193`), and a plaintext
     value with a NUL is refused by the envelope at dispatch (`envelope.py:86`), as today: that is
     after the command's earlier steps, which may have changed state, so the replacement test also
     checks that a NUL value raises `ValidationError` before anything is sent.

## Behavior changes for the upgrade guide

- Session and console environment reaches the guest in the request: no value is on a `tmux` or
  `sudo` command line or in an SSH option, and environment values may now hold newlines.
- A console's admin-shell window now has the console's admin environment.
- An agent's console shell no longer needs the `51-agentworks-console-setenv` sudoers fragment for
  its environment, and reinit removes it. It does need the agent's own SSH access, as an agent
  session does: a console with an agent pane refuses an agent that predates the direct-SSH rollout,
  with the `agw agent reinit` hint.
- Console pane secrets resolve in one pass before the console changes: `console start` of a running
  console now resolves every pane secret (a non-interactive one fails unless they all resolve), and
  `console restore-session` resolves every configured shell's secrets even when few or no panes are
  missing.
- Agent sessions' requests are written to the operation log, with environment names only.
- `session stop`, `delete`, `attach` and pid repair bound each read of a session's state at 10
  seconds.
- `vm backup` stages nothing in `/var/tmp`: its archive is under `/var/lib/agentworks/detached`,
  removed when the backup ends; one a killed backup left is removed at the next backup on that VM a
  week or more later. It now says when it loses and regains its connection.
- `env show` escapes control characters and backslashes in values.

## Tests

None restates the implementation or our prose (`no-prose-policing-tests`).

**The plan's Tests box:**

| Plan item                                       | Test                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A fresh launch never matches an earlier one     | `test_detached_program.py` runs the program in-process against a temporary base: two launches of one command get two directories and each observation reports only its own status and output; a launch whose id already exists is refused and the earlier launch's files are unchanged; and, over the recording fake, a malformed id is refused before dispatch |
| Observation resumes across a dropped connection | Unit, over `RecordingVMAccess`: `observe` returning `Unknown(CONNECTION)` twice and then `EXITED` makes backup's loop report the loss once and the recovery once, launch nothing again, and download; and a supervisor killed while its payload runs still reads `RUNNING`. Live: backup with a forced disconnect                                               |
| Interrupt stops the work                        | In-process: a launch of `("sh", "-c", "sleep 600 & sleep 600")`; `interrupt` leaves no process in its group and the state `EXITED` with 143. Live: backup interrupted on WSL2 and Remote Lima                                                                                                                                                                   |
| The detached base is never world-writable       | In-process, under the sudo lane: the created `detached` directory is mode 0700                                                                                                                                                                                                                                                                                  |

**Unit, every CI host:**

- `ToFile`: an existing path is refused before dispatch; the file is opened in binary mode, so bytes
  with CR and LF arrive unchanged; it exists after `Completed(0)` only, and is gone after
  `Completed(1)`, `Unknown` and a `KeyboardInterrupt` raised by `on_progress` (I20).
- `launch`: after `Unknown(CONNECTION)` it returns when the follow-up observation sees the launch,
  and raises naming the id when it is `ABSENT` or the follow-up fails; after `NotDelivered` it makes
  no follow-up; a request with `stdin`, `cwd` or a `deadline`, or not elevated, is refused.
- The program's states: `RUNNING` while the lock is held, from before the payload starts, `LOST`
  when it is released with no status, `EXITED` with a `process` after an `exec` failure, and the
  prune removing old `EXITED` and `LOST` directories, one with no `process` included, but no
  `RUNNING` one. A discard that wins the race with setup leaves the launch failed and nothing
  started. The read order: an observer stopped between its lock test and its `status` read while the
  supervisor writes `status` and exits reports `RUNNING` or `EXITED`, never `LOST`.
- `interrupt` of a payload that ignores `SIGTERM` returns `EXITED` with 137 after the kill wait.
- `park`: a split that raises closes the parking stdin at once; an uncollected handover reports
  `collected=False`; `Handover.command` puts `sudo -n -u` in front only for another account; a `cwd`
  refusal parks again in the home and warns.
- Backup's outcomes: `LOST` and a non-zero `EXITED` raise `BackupError`; the `finally` discards the
  launch's id even when `launch` raised.
- Pane secrets in the boundary (decision 6), one pin each for `console create`, `restart`, `start`,
  and `restore-session` with a missing window and with missing panes: the command registers its
  targets on the boundary resolver, makes no `resolve_for_command` call, and resolves before its
  first mutation (no tmux request and no database write precede the resolve). Two refusal pins, a
  console not found and a VM with no Tailscale address, resolve nothing and dispatch nothing.
- Stop-all's shared log takes every concurrent plan's records without interleaving.

**Tests that change in batch D because their path no longer calls `resolve_for_command`:**
`cli/tests/test_console_lifecycle.py:161`
(`test_restart_resolves_inputs_before_destroying_running_runtime`), whose invariant is re-pinned
through the boundary, not dropped; `test_secrets_eager_resolve_console.py:149`, `:277` and `:519`,
which patch the late resolve; and `test_consoles_orchestration.py:89`, a start on a running console,
which now resolves every pane secret, the accepted cost.

**The guest programs.** `test_detached_program.py` beside `test_file_program.py`, on Linux CI, both
interpreters. agw-launch's `TerminalCommand` and the inherited `TERM` join the conformance suite as
vectors T1 (an argv on the terminal, with its refusals) and T2 (a pane handover through `sudo -n -u`
under the sudo lane, with `TERM` from the environment and `DEFAULT_TERM` without it), T2 also on a
Debian 13 VM.

**tmux.** Tests against a real tmux (skipped where none is installed), the first of batch C: the
batch status script runs as root against an admin-owned server (decision 5); and a session launched
with its identity in the request's environment is matched through `_matches_launch_attempt`'s
`show-environment -g`.

**Callers.** Session, console, workspace and backup tests that build MagicMock transports and assert
command strings move to `RecordingVMAccess` and `local_files` (`cli/tests/vm_access_support.py`),
keyed by request builder, as slice 6's did (`init-lld.md`, "Tests"). Each batch adds the files it
migrates to the static check (`MIGRATED_FILES`, `cli/tests/test_execution_import_boundary.py`).

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
- **I8** (whole-operation elevation and `setsid` replace `nohup` outside `sudo`; everything the
  command runs is captured). Re-proved by `test_detached_program.py`: a payload that writes to both
  streams from itself and a child has all of it in `output`, and the payload is in a session of its
  own; live by backup surviving a forced disconnect.
- **I19** (an unreachable VM told from an agent that predates the rollout): its function is now
  `_assert_agent_access_works` (`agents/manager/access.py:44-89`) for sessions and consoles too
  (amendment A6); re-proved by the session and console tests that patch it and by
  `test_execution_migrated_commands.py:95`.
- **I20** (the download stops and removes the partial file on Ctrl-C): now the `ToFile` sink's rule,
  re-proved by its unit tests and live by an interrupted download.
- **I57** (a dead workload's capture and cleanup are best effort): `_raise_workload_died`
  (`sessions/tmux.py:755-801`) keeps its structure; re-proved by its tests moved to
  `RecordingVMAccess`, with `capture-pane` and `kill-server` returning `Unknown` and
  `CommandFailed`.
- **I21** and **I6** (terminal restore and the attach notice): session and console attach go through
  `VMAccess.attach`, which slice 2 proved; re-proved live by the attach cells.
- **I67** (workspace ACLs) and **I75** (stale-socket cleanup): unchanged (slice 6).

**New rows, I80 to I89**, added to the HLA's inventory in batch A, before anything is deleted, and
re-proved in the batch that moves their code:

| Row | Behavior                                                                                                                                                                                                                                                                                                                                                                             |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| I80 | A detached launch never adopts an earlier launch's result or files: each launch has a fresh id and the guest refuses an existing directory (`fd220164a`; `remote_exec.py:96-103`; R6)                                                                                                                                                                                                |
| I81 | Detached work survives a dropped connection: its observer keeps polling, and the status and output are read once the connection returns (`ae9df9af6`; `remote_exec.py:147-154`, `:222-281`)                                                                                                                                                                                          |
| I82 | Interrupting a backup stops the remote archive's whole process group, and the backup's guest staging is removed on every path (`97e11a168`; `vms/backup.py:389-399`, `:435-449`)                                                                                                                                                                                                     |
| I83 | Backup's guest staging is root-owned and private, never a predictable path in a world-writable directory (`97e11a168`; `vms/backup.py:287-288`, `:337-353`)                                                                                                                                                                                                                          |
| I84 | A session or console probe that could not run reads as unknown, never as absent; teardown signals a tmux server only when its pid, boot id and start ticks match the stored fingerprint, and never repeats a kill or an uncertain observation (`d99835dac`, `d193e48fd`, `2fd330832`, `2713928c6`, `68c837c1f`; `sessions/tmux.py:155-245`, `sessions/manager/_teardown.py:208-414`) |
| I85 | Runtime recovery binds an unacknowledged launch to its exact session through the launch identity it carried, `_AGW_SESSION_UUID` and `_AGW_RUN_ID` (`b985953a5`; `sessions/tmux.py:1294-1318`)                                                                                                                                                                                       |
| I86 | A session launch's pane capture and failure text have the session's secret values removed before they reach an error or the operation log (`9ff646d14`; `sessions/tmux.py:755-801`, `sessions/manager/_create.py:138-140`, `_create_roll.py:638-642`, `_lifecycle.py:38-48`)                                                                                                         |
| I87 | A failed workspace clone raises a value-free error, since the guest's stderr and the command can hold credentials in the repository address (`78011cce9`; `workspaces/backends/vm.py:132-149`)                                                                                                                                                                                       |
| I88 | Session and console status is bounded per VM, one attempt within 10 seconds, so a VM that does not answer reads as unknown without stalling the listing (`8475c8a80`, `3edd3f8d1`, `c666b6184`; `status_observation.py:13-14`, `sessions/manager/_status.py:240-247`; runnable-status FRD R9)                                                                                        |
| I89 | The codex recorder is published atomically and never fails the launch (`plugins/codex/recorder.py:161-181`)                                                                                                                                                                                                                                                                          |

**How each is re-proved, by test.** I80, I81 and I8 by `test_detached_program.py` and the `detached`
unit tests above; I82 by a backup unit test that raises `KeyboardInterrupt` from the observe loop
and records `interrupt` then `discard`, and live; I83 by the base test; I84 by the teardown and
status tests moved to `RecordingVMAccess`, with a recording assertion that the kill is dispatched
once, that no other teardown request is repeated beyond the code's own explicit second looks (the
bounded absence poll, `_teardown.py:256-265`, and `_stable_absence_probe`'s one re-probe of an
unknown result, `sessions/tmux.py:1109-1112`), and that an unknown probe never removes anything; I85
by the real-tmux test above; I86 by `cli/tests/sessions/test_logging_and_capture.py` moved; I87 by a
backend test whose clone fails with the URL in its stderr; I88 by a status test whose VM returns
`Unknown(DEADLINE)`, which reads as unknown after one dispatch; I89 by a codex test that the
recorder is one `write` with mode 0755 and that a `PathRefused` warns and the launch goes on.

**Not rows.** The console's `--preserve-env` probe (`dc0573ac9`, ADR 0017) is deleted, not
preserved; its failure mode cannot occur without the sudo crossing. The unbounded legacy calls this
slice bounds are a deliberate change (decision 5). The tmux target quoting for zsh (`211edd0b2`,
`exact_tmux_target`) is unchanged and still applies, since the console's server runs pane commands
through the admin's shell.

### ADR 0017

ADR 0017 is superseded in batch D. Its status line becomes "Superseded by the terminal handover
(transport take 2, HLA D8; `sessions-lld.md`)", and a short section says what replaced each
decision: the environment travels in the pane's parked request, so nothing crosses sudo; reinit
removes the `setenv` grant; the probe and memo are gone; and its first Negative, values on the
`tmux -e` argv, no longer applies. Its own text stays as the record. ADR 0014's Status paragraph,
which cites 0017's refinement and says `SetEnv` remains for paths not yet migrated, is amended in
the same batch: 0017 is superseded and no request sends environment by `SetEnv`; whether the sshd
fragment keeps a user stays slice 8's search.

## Collateral

Each item lands in the batch that makes it stale:

| Collateral                                                                                                                                                                                                                                                                                               | Change                                                                                                                                              | Batch                     |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| `cli/agentworks/execution/README.md`                                                                                                                                                                                                                                                                     | `FromFile`, `ToFile` and `detached`, with the base and its states; then `TerminalCommand` (C) and `park` (D)                                        | A, C, D                   |
| `docs/guides/upgrading-to-0.20.md`                                                                                                                                                                                                                                                                       | `:113-114` and the "older SSH path (sessions, workspaces and backups)" mentions (`:143`, `:244-245`) as each moves; the behavior changes above      | A to E                    |
| `docs/guides/session-status.md:30-33`                                                                                                                                                                                                                                                                    | Each guest call is one request with a 10-second deadline; the closed-stdin sentence goes, since every request's stdin is the request                | C                         |
| `docs/guides/runnable-status.md:96-98`                                                                                                                                                                                                                                                                   | Observers use `agentworks.execution`, one dispatch within 10 seconds; the `tty=False` and closed-stdin sentence goes (sessions in C, consoles in D) | C, D                      |
| `docs/arch/runtime-context.md:105-110`                                                                                                                                                                                                                                                                   | No runtime environment travels by `SetEnv`, so the expansion caveat and its pointer to #873 go                                                      | D                         |
| `docs/guides/idempotency.md:91`                                                                                                                                                                                                                                                                          | The "sudoers console setenv" row says reinit removes the fragment                                                                                   | D                         |
| ADR 0017, ADR 0014's Status paragraph                                                                                                                                                                                                                                                                    | Above                                                                                                                                               | D                         |
| `cli/agentworks/sample-config.toml:193-195`                                                                                                                                                                                                                                                              | Env injection leaves the list of line-oriented consumers                                                                                            | E                         |
| Docstrings: `sessions/tmux.py:66-70`, `:74-80`, `:651-675`; `sessions/multi_console/tmux_build.py:183-203`; `sessions/multi_console/secrets_env.py:152-160`; `multi_console/attach.py:362-366`; `vms/backup.py:268-285`; `plugins/codex/recorder.py:13-16`, `:50-55`; `harness_integration.py:1002-1009` | Describe the request, the handover and `detached` rather than `SetEnv`, `-e`, `--preserve-env` and `run_detached`                                   | the batch that moves each |

Completions: none change. **Contracts.** `ToFile`, `FromFile`, `TerminalCommand`, `park` and
`detached()` are additive on `VMAccess`, which plugins reach through their run context; every kind
was bumped once for this release already (`init-lld.md`, "Collateral"). No bump and no contract
footer. Nothing here touches the command reference's JSON issue-code contract, which the operator's
2026-11-12 ruling has amended separately.

## Live evidence

| Check                                                                                                                                                      | Cells                                           | Rows                 |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | -------------------- |
| Session create, attach, detach, stop, start and delete, admin and agent, with a secret holding a newline in the session environment                        | Remote Lima from Linux, macOS and Windows; WSL2 | I21, I85, decision 3 |
| Console create, attach, detach, restore-session for a missing pane and a killed attach pane, add-shell for an agent, with agent-scope secrets in that pane | Remote Lima from all three; WSL2                | I6a, I6, I19         |
| An agent pane's shell is interactive and has `TERM` (vector T2)                                                                                            | a Debian 13 VM                                  | decision 4           |
| Kill a console attach's `ssh`, then re-attach                                                                                                              | Remote Lima from one workstation                | I6a                  |
| `session list` with one VM stopped returns within the bound                                                                                                | one cell                                        | I88                  |
| Backup with a forced disconnect during the archive and during the download                                                                                 | WSL2 (with idle shutdown) and Remote Lima       | I8, I81, I25         |
| Backup interrupted during the archive and during the download                                                                                              | WSL2 and Remote Lima                            | I82, I20             |
| Workspace create with a clone, copy between two VMs, rehome and repair                                                                                     | one cell                                        | I67, I87             |
| `vm reinit` on a VM that has the setenv fragment removes it                                                                                                | one cell                                        | deletion 6           |

I57, I80, I83, I84, I86 and I89 depend on failures or races a live run cannot provoke safely, so
they have tests and no live cell.

## Delivery

Five batches, each green on its own, in the order they land in PR C. The rule from slice 6 holds:
**a batch that changes a shared helper moves every caller of it**, and each bridge is named with the
batch that removes it. Each batch adds its migrated files to the static check and carries the
collateral it makes stale.

| Batch                            | Contents                                                                                                                                                                                                                                                                                                                                                                                  | Size                                                                    |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| **A. Detached work and backup**  | The inventory rows I80 to I89 in the HLA, first; `ToFile`; `detached.py` and `detached_program.py` with the plan's four tests; `operation_log`; backup on them; `vm describe`'s live resources; `remote_exec.py` and its tests deleted                                                                                                                                                    | M: about 450 lines of code, 550 of tests                                |
| **B. Workspaces**                | `FromFile`; workspace backends, copy, rehome, repair and delete's own requests; `tmuxinator.py` with its session caller                                                                                                                                                                                                                                                                   | M: 400 and 500                                                          |
| **C. Sessions**                  | The real-tmux tests, first; `TerminalCommand`; every tmux primitive on `access_run_command`; create, start, restart, status, pid repair, teardown, stop-all, attach and logs; `kill_session_windows` with its callers; agent delete; `_assert_agent_ssh_works`, both `transport` re-exports, `_BoundedStatusTarget` and `DEDICATED_TEARDOWN_ATTEMPTS` deleted; the codex recorder         | XL: 1,100 and 1,400; can split at status and pid repair if review wants |
| **D. Consoles and the handover** | `park` and `Parked`; pane secrets in the boundary; the agent access check; the pane and admin-window handovers; every console request; console attach; the `--preserve-env` carry, `_tmux_env_flags` and `GUEST_OBSERVATION_ATTEMPTS` deleted; `_remove_sudoers_console_setenv`; ADR 0017 superseded and ADR 0014 amended; the slice 6 test bridges and the legacy-target bridges removed | L: 650 and 750                                                          |
| **E. Closure**                   | The newline refusals and `ENVIRONMENT_REVEAL`, with `env show`'s escaping                                                                                                                                                                                                                                                                                                                 | S: 120 and 200                                                          |

**Bridges.**

| Bridge                                                                                                                                                                        | Left by | Removed in                                     |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | ---------------------------------------------- |
| `_scope._regenerate_tmuxinator` builds an admin access beside its legacy target for the tmuxinator write and `remove_config`                                                  | B       | C                                              |
| `delete_workspace` keeps a legacy target for session teardown and `kill_session_windows`                                                                                      | B       | C                                              |
| The console modules pass their legacy `target.run` as a `RunCommand`, and `crud.remove_sessions` builds an admin access beside its target for `kill_session_windows`          | C       | D                                              |
| Slice 6's test bridges `route_vm_access_to_target` and `files_over_target` (`cli/tests/vm_access_support.py`), which run a moved step's requests on a legacy transport double | slice 6 | D, with the last legacy target                 |
| `tmux.CommandOutcome`'s duck-typed mirror of `SSHResult`: `RunCommand` returns `object` while legacy `Transport.run` can be one                                               | slice 6 | D, where `RunCommand` returns `CommandOutcome` |

## Decisions

The lead accepted each on 2026-11-12, and the operator ruled on the pane secrets.

1. **`vm describe`'s live resources move in batch A** as one run bounded at 30 seconds, so slice 8
   has no legacy caller left to migrate.
2. **`remote_exec.py` and its tests are deleted in batch A**, once backup no longer calls it. The
   lead edits slice 8's box.
3. **`ENVIRONMENT_REVEAL` retires** with the environment refusal, and `env show` escapes every
   value, `\` as `\\` included, since plaintext values with newlines can now reach its table.
4. **Pane `TERM` comes from the terminal's owner**: only a `park` envelope carries no `TERM`, and
   `tty` mode then takes it from the inherited environment, falling back to `DEFAULT_TERM`.
5. **Two tighter bounds**: the session status and pid-repair reads at 10 seconds, and the batch
   status script elevated as a whole, gated on the real-tmux root-connect test that opens batch C.
6. **Pane secrets in the boundary** (operator, 2026-11-12), as tabled above.
7. **No re-dispatch option.** Every read is one dispatch with its deadline (complexity review).
8. **Detached work takes no input and no working directory**, and identifies its process by a held
   lock (complexity review). Passing backup's paths as arguments puts every option before `--` and
   the members after it, which also settles the review's argument-order finding, and leaves no
   truncated-stdin case and no stdin file on the guest to retain.

Not taken: streaming tar straight to the workstation instead of detaching it. R6, survival across a
disconnect, is the operator's requirement.

## Amendments

Edits this LLD needs in the HLA and the plan, for the lead to make:

- **A1, HLA D4** (agw-launch step 4): `TERM` comes from the envelope, except for a `park` envelope,
  which carries none; `tty` mode then takes it from the inherited environment, set by the terminal's
  owner (tmux), or `DEFAULT_TERM`.
- **A2, HLA D5**: the "Retries" bullet's `observation=True` option, "an option that arrives with its
  first caller, `detached.observe` in slice 7", goes: `VMAccess` never dispatches a request twice,
  reads are single dispatches with their deadlines, and repetition is the caller's loop
  (`reachability.wait`, a detached observer). The table gains `TerminalCommand` (valid only for
  `attach`, beside `LoginShell`), and D8 gains `park` and `Parked`, the parking half shared by
  `attach` and console panes.
- **A3, HLA D7**: launch runs a core-owned Python program that forks a supervisor with `setsid` (no
  `nohup` wrapper); `launch` takes an elevated `Command` with no input, working directory or
  deadline, under a `Launch` the caller creates first; `observe(launch)` returns the state, `work/`
  sizes and the output tail, with no offset; `stop` is `interrupt`; `download` and `discard` are
  operations; process identity is a held lock.
- **A4, HLA D6**: the `remove(path)` row's consumer "workspaces (7)" goes; no slice 7 caller needs
  an unconditional remove.
- **A5, HLA inventory row I8**: "the output-capturing brace group is kept" becomes "the payload's
  descriptors capture everything it runs".
- **A6, HLA inventory row I19**: its citation `agents/manager/_common.py:237-286` becomes
  `agents/manager/access.py:44-89`, `_assert_agent_access_works`, now its only implementation.
- **A7, plan, slice 7**: the box "Add the `observation` request option ..." becomes "Bound the
  session and console status reads and pid repair at 10 seconds and `vm describe`'s live resources
  at 30, each one dispatch, and retire `GUEST_OBSERVATION_ATTEMPTS` and
  `DEDICATED_TEARDOWN_ATTEMPTS`". The Migrate box gains agent delete's session teardown and window
  cleanup and `vm describe`'s live resources. The "Settle with the operator ..." box closes: the
  pane secrets by the 2026-11-12 ruling, and the `agw vm reinit` hint half because
  `vms/applied_state.py:262-273` already gives it, reached through `vms/manager/boundary.py:87-104`,
  with `vm reinit` accepting that state (`vms/manager/reinit.py:231`).
- **A8, plan, slice 8**: `agentworks.remote_exec` leaves the delete list, deleted in slice 7.

## Open questions

1. Whether `vm backup` should record its launch id where a later command could find it. R6 says a
   later invocation can observe a launch by its recorded id; `observe` takes the id as data, and the
   id is in the operation log, but no command observes or resumes a backup, and none is proposed
   here, since no caller needs one.
2. The seven-day prune is this LLD's choice. A shorter bound frees disk sooner; a longer one keeps a
   finished launch observable longer, which nothing uses.
