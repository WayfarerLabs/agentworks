# Agent tmux sockets -- functional requirements

## Background

Agent-mode tasks run inside tmux sessions that are currently owned by the admin user's tmux server.
The actual agent process runs via `sudo su --login <agent-user>` inside the pane. This causes two
problems:

1. **Resize failure**: The system-wide `Defaults use_pty` in sudoers causes `sudo` to allocate an
   intermediary PTY between the tmux pane and the agent's shell. When the terminal resizes, tmux
   resizes its pane PTY, but `sudo` does not propagate the resize to the inner PTY. The agent's
   shell stays stuck at the default detached size (80x24).

2. **Security concern**: The tmux server for agent sessions runs as the admin user. A compromised
   agent process could potentially exploit the tmux session to execute commands as admin, since the
   tmux server (and its command prompt, hooks, etc.) run with admin privileges.

Admin-mode tasks are unaffected because they run directly in the admin's tmux pane without `sudo`.

## Requirements

### R1: Agent tmux sessions run as the agent user

The tmux server for an agent-mode task must run as the agent's Linux user, not the admin user. The
agent's shell connects directly to the tmux pane PTY with no intermediary `sudo` or `su` process.

### R2: Admin can attach to agent tmux sessions

The admin user must be able to attach to, inspect, send keys to, capture output from, and kill
agent tmux sessions. This is required for the VM console, workspace console, task management CLI,
and operational tooling.

### R3: Terminal resize works for agent sessions

When the operator resizes their terminal, the resize must propagate through the console to the
agent's shell. The agent's shell must reflect the correct terminal dimensions.

### R4: Cross-agent isolation is preserved

An agent user must not be able to connect to or interact with another agent's tmux session. The
existing Linux user isolation model (per the user-based security SDD) must be maintained.

### R5: Setup is idempotent and self-healing

The infrastructure (groups, directories) required for this feature must be created idempotently
during:

- **VM init/reinit**: creates the shared group, root socket directory, and ensures all existing
  agents have their per-agent socket directories
- **Agent create**: creates the per-agent socket directory
- **Agent reinit**: ensures the per-agent socket directory exists (repairs if missing)

### R6: Admin-mode tasks are unaffected

Admin-mode tasks must continue to work exactly as they do today. No socket, no user switching. The
changes are scoped to agent-mode tasks only.
