# Task-to-session rename -- functional requirements

## Background

The concept currently called "task" in agentworks is a persistent, interactive tmux session that
runs a command (typically a Claude instance) in a workspace. Users attach to, detach from, and
reattach to these sessions. The term "task" implies a discrete unit of work that completes, which
does not match the actual interaction model. "Session" is a better fit and aligns with the
underlying tmux mental model.

Additionally, task names are currently scoped to a workspace, requiring a compound key
(`<workspace>--<task>`) for the underlying tmux session name. This adds complexity to the CLI
(every command needs `--workspace`) and the code (compound key derivation everywhere). Since
sessions are the primary interactive surface, they should have globally unique names.

## Requirements

### R1: Complete rename of "task" to "session"

Every user-visible reference to "task" must become "session":

- CLI command group: `agentworks task` becomes `agentworks session`
- All subcommands: `task create`, `task attach`, etc. become
  `session create`, `session attach`, etc.
- CLI help text and error messages
- Config sections: `[task.config]`, `[task_templates.*]` become `[session.config]`,
  `[session_templates.*]`
- Template variables: `{{task_name}}` becomes `{{session_name}}`
- Sample config file
- User-facing documentation (guides, current/unlocked SDDs)

### R2: Complete rename in code internals

All internal references must also be renamed for consistency:

- Python module: `cli/agentworks/tasks/` becomes `cli/agentworks/sessions/`
- All class names: `TaskRow`, `TaskStatus`, `TaskMode`, `TaskTemplate`, `TaskConfig`, etc.
- All function names: `create_task()`, `list_tasks()`, `stop_task()`, etc.
- All variable/parameter names: `task_name`, `running_tasks`, etc.
- Database table and column names (via migration)
- VM-side file paths: `/opt/agentworks/tmux-task.conf` becomes `/opt/agentworks/tmux-session.conf`
- Completion spec entries
- All test files and test content

### R3: Globally unique session names

Session names are globally unique across the entire agentworks instance, not scoped to a workspace.
This simplifies the CLI (no `--workspace` needed to identify a session), the tmux integration (the
session name IS the tmux session name), and the code (no compound key derivation).

- The session name is the primary key in the database.
- The session name is used directly as the tmux session name.
- The session name is used directly in socket paths.
- The workspace is a relationship, not part of the session identity (same model as
  session-to-agent).
- The `--workspace` flag on session commands becomes optional context for create, not a required
  identifier for all operations.

### R4: Database migration

A new migration:

1. Renames the `tasks` table to `sessions`.
2. Renames `task_name` in `agent_grants` to `session_name`.
3. Migrates existing task data: each task named `<task>` in workspace `<workspace>` becomes a
   session named `<workspace>--<task>` (double dash, matching the existing tmux session and socket
   naming convention). The `--` separator is collision-free because it is disallowed in names.
4. Adds a `socket_path` column to the sessions table. This is the persisted path to the tmux
   socket for agent-mode sessions (NULL for admin-mode sessions that use the default tmux server).
   Persisting the socket path decouples naming conventions from socket location, making future
   changes safe for existing sessions.

### R5: Clean break on config

No backward compatibility for old config keys. `[task.config]` and `[task_templates.*]` are
replaced by `[session.config]` and `[session_templates.*]`. Users must update their config files.
Similarly, `{{task_name}}` in templates is replaced by `{{session_name}}` with no alias.

### R6: Old SDDs are preserved

Locked SDD artifacts (e.g., `docs/sdd/2026-03-23-tasks/`) are historical records and must not be
modified. The rename applies only to current/active documentation and code.
