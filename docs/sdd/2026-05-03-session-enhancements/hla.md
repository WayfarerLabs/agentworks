# Session enhancements -- high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/sessions/`

## Overview

This enhancement adds PID-based liveness checking to sessions. Each session is backed by a process
(currently a tmux server) whose PID is captured at creation and stored in the database. The model is
defined in terms of PIDs, not tmux, so the architecture could accommodate non-tmux session backends
in the future.

Liveness is expressed as two distinct concepts: status (binary, is the process alive?) and health
(enumerated, can we interact with the session?). A batch status mechanism makes `session list` fast
regardless of session count. A repair command recovers PIDs for sessions created before this
enhancement.

## Liveness model

```text
                create (capture PID)
                      |
                      v
     .---- [OK] ----. (PID alive, tmux connectable)
     |              |
     |   process    |  restart
     |   exits      |  (capture new PID)
     |              |
     v              |
   [STOPPED] ------'
   (PID not alive)

   [BROKEN] (PID alive, tmux not connectable -- rare, permission/ACL drift)
     |
     | --force (kill PID)
     v
   [STOPPED]

   [UNKNOWN] (no PID in DB -- pre-enhancement session)
     |
     | session repair
     v
   [OK] or [STOPPED]
```

### Status vs health

| Concept | Determined by | Returns | Batch variant | Used by |
|---------|--------------|---------|---------------|---------|
| Status  | `test -d /proc/<pid>` | alive / dead | Yes (one SSH call per VM) | `session list` |
| Health  | `/proc` check + connectivity test | OK / STOPPED / BROKEN / UNKNOWN | No | `attach`, `describe`, pre-op checks |

Status is process-level: is the PID alive? Checked via `/proc/<pid>` which requires no signal
permissions (the admin can check agent-owned processes without sudo). It is transport-agnostic and
would work for any process-backed session type. Health adds a transport-specific connectivity test
(currently `tmux has-session`). A session that is "alive" by status is either OK or BROKEN by
health. Callers that only need to know whether the process is running use status. Callers that need
to interact with the session use health.

## DB schema changes

### Migration (next after current LATEST_VERSION)

```sql
ALTER TABLE sessions DROP COLUMN status;
ALTER TABLE sessions ADD COLUMN pid INTEGER;
```

The `status` column is dropped; liveness is always determined live via PID checks. The `pid` column
is nullable; existing sessions get NULL.

PID column values:

- `NULL` -- pre-enhancement session, never checked (UNKNOWN health)
- `-1` -- known to be stopped (no process to check)
- `>0` -- known PID (check `/proc/<pid>` for current liveness)

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
```

### DB method changes

```python
def update_session_pid(self, name: str, pid: int | None) -> None:
    """Store or clear the tmux server PID for a session."""
```

The existing `update_session_status` method and `SessionStatus` enum are removed.

## SessionHealth enum

```python
class SessionHealth(Enum):
    OK = "ok"           # PID alive, tmux connectable
    STOPPED = "stopped" # process not running
    BROKEN = "broken"   # PID alive, tmux not connectable
    UNKNOWN = "unknown" # no PID in DB
```

Defined in `sessions/health.py` or in `db.py`.

## Status checking (binary, PID-based)

### Single session

```python
def check_session_status(pid: int, *, target: ExecTarget) -> bool:
    """Check if a PID is alive via /proc. No sudo or signal permissions needed."""
    result = target.run(f"test -d /proc/{pid}", check=False)
    return result.ok
```

### Batch (one SSH call per VM)

```python
def batch_check_status(
    sessions: list[SessionRow],
    *,
    target: ExecTarget,
) -> dict[str, bool]:
    """Check PIDs for multiple sessions in one SSH call.

    Returns {session_name: alive}. Sessions with pid=None are excluded.
    """
```

The compound command:

```bash
test -d /proc/<pid1>; echo "STATUS:<name1>:$?";
test -d /proc/<pid2>; echo "STATUS:<name2>:$?";
...
```

Each line outputs `STATUS:<name>:0` (alive) or `STATUS:<name>:1` (dead). The caller parses the
output and maps results back to sessions.

### Batch flow for session list

```text
session list
  |
  v
load all sessions from DB
  |
  v
group by VM (workspace -> VM lookup)
  |
  for each VM (in parallel, capped at 8):
  |   |
  |   v
  |   partition: has_pid (include) vs no_pid (report as UNKNOWN)
  |   |
  |   v
  |   build compound /proc check command
  |   |
  |   v
  |   single SSH call -> parse results
  |
  v
render table with status column
```

## Health checking (enumerated, PID + connect)

### Single session only

```python
def check_session_health(
    session: SessionRow,
    *,
    target: ExecTarget,
) -> SessionHealth:
    """Full health check: PID liveness + tmux connectivity.

    Pure function -- no DB side effects.
    """
    if session.pid is None:
        return SessionHealth.UNKNOWN
    if session.pid == PID_STOPPED:
        return SessionHealth.STOPPED

    # Step 1: is the process alive?
    alive = check_session_status(session.pid, target=target)
    if not alive:
        return SessionHealth.STOPPED

    # Step 2: can we interact with the session? (transport-specific)
    sock = session.socket_path
    q_session = shlex.quote(session.name)
    cmd = tmux_cmd(f"has-session -t {q_session}", sock) + " 2>/dev/null"
    result = target.run(cmd, check=False)
    if result.ok:
        return SessionHealth.OK

    # PID alive but tmux unreachable
    return SessionHealth.BROKEN
```

No batch variant. Health checks involve tmux connectivity which is only needed for individual
operations, not bulk listing.

## PID capture at session creation

After `tmux new-session`, retrieve the PID:

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

The `create_session` function return type changes:

```python
# Before
def create_session(...) -> str | None:
    """Returns socket_path or None."""

# After
def create_session(...) -> tuple[str | None, int]:
    """Returns (socket_path, tmux_server_pid)."""
```

The manager stores the PID after creation:

```python
sock, pid = create_tmux_session(...)
db.update_session_pid(name, pid)
```

On restart, the new PID replaces the old one.

## Kill escalation path

When --force is used on a BROKEN or unresponsive session:

```text
1. kill <pid>              (SIGTERM via sudo)
2. sleep 2
3. test -d /proc/<pid>     (check if still alive)
4. if alive: kill -9 <pid> (SIGKILL via sudo)
5. if socket exists and server dead: rm <socket>
6. clear PID in DB
```

Implementation:

```python
def force_kill_tmux_server(
    pid: int,
    *,
    target: ExecTarget,
    socket_path: str | None = None,
) -> bool:
    """Kill a tmux server by PID with SIGTERM -> SIGKILL escalation.

    Cleans up socket file if present. Returns True if the process is dead.
    """
```

**Admin-mode caveat**: multiple admin-mode sessions share the same PID (the admin's default tmux
server). Killing this PID kills all admin-mode sessions. The --force path must warn about this.

## Repair mechanism

### Single session

```text
session repair <name>
  |
  v
load session from DB
  |
  if session.pid is not None: skip
  |
  v
derive socket_path (agent) or default server (admin)
  |
  v
SSH: tmux [-S <socket>] display-message -p '#{pid}'
  |
  if success: store PID, report recovered
  if failure: report not running
```

### Batch repair

```text
session repair --all [--vm <vm>]
  |
  v
load sessions with pid=NULL (optionally filtered)
  |
  v
group by VM
  |
  for each VM (one SSH call):
  |   build compound command:
  |     tmux [-S <sock1>] display-message -p '#{pid}' 2>/dev/null || echo STOPPED:<name1>;
  |     tmux [-S <sock2>] display-message -p '#{pid}' 2>/dev/null || echo STOPPED:<name2>;
  |     ...
  |   |
  |   v
  |   parse results, update DB
```

For admin-mode sessions (no socket), the repair queries the admin's default tmux server. Multiple
admin-mode sessions will get the same PID, which is correct.

## Component changes

### sessions/tmux.py

| Change | Detail |
|--------|--------|
| `create_session` return type | `str \| None` -> `tuple[str \| None, int]` (socket, pid) |
| New: PID retrieval after create | `tmux display-message -p '#{pid}'` |
| New: `force_kill_tmux_server` | PID-based kill with SIGTERM/SIGKILL escalation |
| New: `get_tmux_server_pid` | Retrieve PID from running server (for repair) |

### sessions/manager.py

| Change | Detail |
|--------|--------|
| New: `check_session_status` | Single PID check (binary) |
| New: `batch_check_status` | Batch PID check (one SSH call per VM) |
| New: `check_session_health` | PID + tmux connectivity (enumerated) |
| `create_session` | Store PID after creation |
| `restart_session` | Prompts for running (-y to skip); --force for BROKEN; store new PID |
| `stop_session` | Health check; --force for BROKEN |
| `delete_session` | Health check; prompts for running/unknown (-y to skip); --force for BROKEN |
| `list_sessions` | Use batch status check, parallel across VMs |
| `describe_session` | Show health, suggest repair for BROKEN/UNKNOWN |
| `attach_session` | Use health check, clear error for BROKEN |
| `session_logs` | Use health check, clear error for BROKEN |
| New: `stop_all_sessions` | Batch stop with --vm/--workspace filters |
| New: `repair_session` | Recover PID for single session |
| New: `repair_all_sessions` | Batch PID recovery |
| Removed: `restart-all` subcommand | Replaced by `restart --all-stopped` / `restart --all` |

### db.py

| Change | Detail |
|--------|--------|
| Migration | Drop `status` column, add `pid` (INTEGER) column |
| `SessionRow` | Remove `status: str`, add `pid: int \| None` |
| Remove: `update_session_status` | No longer needed |
| Remove: `SessionStatus` enum | No longer needed |
| New: `update_session_pid` | Store/clear PID |
| New: `PID_STOPPED = -1` | Sentinel: session is known to be stopped |
| New or moved: `SessionHealth` | OK / STOPPED / BROKEN / UNKNOWN enum |

### cli.py

| Change | Detail |
|--------|--------|
| `session stop` | Optional name or `--all` with `--vm`/`--workspace` filters |
| `session restart` | Optional name, `--all-stopped`, or `--all` with filters |
| Removed: `session restart-all` | Replaced by `restart --all-stopped` / `restart --all` |
| New: `session repair` | `session repair <name>` and `session repair --all` |
| `session list` | Status column from batch PID check |
| `session describe` | Health display, repair suggestions |

## Design decisions

### No cached status

Liveness is always determined live. The database does not store a running/stopped status column.
This eliminates stale-state problems and the need for cache invalidation logic. `session list` uses
batch PID checks (one SSH call per VM) so the performance cost is negligible.

### PID column semantics

The PID column is nullable with a sentinel value:

- `NULL` -- pre-enhancement session, never checked (UNKNOWN health, suggest repair)
- `-1` (`PID_STOPPED`) -- known to be stopped (no process to check, restartable)
- `>0` -- known PID (check `/proc/<pid>` for current liveness)

NULL and -1 are distinct: NULL means "we have no information" (repair needed), while -1 means "we
checked and the session is not running" (normal stopped state). Repair sets -1 when it finds a
session is not running, breaking the dead-end loop where repair reports "not running" but commands
still say "run repair."

### Admin-mode PID caveat

Multiple admin-mode sessions share the same tmux server PID. Killing this PID kills all admin-mode
sessions, not just one. The --force path warns about this. This is inherent to admin-mode's
shared-server model and is not introduced by this enhancement.

### No automatic repair on list

`session list` does not auto-repair missing PIDs because: (a) repair requires tmux access, which may
fail for the same reasons PIDs are missing; (b) list should be fast and side-effect-free; (c)
explicit `session repair` gives the operator visibility into what changed.

### Status is process-universal, health is transport-specific

Status (PID alive?) applies to any process-backed session, regardless of whether it uses tmux,
systemd, containers, or something else. Health adds a transport-specific connectivity test that
would need a new implementation for each backend. This separation means adding a non-tmux session
backend only requires implementing the health check, not rethinking status.

### Interaction with socket infrastructure

This enhancement does not change the socket layout, permissions, group model, or server-access ACL
from the agent-tmux-sockets SDD (2026-04-10). It adds a diagnostic layer on top: PID checking
provides reliable liveness information regardless of socket accessibility. The BROKEN state
surfaces what was previously a silent failure.
