# Tasks -- High-Level Architecture

## Overview

A task is a named tmux session running a single command in a workspace directory. Each task runs in
one of two modes: **admin mode** (as the VM's admin user) or **agent mode** (as a specific agent's
Linux user via login shell). Task sessions are locked down (no splits, no new windows, no prefix
key). A separate VM-level console tmux session provides the operator with a multi-window view of all
tasks, with full tmux controls.

All task state is stored in the local SQLite database. tmux is the execution engine -- the CLI
creates, inspects, and destroys tmux sessions over SSH.

## Components

```text
agentworks CLI (operator's machine)
  task commands         create/stop/start/delete/attach/logs
  console commands      create/attach the VM console
  task templates        config-driven command definitions
  tmux module           generates tmux configs and manages sessions over SSH

VM (remote)
  task tmux sessions    one per task, locked down, runs template command
  console tmux session  VM-level, full controls, windows attach to task sessions
```

## tmux Session Architecture

### Task Sessions

Each task gets its own tmux session. The session name follows the pattern `<workspace>--<task>`,
using the workspace as a namespace on the VM (consistent with the `--` separator used for agent
linux usernames). The session is created with a restricted tmux config that disables all interactive
session management.

```text
tmux new-session -d -s <workspace>--<task> -c <workspace-path> \
  -f <restricted-tmux.conf> \
  '<command>'
```

The restricted tmux config loads the user's `~/.tmux.conf` first (preserving familiar keybindings
such as the prefix key, detach, copy mode, and scroll), then selectively unbinds window/pane/session
management keys on top:

```conf
# Load user config first
if-shell "test -f ~/.tmux.conf" "source-file ~/.tmux.conf"

# Large scrollback buffer (override user config)
set -g history-limit 50000

# Disable status bar
set -g status off

# Disable window/pane/session creation and management.
# The user's prefix key, detach, copy mode, and scroll bindings are preserved.
unbind c          # new-window
unbind %          # split-window -h
unbind '"'        # split-window -v
unbind &          # kill-window
unbind x          # kill-pane
unbind n          # next-window
unbind p          # previous-window
unbind w          # choose-window
unbind s          # choose-session
unbind $          # rename-session
unbind ,          # rename-window
unbind .          # move-window
unbind !          # break-pane
unbind :          # command-prompt (prevents arbitrary tmux commands)
```

When the command exits, the tmux session exits (`remain-on-exit off` is the default). The CLI
detects this by checking session existence and updates the task status accordingly.

The scrollback buffer size (history-limit) is configurable via `[task.config]` in the agentworks
config. The default of 50,000 lines provides substantial history for long-running agent sessions.

### Console Sessions

There are two levels of console:

**VM console** (`vm-console`): A regular tmux session (default config, full controls) at the VM
level. Each task window attaches to a task's tmux session (with `$TMUX` unset to allow nesting).
Since the task session's management keys are selectively unbound, the console's prefix key works
without conflict. Task windows use a wrapper that re-attaches if the connection drops and shows a
message when the task session ends.

```text
vm-console (full tmux)
  Window 0: "admin-shell"          ->  login shell for the admin user
  Window 1: "myproject--claude-1"  ->  attached to task session (locked-down)
  Window 2: "myproject--claude-2"  ->  attached to task session (locked-down)
```

**Workspace console** (`ws-<name>-console`): A per-workspace tmux session managed via tmuxinator.
Like the VM console, it provides an admin-shell window plus one window per task. The workspace
console is regenerated from DB state on structural changes (task create/delete/restart, agent
create/delete).

```text
ws-myproject-console (tmuxinator, full tmux)
  Window 0: "admin-shell"          ->  login shell for the admin user
  Window 1: "myproject--claude-1"  ->  attached to task session (locked-down)
```

The `vm console` and `workspace console` commands refuse to run inside an existing tmux session to
avoid confusing prefix key conflicts. Pass `--allow-nesting` to override this check.

When a task stops, its console window shows a message indicating the session has ended
(`remain-on-exit` is enabled on the console session).

### Lifecycle Interactions

- **Task created**: tmux session started. If console exists, a new window is added.
- **Task stopped**: tmux session killed. Console window's attach command exits.
- **Task started (restart)**: new tmux session created. Console window can be refreshed.
- **Console created**: one window per currently-running task, each attaching to the task session.
- **Console killed**: no effect on task sessions -- they continue independently.
- **Console recreated**: rebuilds windows from the current set of running tasks.

## Task Templates

Templates are defined in the agentworks config under `[task_templates]`:

```toml
[task_templates.my-template]
command = "claude --name {{task_name}}"
description = "Claude Code interactive session"
restart_command = "claude --continue --name {{task_name}}"
```

The single built-in template is "default", which runs a login shell with an empty command. Users can
override it by defining `[task_templates.default]` in their config. When `--template` is not
specified, the "default" template is used.

Template resolution (same pattern as workspace templates):

1. If `--template` is specified, use that template.
2. Otherwise, use the "default" template (built-in or user-defined).

### Template variables

Template commands (and env values) support `{{var}}` placeholder substitution, using double-brace
syntax consistent with nerftools manifests. Available variables:

- `{{task_name}}` -- the task name
- `{{workspace_name}}` -- the workspace name

For example, a claude template can use `claude --name {{task_name}}` so that the Claude session name
is tied to the task, giving the operator a consistent name across task restarts.

### Command execution

The command is executed via a login shell to pick up the user's profile, PATH, and environment:

- **Admin mode**: the tmux session runs as the admin user (who owns the SSH connection). The
  template command runs directly in the session.
- **Agent mode**: the tmux session wraps the command in `su --login <linux-user>` to get a proper
  login shell as the agent user, inheriting the agent's home directory, PATH, and installed tools.

### Restart command

Templates may specify a `restart_command` that is used instead of `command` when a task is
restarted. This is useful for tools that support resuming a previous session (e.g.
`claude --continue`). If `restart_command` is not set, the regular `command` is used on restart.

### Environment variables

Templates may also specify environment variables (which also support `{{var}}` substitution):

```toml
[task_templates.claude-with-key]
command = "claude --name {{task_name}}"
restart_command = "claude --continue --name {{task_name}}"
description = "Claude with custom API key"
env = { ANTHROPIC_API_KEY = "sk-..." }
```

## Task Status Model

```text
           create
             |
             v
         [running] ---stop---> [stopped] ---start---> [running]
             |                     |
             | (command exits)     |
             v                     |
         [stopped]             delete
             |                     |
          delete                   v
             |                 (removed)
             v
         (removed)
```

Status is determined by a combination of database state and tmux session existence:

- **running**: DB says running AND tmux session exists.
- **stopped**: DB says stopped OR tmux session does not exist (command exited).
- On any status query, the CLI checks tmux session existence and reconciles.

## SSH Execution

All tmux commands are executed over SSH using the existing `agentworks.ssh` module. The pattern is
the same as current tmuxinator management:

```python
session = f"{workspace_name}--{task_name}"
ssh.run(vm, f"tmux new-session -d -s {session} ...")
ssh.run(vm, f"tmux has-session -t {session} 2>/dev/null")
ssh.run(vm, f"tmux kill-session -t {session}")
ssh.run(vm, f"tmux capture-pane -t {session} -p -S -50000")
```

For attaching (interactive), the CLI uses `ssh -t` to allocate a TTY and runs
`tmux attach -t <task-name>`.

## Restricted tmux Config Deployment

The restricted tmux config file is deployed to the VM during initialization (or on first task
create). It lives at a fixed path (e.g., `/opt/agentworks/tmux-task.conf`). This avoids regenerating
it per task.

## Migration from tmuxinator

The current tmuxinator-based session management (workspace-level tmux sessions with per-agent
windows) is replaced by tasks and the console. The migration path:

1. Tasks and console are introduced as new functionality.
2. The existing tmuxinator code is kept temporarily for backward compatibility.
3. Once tasks are stable, tmuxinator generation and management code is removed.
4. The `tmuxinator` system dependency can eventually be dropped from VM init.

## Config Surface

New config sections:

```toml
[task.config]
history_limit = 50000             # tmux scrollback buffer lines (default: 50000)

[task_templates.default]          # override the built-in default (login shell)
command = "claude --name {{task_name}}"
restart_command = "claude --continue --name {{task_name}}"
description = "Claude Code interactive session"
```

The `[task.config]` section currently supports only `history_limit`. Task templates are defined
under `[task_templates.<name>]` with the fields `command`, `description`, `restart_command`
(optional), and `env` (optional table).
