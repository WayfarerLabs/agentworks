# Task-to-session rename -- implementation plan

This rename has two parts: (1) mechanical rename of "task" to "session" everywhere, and (2)
switching session names from workspace-scoped to globally unique. The order is chosen to keep the
codebase functional at each phase boundary.

---

## Phase 1: Database migration

- [x] Add migration 17: rename `tasks` table to `sessions`, migrate existing names using `--`
      separator for collision safety and tmux compatibility, add `socket_path TEXT` column
- [x] Add migration 18: rename `task_name` column in `agent_grants` to `session_name`, migrate
      values to match the new session names
- [x] Update `LATEST_VERSION`
- [x] Update all SQL strings in `db.py`: table names, column names
- [x] Rename types: `TaskRow` to `SessionRow`, `TaskStatus` to `SessionStatus`, `TaskMode` to
      `SessionMode`
- [x] Add `socket_path: str | None` to `SessionRow` (NULL for admin-mode sessions that use the
      default tmux server)
- [x] Rename `_to_task()` to `_to_session()`
- [x] Rename all db methods: `insert_task` to `insert_session`, `get_task` to `get_session`,
      `list_tasks` to `list_sessions`, `update_task_status` to `update_session_status`,
      `delete_task` to `delete_session`, `delete_tasks_for_workspace` to
      `delete_sessions_for_workspace`, `count_tasks_on_vm` to `count_sessions_on_vm`
- [x] Add `update_session_socket_path()` method
- [x] Update `AgentGrantRow.task_name` to `AgentGrantRow.session_name`
- [x] Session name is now the primary key (globally unique), workspace_name is a regular column
      (relationship, not part of the key)

---

## Phase 2: Config rename

- [x] Rename `TaskTemplate` to `SessionTemplate`, `TaskConfig` to `SessionConfig`
- [x] Rename config fields: `Config.task` to `Config.session`, `Config.task_templates` to
      `Config.session_templates`
- [x] Rename loader functions: `_load_task_config` to `_load_session_config`, `_load_task_templates`
      to `_load_session_templates`
- [x] Rename key sets: `_TASK_CONFIG_KEYS` to `_SESSION_CONFIG_KEYS`, `_TASK_TEMPLATE_KEYS` to
      `_SESSION_TEMPLATE_KEYS`
- [x] Update `EXPECTED_TOP_LEVEL_KEYS`: `"task"` to `"session"`, `"task_templates"` to
      `"session_templates"`
- [x] Update `sample-config.toml`: rename sections, update comments, rename `{{task_name}}` to
      `{{session_name}}`
- [x] Clean break: no backward compatibility for old config keys

---

## Phase 3: Rename module directory

- [x] Rename `cli/agentworks/tasks/` to `cli/agentworks/sessions/`
- [x] Update all imports across the codebase (cli.py, workspaces/manager.py,
      workspaces/tmuxinator.py, agents/manager.py, vms/initializer.py)

---

## Phase 4: Rename core session modules

All within `cli/agentworks/sessions/` (formerly `tasks/`):

### 4.1 `tmux.py`

- [x] Remove `TASK_SEPARATOR` constant and `derive_session_name()` function -- session names are now
      globally unique and used directly as tmux session names
- [x] Rename `RESTRICTED_CONFIG_PATH` from `tmux-task.conf` to `tmux-session.conf`
- [x] Rename `create_task_session` to `create_session`, `kill_task_session` to `kill_session`
- [x] `create_session` returns the socket path (or None for admin mode) so the caller can persist it
      to the DB
- [x] Simplify `agent_socket_path()` -- takes session name and linux_user only (no workspace)
- [x] Remove `build_socket_paths()` -- callers read socket_path from `SessionRow` instead of
      deriving it
- [x] All functions that operate on an existing session (`kill_session`, `session_exists`,
      `capture_output`, `send_keys`) take an optional `socket_path` parameter read from the DB
      instead of deriving it
- [x] Rename parameters: `task_name` to `session_name` throughout
- [x] Update docstrings and comments
- [x] Pre-create socket check: remove stale sockets, fail on active sockets
- [x] Stale socket cleanup helper (`cleanup_stale_sockets`)

### 4.2 `manager.py`

- [x] Rename all public functions: `create_task` to `create_session`, `stop_task` to `stop_session`,
      `restart_task` to `restart_session`, `attach_task` to `attach_session`, `delete_task` to
      `delete_session`, `describe_task` to `describe_session`, `list_tasks` to `list_sessions`,
      `task_logs` to `session_logs`
- [x] Rename all private helpers
- [x] Update `_KNOWN_TEMPLATE_VARS`: replace `"task_name"` with `"session_name"`
- [x] Simplify all functions: session name is passed directly, no workspace+task compound key
- [x] Remove `--workspace` as required flag on most commands (only needed on `session create`)
- [x] Update all variable names, parameters, type hints
- [x] Update all user-facing strings (typer.echo, error messages)
- [x] `_effective_socket_path()` helper to derive socket path for migrated sessions with NULL
      `socket_path`
- [x] `describe_session` uses `_reconcile_status` instead of inlining reconciliation
- [x] Socket cleanup on delete with warning if server still running
- [x] Mode label shows `agent (<name>)` instead of raw `agent`

### 4.3 `console.py`

- [x] Rename `add_task_to_console` to `add_session_to_console`
- [x] Rename `_add_task_window` to `_add_session_window`
- [x] Rename `_get_running_tasks_for_vm` to `_get_running_sessions_for_vm`
- [x] Simplify: session name used directly, no `derive_session_name()` call needed
- [x] Update parameters: `running_tasks` to `running_sessions`
- [x] Read `socket_path` from `SessionRow` directly
- [x] Update docstrings and comments

### 4.4 `templates.py`

- [x] Rename `ResolvedTaskTemplate` to `ResolvedSessionTemplate`
- [x] Update docstrings

---

## Phase 5: Update CLI layer

- [x] Rename command group: `task_app` to `session_app`, `name="task"` to `name="session"`
- [x] Rename all command functions: `task_create` to `session_create`, etc.
- [x] `session create`: takes `name` as required argument (globally unique), `--workspace` as
      required option
- [x] All other commands (`describe`, `stop`, `restart`, `attach`, `delete`, `logs`): take `name`
      only, no `--workspace` needed
- [x] `session list`: `--workspace` becomes an optional filter
- [x] Update all help text and docstrings
- [x] Update import paths from `agentworks.tasks.*` to `agentworks.sessions.*`
- [x] Update function references: `create_task` to `create_session`, etc.

---

## Phase 6: Update cross-module references

- [x] `workspaces/manager.py`: imports, variables, user-facing strings, SGID on subdirectories
- [x] `workspaces/tmuxinator.py`: imports, simplified to use session name and socket_path directly
- [x] `agents/manager.py`: imports, variables, user-facing strings, stale socket cleanup on reinit
- [x] `vms/initializer.py`: imports, stale socket cleanup on vm reinit
- [x] `vms/manager.py`: imports, variables, user-facing strings
- [x] `vms/backup.py`: task references renamed to session

---

## Phase 7: Completions

- [x] Update `cli/agentworks/completions/spec.py`: rename all `task.*` command entries to
      `session.*`, drop `--workspace` completions from simplified commands
- [x] Rename `"tasks"` data source to `"sessions"`, `"task_templates"` to `"session_templates"`
- [x] Update shell completion generators (bash, zsh, powershell)

---

## Phase 8: Tests

- [x] Update `cli/tests/test_tmuxinator.py` references from task to session
- [x] Update `cli/tests/test_db.py` references: `task_name` to `session_name` in grant tests
- [x] All 181 tests pass

---

## Phase 9: Documentation

- [x] Rewrite `cli/README.md` with four-concept domain model (Operator, VM, Workspace, Agent,
      Session), Key Principles section, Tightly Integrated Tools (SSH, Tailscale, Tmux)
- [x] Rewrite Sessions command table with simplified interface
- [x] Rewrite tmux Architecture section
- [x] Rewrite Session Templates section with `{{session_name}}`
- [x] Add `[proxmox]` to config section reference
- [x] Do NOT modify any locked SDDs

---

## Phase 10: Cleanup

- [x] Final grep for remaining `task` references (only Proxmox API "task" UPIDs remain, correct)
- [x] All 181 tests pass
- [x] Fixed two rename bugs caught by /simplify review (stale variable references)
- [x] Fixed workspace SGID propagation to subdirectories (create, repair, rehome, copy)

---

## Migration notes

### Data migration (phase 1)

Existing tasks are migrated to sessions with globally unique names:

- Task `claude` in workspace `myws` becomes session `myws--claude`
- Task `debug` in workspace `api` becomes session `api--debug`

The `--` separator is used because it is already disallowed in names (by name validation), making
the mapping collision-free. It also matches the existing tmux session naming pattern
(`<workspace>--<task>`), so migrated DB names match live tmux sessions and socket paths exactly. No
legacy name fallback is needed.

### Socket paths

The `socket_path` column in the sessions table is the source of truth for where to find a session's
tmux socket. This decouples naming conventions from socket location.

- Admin-mode sessions: `socket_path` is NULL (uses default tmux server)
- Agent-mode sessions: `socket_path` is set on session create and persisted

New sessions use the session name directly: `<session>.sock`. For example:

- `/run/agentworks/agent-tmux-sockets/agt--alice/myws--claude.sock`

Since the migration uses `--` as the separator, migrated session names match the existing socket
file names exactly. No socket rename or fallback is needed.

The DB migration sets `socket_path` to NULL for all existing sessions (since we cannot know the
linux username from the sessions table alone). `_effective_socket_path()` derives the socket path
from the agent's linux_user as a fallback when `socket_path` is NULL. On next `session restart`, the
socket path will be computed and persisted.

### Socket lifecycle

- **Create**: checks for existing socket, removes stale ones, fails on active ones
- **Delete**: removes socket after confirming server has exited, warns if still running
- **Reinit** (vm and agent): bulk cleanup of stale sockets via `cleanup_stale_sockets()`

### VM-side config file rename

`/opt/agentworks/tmux-task.conf` becomes `/opt/agentworks/tmux-session.conf`. This is deployed on
session create/restart, so it will be updated naturally. The old file can be left in place
(harmless) or cleaned up during reinit.
