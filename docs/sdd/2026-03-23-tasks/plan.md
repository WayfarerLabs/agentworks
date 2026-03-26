# Tasks -- Implementation Plan

## Definition of Done

- `agentworks task create/list/stop/restart/attach/delete/logs` commands work end-to-end.
- `agentworks vm console` creates and attaches to the VM-level console.
- Task tmux sessions are locked down (no splits, no new windows, no prefix key, large scrollback).
- Console auto-populates with running tasks and survives task lifecycle changes.
- Task templates are config-driven with a built-in "default" template (login shell).
- Tasks work with both admin users and agent users.
- Completions are updated for all new commands.
- Sample config is updated with new sections.
- Existing tmuxinator code is preserved but no longer used for new task workflows.

---

## Phase 1: Database and Domain Model

- [x] Add `tasks` table to SQLite schema
  - Columns: name, workspace_name, template, mode (admin/agent), linux_user, status, created_at,
    updated_at
  - Primary key: (name, workspace_name)
  - Foreign key: workspace_name references workspaces
  - mode is "admin" or "agent"; linux_user stores the resolved user for both modes
- [x] Add `TaskRow` dataclass to `db.py`
- [x] Add `TaskStatus` and `TaskMode` enums
- [x] Add DB methods: insert_task, get_task, list_tasks, update_task_status, delete_task,
  delete_tasks_for_workspace
- [x] Add schema migration 8 (create tasks table)
- [x] Add cascade deletes for tasks in delete_vm and delete_workspace

**Done when**: DB operations work and are covered by the existing migration path.

## Phase 2: tmux Task Session Management

- [x] Create `cli/agentworks/tasks/` package
- [x] Implement `tmux.py` module with functions:
  - `generate_restricted_config(history_limit)` -- returns the locked-down tmux config content
  - `deploy_restricted_config(run_command)` -- writes config to `/opt/agentworks/tmux-task.conf`
  - `derive_session_name(workspace_name, task_name)` -- returns `<workspace>--<task>`
  - `create_task_session(workspace_name, task_name, workspace_path, command, linux_user,
    run_command)` -- starts the locked-down tmux session
  - `kill_task_session(workspace_name, task_name, run_command)` -- kills the session
  - `session_exists(workspace_name, task_name, run_command)` -- checks if the tmux session is alive
  - `capture_output(workspace_name, task_name, lines, run_command)` -- captures scrollback buffer
- ~~Write LLD for the restricted tmux config~~ (dropped -- README covers the architecture)

**Done when**: Can create and destroy locked-down tmux sessions on a VM over SSH.

## Phase 3: Task Templates

- [x] Add `[task.config]` and `[task_templates.*]` to config parsing in `config.py`
- [x] Add built-in "default" template (login shell)
- [x] Implement template resolution logic (explicit > default > built-in)
- [x] Update `sample-config.toml` with task config and template examples
- [x] Validate template commands at config load time

**Done when**: Templates resolve correctly from config with built-in fallbacks.

## Phase 4: Task Lifecycle (Manager)

- [x] Implement `cli/agentworks/tasks/manager.py` with functions:
  - `create_task(db, config, name, workspace_name, template_name, agent_name)` -- validates inputs,
    inserts DB row, deploys tmux config if needed, creates tmux session, updates console
  - `stop_task(db, config, name, workspace_name)` -- kills tmux session, updates DB
  - `restart_task(db, config, name, workspace_name)` -- re-creates tmux session from DB state
  - `delete_task(db, config, name, workspace_name)` -- stops if running, deletes DB row
  - `list_tasks(db, workspace_name)` -- lists tasks with reconciled status
  - `attach_task(db, config, name, workspace_name)` -- interactive SSH attach to tmux session
  - `task_logs(db, config, name, workspace_name, lines)` -- captures and prints scrollback
- [x] Implement status reconciliation (check tmux session existence on list/get)

**Done when**: Full task lifecycle works via manager functions.

## Phase 5: Console

- [x] Implement console management in `cli/agentworks/tasks/console.py`:
  - `create_console(running_tasks, run_command)` -- creates the VM console session with one window
    per running task
  - `add_task_to_console(task_name, workspace_name, run_command)` -- adds a window to an existing
    console
  - `attach_console(db, config, vm_name, recreate)` -- interactive SSH attach to the console
  - `console_exists(run_command)` -- checks if console session exists
  - `recreate_console(running_tasks, run_command)` -- kills and rebuilds
- [x] Console session naming: `vm-console` (one per VM, no collision risk)
- [x] Task window naming in console: use `<workspace>--<task>` to match the session name
- [x] Auto-add tasks to console on task create (if console exists)

**Done when**: Console aggregates running tasks and supports full tmux controls.

## Phase 6: CLI Commands

- [x] Add `task` command group to `cli.py`
  - `task create <name> --workspace <ws> [--template <tpl>] [--agent <agent>]`
  - `task list [--workspace <ws>]`
  - `task stop <name> --workspace <ws>`
  - `task restart <name> --workspace <ws>`
  - `task attach <name> --workspace <ws>`
  - `task delete <name> --workspace <ws>`
  - `task logs <name> --workspace <ws> [--lines <n>]`
- [x] Add `vm console <vm-name> [--recreate]` command
- [x] Update completions for all new commands and dynamic arguments
- [x] Update help text and command descriptions

**Done when**: All commands work from the CLI with proper error handling and output.

## Phase 7: Documentation and Cleanup

- [x] Update sample-config.toml with `[task.config]` and `[task_templates.*]` sections
- [x] Update any relevant existing docs
- [x] Spell-check new files with cspell
- [x] Verify completions work for new commands
- [x] Repurpose tmuxinator code for task session management (no longer used for original workflow)

**Done when**: Docs are accurate, config is complete, completions work.
