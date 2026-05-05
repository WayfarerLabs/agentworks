# Session enhancements -- high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/sessions/`

## Overview

This enhancement adds a unified status model for sessions. Status is determined by a
transport-specific connectivity test (`tmux has-session`) combined with a process-level check (boot
ID + existence of `/proc/<pid>`). The PID and VM boot ID are captured at creation and stored in the
database, but they are internal implementation details -- callers only see the status
(OK/STOPPED/BROKEN/UNKNOWN).

A batch status mechanism makes `session list` fast regardless of session count. Sessions created
before this enhancement have their PID and boot ID auto-recovered on first access.

## Status model

```text
                create (capture PID + boot ID)
                      |
                      v
     .---- [OK] ----. (has-session succeeds)
     |              |
     |   session    |  restart
     |   ends       |  (capture new PID + boot ID)
     |              |
     v              |
   [STOPPED] ------'
   (has-session fails, PID dead or stale boot)

   [BROKEN] (has-session fails, PID alive on same boot -- rare, permission/ACL drift)
     |
     | --force (kill PID)
     v
   [STOPPED]

   [UNKNOWN] (no PID in DB -- pre-enhancement session)
     |
     | auto-repair on access (has-session check)
     v
   [OK] or [STOPPED]
```

### Status values

```python
class SessionStatus(Enum):
    OK = "ok"           # has-session succeeds -- session is alive and accessible
    STOPPED = "stopped" # has-session fails, process not running (or stale boot)
    BROKEN = "broken"   # has-session fails, process IS running (permission drift)
    UNKNOWN = "unknown" # no PID in DB (pre-enhancement, auto-repaired on access)
```

### Status check algorithm

The check is dispatched by socket model. Future models (e.g. admin sessions on dedicated sockets)
would add a new branch here.

```python
def check_session_status(session, *, target) -> SessionStatus:
    if session.pid is None:
        return SessionStatus.UNKNOWN
    if session.pid == PID_STOPPED:
        return SessionStatus.STOPPED

    if session.mode == "agent" and session.socket_path is not None:
        return _check_dedicated_agent_session(session, target=target)
    if session.mode == "admin" and session.socket_path is None:
        return _check_shared_admin_session(session, target=target)
    raise RuntimeError(
        f"unexpected session config: mode={session.mode}, socket_path={session.socket_path}"
    )


def _check_dedicated_agent_session(session, *, target) -> SessionStatus:
    """Agent sessions with their own tmux server and socket."""
    if tmux_has_session(session.name, socket_path=session.socket_path, target=target):
        return SessionStatus.OK
    # has-session failed -- STOPPED or BROKEN?
    if session.boot_id != current_boot_id(target):
        return SessionStatus.STOPPED  # stale boot, PID is meaningless
    if not pid_alive(session.pid, target=target):
        return SessionStatus.STOPPED  # process is dead
    return SessionStatus.BROKEN  # process alive, socket unreachable


def _check_shared_admin_session(session, *, target) -> SessionStatus:
    """Admin sessions on the default tmux server. BROKEN does not apply."""
    if tmux_has_session(session.name, target=target):
        return SessionStatus.OK
    return SessionStatus.STOPPED
```

Callers just receive the status enum. The socket-model dispatch is an internal implementation
detail.

### Batch status (one SSH call per VM)

All sessions on a VM are checked in a single compound SSH command. Agent sessions that fail
has-session get an inline boot ID + PID follow-up. Admin sessions never need follow-up.

```bash
# Agent session: has-session with inline STOPPED/BROKEN follow-up
tmux -S <sock1> has-session -t name1 2>/dev/null; \
  if [ $? -ne 0 ]; then \
    BOOT=$(cat /proc/sys/kernel/random/boot_id); \
    test -d /proc/<pid1>; \
    echo "S:name1:1:$BOOT:$?"; \
  else \
    echo "S:name1:0"; \
  fi;

# Admin session: has-session only (BROKEN does not apply)
tmux has-session -t admin1 2>/dev/null; echo "S:admin1:$?";
```

Output formats:

- `S:name:0` -- has-session succeeded, OK
- `S:name:1` -- admin session failed has-session, STOPPED
- `S:name:1:<boot_id>:<pid_exit>` -- agent session failed has-session; caller compares boot_id
  against stored value and checks pid_exit to determine STOPPED vs BROKEN

The boot ID read (`cat /proc/.../boot_id`) is the same value for the whole VM. It could be read once
at the top of the compound command as an optimization.

For N sessions across M VMs: M parallel SSH calls (one per VM).

### Batch flow for session list

```text
session list
  |
  v
load all sessions from DB
  |
  v
auto-repair NULL-PID sessions (has-session + PID recovery)
  |
  v
group by VM (workspace -> VM lookup)
  |
  for each VM (in parallel, capped at 8):
  |   |
  |   v
  |   build compound command (has-session + inline boot_id/PID for agent failures)
  |   |
  |   v
  |   single SSH call -> parse results -> OK / STOPPED / BROKEN per session
  |
  v
render table with status column
```

## DB schema changes

### Migration (next after current LATEST_VERSION)

```sql
ALTER TABLE sessions DROP COLUMN status;
ALTER TABLE sessions ADD COLUMN pid INTEGER;
ALTER TABLE sessions ADD COLUMN boot_id TEXT;
```

The `status` column is dropped; status is always determined live. The `pid` and `boot_id` columns
are nullable; existing sessions get NULL for both.

PID column values:

- `NULL` -- pre-enhancement session, never checked (UNKNOWN status, triggers auto-repair)
- `-1` (`PID_STOPPED`) -- known to be stopped (no process to check, restartable)
- `>0` -- known PID (used for BROKEN detection and force-kill)

### SessionRow

```python
@dataclass
class SessionRow:
    name: str
    workspace_name: str
    template: str
    mode: str
    created_at: str
    updated_at: str
    agent_name: str | None = None
    created_workspace: bool = False
    socket_path: str | None = None
    pid: int | None = None              # new
    boot_id: str | None = None          # new
```

### DB method changes

```python
def update_session_pid(self, name: str, pid: int | None, boot_id: str | None = None) -> None:
    """Store or clear the PID and boot ID for a session."""
```

The existing `update_session_status` method and `SessionStatus` enum are removed (the old
running/stopped enum). The new `SessionStatus` enum (OK/STOPPED/BROKEN/UNKNOWN) is computed live,
not stored.

## PID capture at session creation

After `tmux new-session`, retrieve the PID and boot ID:

```text
create_session flow (agent mode):
  1. sudo -u <agent> tmux -S <socket> new-session ...   (existing)
  2. chmod g+rwx <socket>                               (existing)
  3. server-access -a ...                                (existing)
  4. tmux -S <socket> display-message -p '#{pid}'        (new)
  5. return (socket_path, pid)                           (changed)

create_session flow (admin mode):
  1. tmux new-session -d -s <name> ...                   (existing)
  2. tmux display-message -p '#{pid}'                    (new)
  3. return (None, pid)                                  (changed)
```

The manager reads boot ID separately via `cat /proc/sys/kernel/random/boot_id` and stores both PID
and boot ID together via `db.update_session_pid(name, pid, boot_id=boot_id)`.

On restart, the new PID and current boot ID replace the old ones.

## Kill escalation path

When --force is used on a BROKEN session:

```text
1. kill <pid>              (SIGTERM via sudo)
2. sleep 2
3. test -d /proc/<pid>     (check if still alive)
4. if alive: kill -9 <pid> (SIGKILL via sudo)
5. if socket exists and server dead: rm <socket>
6. clear PID in DB
```

**Admin-mode note**: admin sessions cannot be BROKEN (the admin owns the default server, no socket
permissions to drift). Since `--force` is exclusively for BROKEN, it is never offered for admin
sessions. The shared-server PID kill concern does not arise.

## Auto-repair mechanism

When any command accesses a session with `pid=NULL`, recovery happens automatically:

1. `tmux has-session` to check if the session is alive.
2. If alive: recover PID via `tmux display-message`, store with current boot ID.
3. If not alive (admin session, or agent session with no socket): mark PID_STOPPED.
4. If not alive (agent session with socket file present): probe with sudo
   (`sudo tmux -S <socket> list-sessions`) to distinguish stale socket from live-but-unreachable
   server.
   - Probe fails: stale socket, mark PID_STOPPED.
   - Probe succeeds: live server, unreachable. Leave as NULL (UNKNOWN), warn about permissions.

For batch commands (list, stop --all, restart --all), all NULL-PID sessions are auto-repaired before
the batch status check.

## Component changes

### sessions/tmux.py

| Change                        | Detail                                                   |
| ----------------------------- | -------------------------------------------------------- |
| `create_session` return type  | `str \| None` -> `tuple[str \| None, int]` (socket, pid) |
| New: PID + boot ID retrieval  | `tmux display-message` + `cat /proc/.../boot_id`         |
| New: `force_kill_tmux_server` | PID-based kill with SIGTERM/SIGKILL escalation           |
| New: `get_tmux_server_pid`    | Retrieve PID from running server (for auto-repair)       |

### sessions/manager.py

| Change                          | Detail                                                                     |
| ------------------------------- | -------------------------------------------------------------------------- |
| New: `check_session_status`     | has-session + PID/boot_id context -> SessionStatus                         |
| New: `batch_check_status`       | Compound has-session, one SSH call per VM                                  |
| New: `batch_check_all_sessions` | Groups by VM, parallel across VMs                                          |
| `create_session`                | Store PID + boot ID after creation                                         |
| `restart_session`               | Prompts for running (-y to skip); --force for BROKEN; store new PID        |
| `stop_session`                  | Status check; --force for BROKEN                                           |
| `delete_session`                | Status check; prompts for running/unknown (-y to skip); --force for BROKEN |
| `list_sessions`                 | Batch status check, parallel across VMs                                    |
| `describe_session`              | Auto-repair, then show status                                              |
| `attach_session`                | Auto-repair, then status check                                             |
| `session_logs`                  | Auto-repair, then status check                                             |
| New: `stop_all_sessions`        | Batch stop with --vm/--workspace filters                                   |
| New: `_ensure_pid`              | Auto-repair single session with NULL PID                                   |
| New: `ensure_pids_batch`        | Auto-repair all NULL-PID sessions (batch commands)                         |
| New: `filter_sessions`          | Load sessions with optional workspace/VM filters                           |

### db.py

| Change                           | Detail                                                                  |
| -------------------------------- | ----------------------------------------------------------------------- |
| Migration                        | Drop `status` column, add `pid` (INTEGER) and `boot_id` (TEXT) columns  |
| `SessionRow`                     | Remove `status: str`, add `pid: int \| None` and `boot_id: str \| None` |
| Remove: old `SessionStatus` enum | The running/stopped enum is removed                                     |
| New: `SessionStatus` enum        | OK / STOPPED / BROKEN / UNKNOWN (computed live, not stored)             |
| New: `PID_STOPPED = -1`          | Sentinel: session is known to be stopped                                |
| New: `update_session_pid`        | Store/clear PID and boot ID                                             |

### cli.py

| Change             | Detail                                                     |
| ------------------ | ---------------------------------------------------------- |
| `session stop`     | Optional name or `--all` with `--vm`/`--workspace` filters |
| `session restart`  | Optional name, `--all-stopped`, or `--all` with filters    |
| `session list`     | Auto-repair, then batch status check                       |
| `session describe` | Auto-repair, then status display                           |

## Design decisions

### No cached status

Status is always determined live. The database does not store a running/stopped status column. This
eliminates stale-state problems and the need for cache invalidation logic. Batch has-session checks
(one SSH call per VM, parallel) make the performance cost negligible.

### Status is the single concept

There is no separate "health" concept. Status (OK/STOPPED/BROKEN/UNKNOWN) is the one answer all
callers use. The PID and boot ID are internal details of the status calculation, never exposed as a
standalone liveness indicator.

This simplifies the model: every command asks "what is the status?" and gets a definitive answer.
The implementation of the status check may vary by backend (tmux today, something else tomorrow),
but the enum and the command behavior table are the same.

### PID column semantics

The PID column is nullable with a sentinel value:

- `NULL` -- pre-enhancement session, never checked (triggers auto-repair)
- `-1` (`PID_STOPPED`) -- known to be stopped (no process to check, restartable)
- `>0` -- known PID (used for BROKEN detection and force-kill when has-session fails)

### Boot ID prevents stale PID confusion

After a VM reboot, all tmux servers die but the DB retains their old PIDs. A new process could reuse
the PID, and `/proc/<pid>` would exist. The boot ID (from `/proc/sys/kernel/random/boot_id`) changes
on every boot. If the stored boot ID doesn't match the current one, the PID is stale and the session
is STOPPED regardless of `/proc/<pid>`.

When a session is stopped, the boot ID is left as-is (the last boot the session ran in). This is the
most useful value: on the next status check, a stale boot ID immediately resolves to STOPPED without
needing a PID check. Clearing it would lose information for no benefit.

### Admin-mode sessions

Admin-mode sessions share the admin's default tmux server. The PID identifies the server, not the
session. `has-session` is the definitive check for whether a specific admin session exists. Since
the admin owns the default server (no socket permissions to drift), admin sessions can only be OK or
STOPPED -- BROKEN does not apply. This means `--force` (which is exclusively for BROKEN) is never
offered for admin sessions, and the shared-server PID kill concern does not arise.

### Transparent auto-repair

Rather than requiring an explicit `session repair` command, status recovery happens automatically
when any command accesses a session with a NULL PID. This eliminates dead-end error loops and makes
the migration from pre-enhancement sessions seamless.

### Interaction with socket infrastructure

This enhancement does not change the socket layout, permissions, group model, or server-access ACL
from the agent-tmux-sockets SDD (2026-04-10). Session commands (has-session, kill-session,
send-keys, etc.) do NOT use sudo -- they go through normal socket group permissions. If permissions
drift, the session appears BROKEN and the operator must fix permissions or use `--force` (PID kill).
