# Agent tmux sockets -- locked

Locked: 2026-04-10

## Summary

Agent-mode tmux sessions now run as the agent's Linux user on a per-task socket, instead of running
on the admin's tmux server with `sudo su --login` inside the pane. This fixes terminal resize
propagation (broken by sudo's `use_pty` allocating an intermediary PTY) and eliminates the security
concern of agent sessions running under the admin's tmux server.

## Key decisions

- **Socket paths keyed on Linux username**, not agent name. The VM layer has no database and
  operates entirely in terms of Linux users.
- **Root directory mode `2771`** (not `2770`). Agent users need the `other` execute bit to traverse
  into their own subdirectory since they are not members of the socket group.
- **`tmux-agent-access` group** contains only the admin user. Agent users access their own directory
  via ownership. Cross-agent isolation is enforced by directory ownership.
- **Belt-and-suspenders access control**: filesystem group permissions on the socket AND tmux
  `server-access` ACL granting access to all group members.
- **`sudo` for non-interactive tmux operations** (kill, has-session, send-keys, capture-pane) on
  agent sockets. Interactive attach does NOT use sudo to avoid reintroducing the `use_pty` resize
  problem.
- **`window-size latest` and `aggressive-resize on` retained** in the task config as
  defense-in-depth.
- **Migration helpers** (`_kill_task_any_server`, `_session_exists_any_server`) check both the agent
  socket and the default server to handle the transition from legacy sessions. These can be removed
  once all VMs have been migrated.
- **No permanent workarounds** for the group membership inheritance issue (existing tmux server
  processes don't pick up new groups). Migration requires killing the tmux server once.

## Files changed

- `cli/agentworks/tasks/tmux.py` -- core socket infrastructure and session management
- `cli/agentworks/tasks/manager.py` -- socket path threading through all task operations
- `cli/agentworks/tasks/console.py` -- console wrapper socket support
- `cli/agentworks/workspaces/tmuxinator.py` -- workspace console socket support
- `cli/agentworks/workspaces/manager.py` -- workspace delete socket cleanup
- `cli/agentworks/vms/initializer.py` -- VM init socket directory setup
- `cli/agentworks/agents/manager.py` -- agent create socket dir, agent delete socket cleanup
- `cli/agentworks/ssh.py` -- sudo scoping documentation
- `cli/tests/test_tmuxinator.py` -- socket path and wrapper tests
