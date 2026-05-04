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
  is defined in terms of a PID so it could apply to other process types in the future.
- **Session PID**: the process ID of the session process. For admin-mode sessions, this is the admin
  user's default tmux server. For agent-mode sessions, this is the per-socket tmux server created
  for the agent.
- **Status** (binary): whether the session process is alive, determined by checking `/proc/<pid>`.
  Does not require tmux socket access, sudo, or group membership. Answers "is the process running?"
- **Health** (enumerated): the full picture of session accessibility, determined by combining PID
  liveness with a transport-specific connectivity test (currently `tmux has-session`). Answers "can
  we interact with the session?"

## Requirements

### R1: PID capture at session creation

When a session is created, the tmux server PID must be captured and stored in the database. For
agent-mode sessions, the PID comes from the per-socket tmux server started during creation. For
admin-mode sessions, the PID comes from the admin's default tmux server.

The PID is retrieved immediately after session creation via:

```shell
tmux [-S <socket>] display-message -p '#{pid}'
```

This runs in the same SSH connection used for session creation, adding negligible overhead. On
restart, the new PID replaces the old one.

### R2: Two-tier liveness model (status and health)

Session liveness is expressed as two distinct concepts with separate helpers. Callers choose which
they need.

**Status** is binary: alive or dead. Determined by checking `/proc/<pid>`. Fast, requires no tmux
access or signal permissions. Has both single-session and batch variants.

**Health** is enumerated. Determined by PID liveness plus a tmux connectivity test
(`tmux has-session`). Single-session only (no batch variant). Used when an operation needs to
actually interact with the tmux server.

| PID alive | tmux connectable | Health  | Meaning                                                 |
| --------- | ---------------- | ------- | ------------------------------------------------------- |
| yes       | yes              | OK      | Normal operation                                        |
| no        | n/a              | STOPPED | Process not running                                     |
| yes       | no               | BROKEN  | Running but admin cannot connect (permission/ACL drift) |
| n/a       | n/a              | UNKNOWN | No PID in database                                      |

BROKEN is a diagnostic edge case that should rarely, if ever, occur in practice. It indicates
infrastructure drift (socket permissions or tmux server-access ACL changes/misconfiguration).

### R3: Command behavior by health

Each session command should behave according to the session's health:

| Command  | OK                             | STOPPED            | BROKEN                         | UNKNOWN (no PID)                         |
| -------- | ------------------------------ | ------------------ | ------------------------------ | ---------------------------------------- |
| list     | Show running                   | Show stopped       | Not detected. PID status only. | Auto-repair, then show status            |
| describe | Show details + health          | Show as stopped    | Show as broken                 | Auto-repair, then show health            |
| stop     | Normal stop (C-c, grace, kill) | Already stopped    | Error, suggest --force         | Auto-repair, then stop normally          |
| restart  | Confirm, restart (-y to skip)  | Normal restart     | Error, suggest --force         | Auto-repair, then restart normally       |
| delete   | Confirm, delete (-y to skip)   | Confirm, delete    | Error, suggest --force         | Auto-repair, then confirm                |
| attach   | Normal attach                  | Error: not running | Error: broken                  | Auto-repair, then attach or error        |
| logs     | Normal capture                 | Error: not running | Error: broken                  | Auto-repair, then capture or error       |

`session list` uses batch status (PID only). All other commands use health (PID + connect test) when
they need to verify liveness.

`--force` is exclusively for BROKEN sessions (PID-based kill escalation). `--yes/-y` skips
confirmation prompts (running session restart, delete). These are orthogonal.

### R4: Batch operations

#### Batch status checking for session list

`session list` must check all sessions on a VM in a single SSH round-trip. The implementation groups
sessions by VM, builds a compound `test -d /proc/<pid>` command for each PID, and runs one SSH call
per VM. VMs are queried in parallel.

For N sessions across M VMs, this reduces status checking from N SSH calls to M parallel SSH calls.

Sessions with no PID in the database are reported as "unknown" without any SSH call.

`--no-status` skips all SSH checks and shows `-` for the status column.

#### Batch stop and restart

`session stop --all` stops all running sessions. `session restart --all-stopped` restarts all
stopped sessions. `session restart --all` restarts everything (prompts if any are running, unless
`--force` is passed). All batch variants accept `--vm` and `--workspace` filters and use the same
batch PID checking infrastructure as `session list`.

BROKEN sessions are warned and skipped unless `--force` is passed. UNKNOWN sessions are
auto-repaired before the batch proceeds.

### R5: Force escalation pattern

`--force` triggers PID-based kill escalation for BROKEN sessions. It is not used for running
sessions (those use confirmation prompts with `--yes/-y`). The escalation path:

1. Send `kill <pid>` (SIGTERM) to the tmux server process via sudo.
2. If the PID is still alive after a grace period, send `kill -9 <pid>` (SIGKILL).
3. If there is a socket file and the server is dead, remove the stale socket.

This path does not require tmux socket access or server-access ACL membership. It works through the
OS process management layer.

Without `--force`, these commands should simply error when the session is BROKEN, suggesting the
operator use `--force` to proceed.

For admin-mode sessions, killing the PID kills the entire admin tmux server (all admin-mode sessions
on that server). The --force path must warn the operator before proceeding.

### R6: Auto-repair for existing sessions

Existing sessions created before this enhancement will not have a PID in the database. Rather than
requiring an explicit repair command, PID recovery happens automatically when any command accesses a
session with a NULL PID:

```shell
tmux [-S <socket>] display-message -p '#{pid}'
```

If the tmux server is running, the PID is stored. If not, the session is marked as stopped
(`PID_STOPPED`). A warning is emitted so the operator knows the repair happened.

For batch commands (`session list`, `stop --all`, `restart --all`), all NULL-PID sessions are
auto-repaired before the batch proceeds. On restart, the new tmux server's PID is captured,
replacing any previous value.

## Non-goals

- **Non-tmux session backends**: health is specific to tmux. That said, the PID status applies to
  any process-based session. Expanding to alternative backends (containers, systemd units) is
  out-of-scope for this SDD but the model is designed to accommodate them in the future by adding
  new backend-specific health checks.
- **Changes to session creation UX**: the `session create` command and template system are
  unchanged. PID capture is an internal implementation detail.
- **Socket permission hardening**: the root cause of BROKEN state (permission drift) is a separate
  concern. This SDD diagnoses and surfaces the problem; it does not fix the underlying permission
  model.
- **PID reuse detection**: PIDs can be reused by the OS. The window for false positives is small
  (original process dies and a new process takes the same PID between checks). This is an acceptable
  risk for a CLI tool that runs interactively.
