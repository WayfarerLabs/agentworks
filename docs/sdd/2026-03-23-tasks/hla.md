# Tasks -- High-Level Architecture

## Overview

A task is a named tmux session running a single command in a workspace directory. Each task runs
in one of two modes: **admin mode** (as the VM's admin user) or **agent mode** (as a specific
agent's Linux user via login shell). Task sessions are locked down (no splits, no new windows,
no prefix key). A separate VM-level console tmux session provides the operator with a
multi-window view of all tasks, with full tmux controls.

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

Each task gets its own tmux session. The session name follows the pattern
`<workspace>--<task>`, using the workspace as a namespace on the VM (consistent with the `--`
separator used for agent linux usernames). The session is created with a restricted tmux config
that disables all interactive session management.

```text
tmux new-session -d -s <workspace>--<task> -c <workspace-path> \
  -f <restricted-tmux.conf> \
  '<command>'
```

The restricted tmux config:

```conf
# Large scrollback buffer
set -g history-limit 50000

# Disable all window/pane management
set -g status off
set -g prefix None
unbind-key -a

# Re-bind only what is needed for attach/detach and scrollback
bind -n C-c send-keys C-c
```

When the command exits, the tmux session exits (`remain-on-exit off` is the default). The CLI
detects this by checking session existence and updates the task status accordingly.

The scrollback buffer size (history-limit) is configurable via `[task.config]` in the agentworks
config. The default of 50,000 lines provides substantial history for long-running agent sessions.

### Console Session

The console is a regular tmux session (default config, full controls) at the VM level. Its name
is simply `console` (one per VM, no collision risk since each VM is its own machine).

Each task window in the console runs `tmux attach -t <workspace>--<task>`, which provides a
live view of the task session. Since the task session has all keybindings stripped, the
console's prefix key works without conflict -- there is no nested-prefix problem.

```text
Console (full tmux)
  Window 0: "myproject--claude-1"  ->  tmux attach -t myproject--claude-1  (locked-down)
  Window 1: "myproject--claude-2"  ->  tmux attach -t myproject--claude-2  (locked-down)
  Window 2: (operator's own shell, if desired)
```

When a task stops, its console window shows the attach command exiting. The window remains
(the operator can close it or it can be cleaned up on next console refresh).

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
[task_templates.claude]
command = "claude"
description = "Claude Code interactive session"

[task_templates.claude-resume]
command = "claude --resume"
description = "Claude Code resume last conversation"

[task_templates.shell]
command = "bash"
description = "Plain shell session"
```

Template resolution:

1. If `--template` is specified, use that template.
2. Otherwise, use the default template from `[task.config]` (defaults to "claude").
3. Built-in templates ("claude", "shell") are always available and can be overridden.

The command is executed via a login shell to pick up the user's profile, PATH, and environment:

- **Admin mode**: the tmux session runs as the admin user (who owns the SSH connection). The
  template command runs directly in the session.
- **Agent mode**: the tmux session wraps the command in `su --login <linux-user>` to get a
  proper login shell as the agent user, inheriting the agent's home directory, PATH, and
  installed tools.

Templates may also specify environment variables:

```toml
[task_templates.claude-with-key]
command = "claude"
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

All tmux commands are executed over SSH using the existing `agentworks.ssh` module. The pattern
is the same as current tmuxinator management:

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
create). It lives at a fixed path (e.g., `/opt/agentworks/tmux-task.conf`). This avoids
regenerating it per task.

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
default_template = "claude"       # default template for task create
history_limit = 50000             # tmux scrollback buffer lines

[task_templates.claude]
command = "claude"
description = "Claude Code interactive session"
```
