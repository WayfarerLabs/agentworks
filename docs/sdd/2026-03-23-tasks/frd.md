# Tasks -- Functional Requirements

## Problem Statement

Agentworks currently models VMs, workspaces, and agents as independent concepts that operators
compose manually. A workspace gets a tmuxinator session with one window per agent, but there is no
first-class abstraction for "a work stream running in a workspace." This means:

- There is no clean way to run a tool (like Claude) in a workspace without also setting up an agent.
- There is no lifecycle management for individual running processes -- starting, stopping, and
  monitoring are ad hoc.
- The tmuxinator-based tmux sessions mix operator navigation (switching between windows) with
  process execution (running the agent's command), making it hard to lock down agent sessions while
  keeping the operator experience flexible.

The task abstraction solves this by giving a name and lifecycle to "a work stream on one VM, rooted
in one workspace, run as one user (agent or admin)."

## Personas

### Operator

The human who owns the VM. Creates tasks to run tools in workspaces. Monitors task output via the
console. May run tasks as themselves (admin user) or as an agent user.

### Agent

An isolated Linux user scoped to a workspace. Agents have their own home directory, PATH, and
installed tools. Agents exist independently of tasks -- they are created and managed via
`agentworks agent create --workspace <ws>`. Tasks can optionally run in agent mode to use an
agent's identity and environment.

## Domain Model

### Task

A task is a named work stream with the following properties:

- **Name**: unique within its workspace. Must conform to tmux session naming constraints (no dots
  or colons). The tmux session name on the VM is `<workspace>--<task>`, using the workspace as a
  namespace (consistent with the existing `--` separator convention for agent linux usernames).
- **Workspace**: the workspace the task is rooted in. The task's working directory is the
  workspace's path.
- **Mode**: determines which Linux user runs the task command. A task runs in one of two modes:
  - **Admin mode** (default): the task runs as the VM's admin user. No agent is required. This is
    the simple path for operators who want to run tools in a workspace without the complexity of
    agent isolation.
  - **Agent mode**: the task runs as a specific agent's Linux user (via login shell). The agent
    must already exist in the workspace. This provides full isolation -- the task inherits the
    agent's home directory, PATH, installed tools, and permissions.
- **Template**: determines what command the task runs (e.g., "claude"). See Task Templates below.
- **Status**: reflects whether the task is running, stopped, or failed.

### Hierarchy

```text
VM
  Workspace
    Agent (optional, isolated Linux user)
    Task (admin mode -- runs as VM admin user)
    Task (agent mode -- runs as a workspace agent's Linux user)
```

Agents and tasks are both scoped to a workspace. A workspace can have any number of each. An
agent-mode task references an agent in the same workspace. It is the operator's responsibility to
ensure that multiple tasks in the same workspace do not conflict (e.g., both editing the same
files).

### Console

The console is a VM-level tmux session that provides a unified view of all tasks running on that
VM. It is a convenience layer, not a requirement.

- Full tmux controls (split panes, create windows, rearrange layout).
- Each task appears as a window in the console, attached to the task's own tmux session.
- The operator can add their own windows (shells, monitoring, etc.).
- If the console is killed or detached, tasks continue running independently.
- When a new task is created, it is automatically added as a window in the console (if the console
  is running).
- The console can be recreated at any time with all currently running tasks.

## Requirements

### R1: Task lifecycle

An operator can create, stop, restart, and delete tasks.

- **Create** registers the task in the database and starts it (creates the tmux session, runs the
  template command).
- **Stop** sends a signal to the running command. If the command does not exit within a grace
  period, the tmux session is killed.
- **Restart** re-runs the task. If the template defines a `restart_command`, that is used instead
  of `command` (e.g., `claude --resume` instead of `claude --name`). Errors if the task is still
  running unless `--force` is passed, which kills the existing session first.
- **Delete** stops the task if running (with confirmation), then removes it from the database.

A task's tmux session uses `<workspace>--<task>` as the session name (consistent with the namespace
convention described in the domain model). When the template command exits, the tmux session exits.
When the tmux session is killed, the task is considered stopped.

### R2: Locked-down task sessions

Task tmux sessions are locked down:

- No new windows or panes can be created.
- No key bindings for split, new-window, or session management.
- The status bar is hidden.
- The user's `tmux.conf` is loaded, preserving the prefix key and other personal settings.
- Large scrollback buffer (configurable, default 50,000 lines).
- The operator can still attach to view output and interact with the running command.

The purpose is to ensure one task = one process = one tmux session, with no way to spawn additional
processes that would outlive or escape the task lifecycle.

### R3: Task templates

A task template defines the command that a task runs. Templates are defined in the agentworks
config file.

- Each template has a name and a command (string or list).
- The command is run in the workspace directory as the task's user.
- A built-in "default" template that runs a login shell is used when --template is not specified.
- Operators can define custom templates (e.g., "claude", "aider", "cursor-agent").
- Templates may include environment variables to set.
- The "default" template can be overridden in config (same pattern as workspace templates).

### R4: Console

The console is a VM-level tmux session that aggregates tasks.

- Created on demand or automatically when the first task starts.
- Each running task gets a window in the console that attaches to the task's tmux session.
- New tasks are automatically added as windows.
- The console has full tmux controls -- it is the operator's power-user interface.
- If the console does not exist when a task starts, the task still runs; the console is not
  required.
- The console can be recreated on demand (`--recreate`), which kills the existing console and
  rebuilds it from all currently running tasks. Useful if the console gets into a bad state or
  the operator wants a fresh layout.

### R5: CLI surface

New command group `agentworks task`:

- `task create [--workspace <ws>] [--name <name>] [--template <tpl>] [--agent <agent>]` -- create
  and start a task. Name and workspace are prompted if omitted. Runs in admin mode by default.
  Pass `--agent` to run in agent mode as the specified agent user.
  Pass `--new-workspace` to create a workspace on the fly (with optional `--workspace-name`,
  `--workspace-template`, and `--vm`). Mutually exclusive with `--workspace`.
- `task list [--workspace <ws>]` -- list tasks, showing status.
- `task stop <name> --workspace <ws>` -- stop a running task.
- `task restart <name> --workspace <ws> [--force]` -- restart a task. Uses `restart_command` if
  defined in the template. Errors if still running unless `--force` is passed.
- `task attach <name> --workspace <ws>` -- attach to the task's tmux session (read-only view of
  output, or interactive if the command accepts input).
- `task delete <name> --workspace <ws>` -- stop and remove a task.
- `task logs <name> --workspace <ws>` -- dump the scrollback buffer.

New commands for the console:

- `vm console <vm-name>` -- attach to the VM console, creating it if it does not exist. Creates
  one window per currently running task. Refuses to run inside an existing tmux session unless
  `--allow-nesting` is passed.
- `vm console <vm-name> --recreate` -- kill the existing console (if any), rebuild it from the
  current set of running tasks, and attach.
- `vm console <vm-name> --allow-nesting` -- allow running inside an existing tmux session
  (not recommended due to prefix key conflicts).

### R6: Database

Tasks are stored in the local SQLite database with:

- Task name, workspace name, template name, mode (admin or agent), linux user.
- Status (running, stopped).
- Created/updated timestamps.

### R7: Relationship to agents

Tasks and agents are complementary but independent:

- **Agents** are workspace-scoped Linux users that provide isolation (own home directory, PATH,
  installed tools). They are created and deleted independently via `agentworks agent` commands.
  An agent's existence does not imply any running process.
- **Tasks** are workspace-scoped running processes with lifecycle management. A task always runs
  as some Linux user, but that user can be either the VM admin or an agent.
- **Admin mode** (default): the task runs as the VM's admin user. No agent is needed. This is the
  simple path -- the operator runs a tool in a workspace as themselves.
- **Agent mode**: the task runs as a specific agent's Linux user via login shell, inheriting the
  agent's full environment. The agent must already exist in the same workspace. This provides
  process isolation on top of the user isolation that the agent already provides.
- An agent can have zero, one, or many tasks running as it. A task in agent mode references
  exactly one agent.
- The existing agent create/delete workflow is unchanged.
- The tmuxinator-based session management is replaced by tasks and the console.

## Future Considerations

- Task groups or profiles (e.g., "start my usual 3 tasks").
- Task restart policies (auto-restart on exit, with backoff).
- Task output streaming/capture to files.
- Task-level resource limits (CPU, memory).
- Remote task management (create/monitor tasks from the operator's machine without SSH).
