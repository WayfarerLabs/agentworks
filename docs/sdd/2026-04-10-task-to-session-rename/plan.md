# Task-to-session rename -- implementation plan

This rename has two parts: (1) mechanical rename of "task" to "session" everywhere, and
(2) switching session names from workspace-scoped to globally unique. The order is chosen to keep
the codebase functional at each phase boundary.

---

## Phase 1: Database migration

- [ ] Add migration 17: rename `tasks` table to `sessions`, migrate existing names from `<name>`
  to `<workspace>-<name>` (single dash) for global uniqueness, add `socket_path TEXT` column
- [ ] Add migration 18: rename `task_name` column in `agent_grants` to `session_name`, migrate
  values to match the new session names
- [ ] Update `LATEST_VERSION`
- [ ] Update all SQL strings in `db.py`: table names, column names
- [ ] Rename types: `TaskRow` to `SessionRow`, `TaskStatus` to `SessionStatus`, `TaskMode` to
  `SessionMode`
- [ ] Add `socket_path: str | None` to `SessionRow` (NULL for admin-mode sessions that use the
  default tmux server)
- [ ] Rename `_to_task()` to `_to_session()`
- [ ] Rename all db methods: `insert_task` to `insert_session`, `get_task` to `get_session`,
  `list_tasks` to `list_sessions`, `update_task_status` to `update_session_status`,
  `delete_task` to `delete_session`, `delete_tasks_for_workspace` to
  `delete_sessions_for_workspace`, `count_tasks_on_vm` to `count_sessions_on_vm`
- [ ] Add `update_session_socket_path()` method
- [ ] Update `AgentGrantRow.task_name` to `AgentGrantRow.session_name`
- [ ] Session name is now the primary key (globally unique), workspace_name is a regular column
  (relationship, not part of the key)

---

## Phase 2: Config rename

- [ ] Rename `TaskTemplate` to `SessionTemplate`, `TaskConfig` to `SessionConfig`
- [ ] Rename config fields: `Config.task` to `Config.session`, `Config.task_templates` to
  `Config.session_templates`
- [ ] Rename loader functions: `_load_task_config` to `_load_session_config`,
  `_load_task_templates` to `_load_session_templates`
- [ ] Rename key sets: `_TASK_CONFIG_KEYS` to `_SESSION_CONFIG_KEYS`,
  `_TASK_TEMPLATE_KEYS` to `_SESSION_TEMPLATE_KEYS`
- [ ] Update `EXPECTED_TOP_LEVEL_KEYS`: `"task"` to `"session"`, `"task_templates"` to
  `"session_templates"`
- [ ] Update `sample-config.toml`: rename sections, update comments, rename `{{task_name}}` to
  `{{session_name}}`
- [ ] Clean break: no backward compatibility for old config keys

---

## Phase 3: Rename module directory

- [ ] Rename `cli/agentworks/tasks/` to `cli/agentworks/sessions/`
- [ ] Update all imports across the codebase (cli.py, workspaces/manager.py,
  workspaces/tmuxinator.py, agents/manager.py, vms/initializer.py)

---

## Phase 4: Rename core session modules

All within `cli/agentworks/sessions/` (formerly `tasks/`):

### 4.1 `tmux.py`

- [ ] Remove `TASK_SEPARATOR` constant and `derive_session_name()` function -- session names are
  now globally unique and used directly as tmux session names
- [ ] Rename `RESTRICTED_CONFIG_PATH` from `tmux-task.conf` to `tmux-session.conf`
- [ ] Rename `create_task_session` to `create_session`, `kill_task_session` to `kill_session`
- [ ] `create_session` returns the socket path (or None for admin mode) so the caller can persist
  it to the DB
- [ ] Simplify `agent_socket_path()` -- takes session name and linux_user only (no workspace)
- [ ] Remove `build_socket_paths()` -- callers read socket_path from `SessionRow` instead of
  deriving it
- [ ] All functions that operate on an existing session (`kill_session`, `session_exists`,
  `capture_output`, `send_keys`) take an optional `socket_path` parameter read from the DB
  instead of deriving it
- [ ] Rename parameters: `task_name` to `session_name` throughout
- [ ] Update docstrings and comments

### 4.2 `manager.py`

- [ ] Rename all public functions: `create_task` to `create_session`, `stop_task` to
  `stop_session`, `restart_task` to `restart_session`, `attach_task` to `attach_session`,
  `delete_task` to `delete_session`, `describe_task` to `describe_session`, `list_tasks` to
  `list_sessions`, `task_logs` to `session_logs`
- [ ] Rename all private helpers: `_resolve_task_linux_user` to `_resolve_session_linux_user`,
  `_socket_path_for_task` to `_socket_path_for_session`, `_kill_task_any_server` to
  `_kill_session_any_server`, `_require_task` to `_require_session`,
  `_resolve_task_template` to `_resolve_session_template`,
  `_build_task_command` to `_build_session_command`, `_reconcile_task_status` to
  `_reconcile_session_status`
- [ ] Update `_KNOWN_TEMPLATE_VARS`: replace `"task_name"` with `"session_name"`
- [ ] Simplify all functions: session name is passed directly, no workspace+task compound key
- [ ] Remove `--workspace` as required flag on most commands (only needed on `session create`)
- [ ] Update all variable names, parameters, type hints
- [ ] Update all user-facing strings (typer.echo, error messages)

### 4.3 `console.py`

- [ ] Rename `add_task_to_console` to `add_session_to_console`
- [ ] Rename `_add_task_window` to `_add_session_window`
- [ ] Rename `_get_running_tasks_for_vm` to `_get_running_sessions_for_vm`
- [ ] Simplify: session name used directly, no `derive_session_name()` call needed
- [ ] Update parameters: `running_tasks` to `running_sessions`
- [ ] Update docstrings and comments

### 4.4 `templates.py`

- [ ] Rename `ResolvedTaskTemplate` to `ResolvedSessionTemplate`
- [ ] Update docstrings

---

## Phase 5: Update CLI layer

- [ ] Rename command group: `task_app` to `session_app`, `name="task"` to `name="session"`
- [ ] Rename all command functions: `task_create` to `session_create`, etc.
- [ ] `session create`: takes `name` as required argument (globally unique), `--workspace` as
  required option
- [ ] All other commands (`describe`, `stop`, `restart`, `attach`, `delete`, `logs`): take `name`
  only, no `--workspace` needed
- [ ] `session list`: `--workspace` becomes an optional filter
- [ ] Update all help text and docstrings
- [ ] Update import paths from `agentworks.tasks.*` to `agentworks.sessions.*`
- [ ] Update function references: `create_task` to `create_session`, etc.

---

## Phase 6: Update cross-module references

### 6.1 `workspaces/manager.py`

- [ ] Update imports from `agentworks.sessions.*`
- [ ] Rename variables: `tasks` to `sessions`, `running_tasks` to `running_sessions`, etc.
- [ ] Update user-facing strings

### 6.2 `workspaces/tmuxinator.py`

- [ ] Update imports from `agentworks.sessions.*`
- [ ] Simplify: session name used directly as tmux session name and in socket paths
- [ ] Rename parameters: `tasks` to `sessions`
- [ ] Update comments

### 6.3 `agents/manager.py`

- [ ] Update imports from `agentworks.sessions.*`
- [ ] Rename variables and user-facing strings

### 6.4 `vms/initializer.py`

- [ ] Update imports from `agentworks.sessions.*`

---

## Phase 7: Completions

- [ ] Update `cli/agentworks/completions/spec.py`: rename all `task.*` command entries to
  `session.*`
- [ ] Rename `"tasks"` data source to `"sessions"`, `"task_templates"` to `"session_templates"`
- [ ] Simplify: session commands that no longer need `--workspace` can drop those completion
  entries
- [ ] Update data source resolution in completions engine if it maps source names to CLI commands

---

## Phase 8: Tests

- [ ] Update `cli/tests/test_tmuxinator.py` references from task to session
- [ ] Update `cli/tests/test_db.py` references: `task_name` to `session_name` in grant tests
- [ ] Update `cli/tests/test_name_validation.py` if it references task
- [ ] Update any other test files with task references
- [ ] Run full test suite, verify all pass

---

## Phase 9: Documentation

### 9.1 `cli/README.md` -- rewrite domain model and commands

The README is the primary document that tells the agentworks domain model story. It needs a full
pass, not just find-and-replace. The post-rename domain model is crisp:

- **VM** -- the environment (capability ceiling)
- **Workspace** -- the project (scope: repos, config, rules)
- **Agent** -- the actor (security identity, Linux user)
- **Session** -- the interaction (persistent tmux session, globally unique name, runs in a
  workspace, optionally as an agent)

Specific updates:

- [ ] Rewrite "Core Concepts" section to include sessions as a first-class concept with the
  above framing
- [ ] Update "Ephemerality" paragraph: sessions replace tasks
- [ ] Update "Templates" paragraph: session templates replace task templates
- [ ] Rewrite "Tasks" command table as "Sessions" with simplified interface (no `--workspace`
  on most commands)
- [ ] Rewrite "tmux Architecture" section: session names are globally unique, no compound key,
  socket path persisted in DB
- [ ] Rewrite "Task Templates" section as "Session Templates" with `{{session_name}}`
- [ ] Update all cross-references: `workspace describe` shows sessions not tasks,
  `workspace delete` checks for sessions, `agent delete` checks for sessions, etc.
- [ ] Update "Shell Completion" section: sessions replace tasks in dynamic lookups
- [ ] Update config section references: `[session.config]`, `[session_templates.*]`

### 9.2 Other living docs

- [ ] Update any current guides in `docs/guides/` that reference tasks
- [ ] Do NOT modify any locked SDDs (those with a `locked.md` file, including
  `2026-03-23-tasks/`, `2026-04-10-agent-tmux-sockets/`, etc.)

---

## Phase 10: Cleanup

- [ ] Update `.cspell.json` if needed
- [ ] Final grep for any remaining `task` references (excluding old SDDs, git history)
- [ ] Verify CLI help output reads correctly
- [ ] Verify completions work

---

## Migration notes

### Data migration (phase 1)

Existing tasks are migrated to sessions with globally unique names:

- Task `claude` in workspace `myws` becomes session `myws--claude`
- Task `debug` in workspace `api` becomes session `api--debug`

The `--` separator is used because it is already disallowed in names (by name validation), making
the mapping collision-free. It also matches the existing tmux session naming pattern
(`<workspace>--<task>`), so migrated DB names match live tmux sessions and socket paths exactly.
No legacy name fallback is needed.

### Socket paths

The `socket_path` column in the sessions table is the source of truth for where to find a
session's tmux socket. This decouples naming conventions from socket location.

- Admin-mode sessions: `socket_path` is NULL (uses default tmux server)
- Agent-mode sessions: `socket_path` is set on session create and persisted

New sessions use the session name directly: `<session>.sock`. For example:

- `/run/agentworks/agent-tmux-sockets/agt--alice/myws--claude.sock`

Since the migration uses `--` as the separator, migrated session names match the existing socket
file names exactly. No socket rename or fallback is needed.

The DB migration sets `socket_path` to NULL for all existing sessions (since we cannot know the
linux username from the sessions table alone). On next `session restart`, the socket path will be
computed and persisted. `/run` is tmpfs (cleared on reboot) and sockets are created fresh by tmux
on session start, so this is naturally self-healing.

### VM-side config file rename

`/opt/agentworks/tmux-task.conf` becomes `/opt/agentworks/tmux-session.conf`. This is deployed on
session create/restart, so it will be updated naturally. The old file can be left in place (harmless)
or cleaned up during reinit.
