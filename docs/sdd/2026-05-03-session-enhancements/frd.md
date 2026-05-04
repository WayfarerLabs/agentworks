# Session enhancements -- functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/sessions/`

## Background

Sessions run inside tmux servers on remote VMs. Each session is backed by a process (currently a
tmux server) whose lifecycle determines whether the session is alive.

The current status model is poorly defined. The database stores a simple running/stopped flag, but
there is no clear contract for what "running" means, how it is verified, or what operations should
do when the stored status disagrees with reality. Liveness is determined ad hoc by connecting to the
tmux server via SSH and running `tmux has-session`, but this check is entangled with tmux-specific
concerns (socket permissions, server-access ACLs, default vs per-agent servers) and is scattered
across every session command with slightly different error handling in each.

This weak foundation creates three concrete problems:

1. **Unreliable detection**: when socket permissions or tmux server-access ACLs drift, the
   `tmux has-session` check fails even though the session's process is alive. The CLI reports the
   session as stopped when it is actually running but inaccessible.
2. **No distinction between dead and broken**: a failed `tmux has-session` means either the process
   exited (dead) or it is running but the admin cannot connect (broken). The current model conflates
   these, making diagnosis impossible without manual investigation.
3. **Slow listing**: `session list` makes one SSH round-trip per session to check tmux. With many
   sessions across multiple VMs, this is visibly slow.

A recent attempt to address these issues, starting with the slow listing problem, (PR #32) added
sudo fallbacks, compound shell commands, a SessionState enum, force-kill helpers, and multiple code
paths for edge cases across stop, restart, delete, attach, logs, and list. Each fix exposed the next
edge case because every session command had its own approach to checking and reacting to status. The
attempt was abandoned because it treated symptoms instead of the root cause: status needs a clear,
centralized definition that all commands share, grounded in the process rather than the transport
used to reach it.

## Terminology

- **Session process**: the process that backs a session. Today this is a tmux server, but the model
  could accommodate other session types in the future.
- **Status**: the definitive liveness state of a session. Determined by a transport-specific
  connectivity test (`tmux has-session`) combined with a process-level check (`/proc/<pid>`).
  Returns one of four values: OK, STOPPED, BROKEN, UNKNOWN. This is the single concept all commands
  use. There is no separate "health" concept.
- **PID + boot ID**: internal implementation details stored in the database. The PID is the process
  ID of the session's tmux server. The boot ID is the VM's boot cycle identifier
  (`/proc/sys/kernel/random/boot_id`). Together they enable BROKEN detection (boot ID matches
  current boot AND process alive, but tmux unreachable) and force-kill (`--force`). A stale boot ID
  means the PID is from a previous boot and meaningless. They are never exposed to callers as a
  standalone liveness indicator.
- **PID_STOPPED (-1)**: sentinel value stored in the PID column to indicate the session is known to
  be stopped. Distinct from NULL (never checked, UNKNOWN). A session with PID_STOPPED can be
  restarted immediately without auto-repair.

## Requirements

### R1: PID and boot ID capture at session creation

When a session is created, the tmux server PID and the VM's boot ID must be captured and stored in
the database. For agent-mode sessions, the PID comes from the per-socket tmux server. For admin-mode
sessions, the PID comes from the admin's default tmux server.

The PID is retrieved immediately after session creation via:

```shell
tmux [-S <socket>] display-message -p '#{pid}'
```

The boot ID is read from:

```shell
cat /proc/sys/kernel/random/boot_id
```

Both run in the same SSH connection used for session creation, adding negligible overhead. On
restart, the new PID and current boot ID replace the old ones.

### R2: Unified status model

Session status is a single enumerated value determined by combining a transport-specific
connectivity test with a process-level check:

The status check depends on how the session connects to its tmux server. Today there are two models;
future models (e.g. admin sessions on dedicated sockets) can be added without changing the status
values or the command behavior table.

#### Dedicated socket (agent sessions)

The session has its own tmux server and socket file. The admin accesses it via group permissions.

| has-session | PID alive (same boot) | Status  | Meaning                                             |
| ----------- | --------------------- | ------- | --------------------------------------------------- |
| succeeds    | (not checked)         | OK      | Normal operation                                    |
| fails       | no (or stale boot)    | STOPPED | Session is not running                              |
| fails       | yes                   | BROKEN  | Running but admin cannot connect (permission drift) |

Check order: `has-session` first. If it fails, check boot ID (stale boot = STOPPED immediately),
then PID (process dead = STOPPED, process alive = BROKEN). BROKEN is a diagnostic edge case
indicating socket permission or ACL drift.

#### Shared default server (admin sessions)

The session runs on the admin's default tmux server alongside other admin sessions. The admin owns
the server, so socket permissions are not a concern.

| has-session | Status  | Meaning                                |
| ----------- | ------- | -------------------------------------- |
| succeeds    | OK      | Normal operation                       |
| fails       | STOPPED | Session ended (server may still be up) |

Check order: `has-session` only. No PID follow-up needed. BROKEN does not apply because the admin
owns the server.

#### Pre-enhancement sessions (no PID)

Sessions created before this enhancement have no PID in the database. Their status is UNKNOWN until
auto-repaired on first access (see R6).

Note that even though we could technically try the `has-session` check, we don't as we couldn't
properly distinguish the negative case (BROKEN vs STOPPED) without a PID. Instead, we treat all
NULL-PID sessions as UNKNOWN and auto-repair them on first access (see R6).

Auto-repair resolves most UNKNOWN sessions to OK or STOPPED. In rare cases (agent session with a
live server behind a socket the admin can't reach), auto-repair cannot determine the state and the
session remains UNKNOWN. Commands error in this case with a message to investigate manually.

### R3: Command behavior by status

Each session command should behave according to the session's status:

| Command  | OK                             | STOPPED            | BROKEN                 | UNKNOWN (no PID)                   |
| -------- | ------------------------------ | ------------------ | ---------------------- | ---------------------------------- |
| list     | Show running                   | Show stopped       | Show broken            | Auto-repair, then show status      |
| describe | Show details + status          | Show as stopped    | Show as broken         | Auto-repair, then show status      |
| stop     | Normal stop (C-c, grace, kill) | Already stopped    | Error, suggest --force | Auto-repair, then stop normally    |
| restart  | Confirm, restart (-y to skip)  | Normal restart     | Error, suggest --force | Auto-repair, then restart normally |
| delete   | Confirm, delete (-y to skip)   | Confirm, delete    | Error, suggest --force | Auto-repair, then confirm          |
| attach   | Normal attach                  | Error: not running | Error: broken          | Auto-repair, then attach or error  |
| logs     | Normal capture                 | Error: not running | Error: broken          | Auto-repair, then capture or error |

`--force` authorizes PID-based kill if needed (for BROKEN sessions where tmux is unreachable). On
non-BROKEN sessions, the command proceeds normally -- `--force` does not change behavior. `--yes/-y`
skips confirmation prompts (running session restart, delete). These are orthogonal.

### R4: Batch operations

#### Batch status checking

All batch operations (`session list`, `stop --all`, `restart --all`) check status for all sessions
on a VM in a single SSH round-trip. The implementation groups sessions by VM, builds a compound
`tmux has-session` command for each session (with the appropriate socket path), and runs one SSH
call per VM. VMs are queried in parallel.

For N sessions across M VMs, this reduces status checking from N SSH calls to M parallel SSH calls.

When has-session fails for any agent session, a follow-up boot ID + PID check determines STOPPED vs
BROKEN (boot ID first; stale = STOPPED without checking PID). The follow-up is folded into the same
compound command so that the entire batch completes in a single SSH call per VM. Admin sessions that
fail has-session are always STOPPED and need no follow-up.

`--no-status` skips all SSH checks and shows `-` for the status column.

#### Batch stop and restart

`session stop --all` stops all running sessions. `session restart --all-stopped` restarts all
stopped sessions. `session restart --all` restarts everything (prompts if any are running, unless
`--yes` is passed). All batch variants accept `--vm` and `--workspace` filters.

BROKEN sessions are warned and skipped unless `--force` is passed. UNKNOWN sessions are
auto-repaired before the batch proceeds.

### R5: Force escalation pattern

`--force` triggers PID-based kill escalation for BROKEN sessions. It is not used for running
sessions (those use confirmation prompts with `--yes/-y`). The escalation path:

1. Send `kill <pid>` (SIGTERM) to the tmux server process via sudo.
2. If the PID is still alive after a grace period, send `kill -9 <pid>` (SIGKILL).
3. If there is a socket file and the server is dead, remove the stale socket.

The force path is only reachable for BROKEN sessions, which already verified same-boot + PID alive
during the status check. A stale boot ID means the PID is treated as PID_STOPPED (-1) -- the session
is STOPPED, not BROKEN, so `--force` is never offered.

This path does not require tmux socket access or server-access ACL membership. It works through the
OS process management layer.

Without `--force`, these commands should simply error when the session is BROKEN, suggesting the
operator use `--force` to proceed.

Note: admin-mode sessions cannot be BROKEN (the admin owns the default server, no socket permissions
to drift). Since `--force` is exclusively for BROKEN, it is never offered for admin sessions. The
shared-server PID kill concern does not arise.

### R6: Auto-repair for existing sessions

Existing sessions created before this enhancement will not have a PID or boot ID in the database.
Rather than requiring an explicit repair command, recovery happens automatically when any command
accesses a session with a NULL PID:

1. Attempt `tmux has-session` to check if the session is alive.
2. If alive, recover the PID via `tmux display-message -p '#{pid}'` and store it with the current
   boot ID.
3. If not alive (admin session, or agent session with no socket): mark as stopped (`PID_STOPPED`).
4. If not alive (agent session with socket file present): probe the socket with sudo
   (`sudo tmux -S <socket> list-sessions`) to distinguish a stale socket from a live server the
   admin can't reach.
   - Stale socket (probe fails): mark as `PID_STOPPED`.
   - Live server, unreachable (probe succeeds): leave as NULL (UNKNOWN). Warn that permissions need
     investigation.

A warning is emitted so the operator knows the repair happened.

For batch commands (`session list`, `stop --all`, `restart --all`), all NULL-PID sessions are
auto-repaired before the batch proceeds. On restart, the new tmux server's PID and boot ID are
captured, replacing any previous values.

## Non-goals

- **Non-tmux session backends**: status checking is currently tmux-specific. The model accommodates
  future backends (containers, systemd units) by allowing backend-specific status implementations
  that return the same OK/STOPPED/BROKEN/UNKNOWN values.
- **Changes to session creation UX**: the `session create` command and template system are
  unchanged. PID/boot ID capture is an internal implementation detail.
- **Socket permission hardening**: the root cause of BROKEN state (permission drift) is a separate
  concern. This SDD diagnoses and surfaces the problem; it does not fix the underlying permission
  model.
