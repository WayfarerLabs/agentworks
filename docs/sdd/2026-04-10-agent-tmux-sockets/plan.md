# Agent tmux sockets -- implementation plan

## Phase 1: Infrastructure helpers

### 1.1 Constants and socket path helper

Added to `cli/agentworks/tasks/tmux.py`:

- [x] Constant `AGENT_SOCKET_ROOT = "/run/agentworks/agent-tmux-sockets"`.
- [x] Constant `AGENT_SOCKET_GROUP = "tmux-agent-access"`.
- [x] Function `agent_socket_path(linux_user, workspace_name, task_name)`.

### 1.2 Idempotent ensure helpers

Added to `cli/agentworks/tasks/tmux.py`:

- [x] `ensure_agent_socket_root(run_command, admin_username)` -- creates group, adds admin,
  creates root directory with mode `2771`.
- [x] `ensure_agent_socket_dir(run_command, linux_user)` -- creates per-agent subdirectory
  with mode `2770`.

---

## Phase 2: Integrate into provisioning flows

- [x] VM init/reinit: calls `ensure_agent_socket_root` + `ensure_agent_socket_dir` for each agent.
- [x] Agent create/reinit: calls `ensure_agent_socket_dir` in `_create_agent_on_vm()`.

---

## Phase 3: Task session refactoring

- [x] `create_task_session` agent mode: creates session as agent user via
  `sudo --login -u <user> tmux -S <socket> new-session ...`, then `sudo chmod g+rwx` on the
  socket, then grants `server-access` to all `tmux-agent-access` group members.
- [x] `tmux_cmd()` helper with optional `sudo` flag for non-interactive operations on agent
  sockets (kill, has-session, send-keys, capture-pane). Interactive attach does not use sudo.
- [x] `build_socket_paths()` helper to deduplicate socket path map construction.
- [x] All call sites updated: stop, restart, delete, describe, list, attach, logs, console,
  tmuxinator, agent delete, workspace delete.
- [x] Migration helpers: `_kill_task_any_server` and `_session_exists_any_server` check both
  the agent socket and the default server to handle legacy sessions.
- [x] Legacy warning when an agent task is found on the default server (suppressed during
  stop/restart where the user is already acting on it).
- [x] Bidirectional status reconciliation (RUNNING -> STOPPED and STOPPED -> RUNNING).
- [x] `recreate_console` collapsed into `create_console(recreate=True)`.

---

## Phase 4: Clean up

- [x] Old `sudo su --login` agent code path fully removed from `create_task_session()`.
- [x] `window-size latest` and `aggressive-resize on` kept as defense-in-depth.
- [x] `run_as_root` in `ssh.py` annotated with NOTE about sudo scoping pitfall.

---

## Phase 5: Testing

### 5.1 Manual testing (verified on vm1)

- [x] Admin task (`ws1--ats1`) works as before on default server.
- [x] Agent tasks run as `agt--ag1` (verified via `ps aux`).
- [x] Sockets at expected paths with correct ownership (`agt--ag1:tmux-agent-access 770`).
- [x] Resize propagation works (`ts3` at 240x93, not stuck at 80).
- [x] Console attach works via socket (all 5 agent tasks attached).
- [x] `server-access` ACL correctly grants `agentworks` (W) and `agt--ag1` (W).
- [x] Agent user is NOT in `tmux-agent-access` group (cross-agent isolation).
- [x] No legacy agent sessions remain on default server.
- [x] VM reinit creates group, root directory, and per-agent directories idempotently.
- [x] Task restart kills old session and creates new one on agent socket.

### 5.2 Unit tests

- [x] `agent_socket_path()` returns expected path.
- [x] `generate_config()` produces `-S <socket>` wrappers for agent tasks.
- [x] Admin tasks do not include `-S` in wrappers.
- [x] All 6 tests pass, ruff clean.

---

## Resolved questions

1. **Login environment**: `sudo --login -u <agent> tmux new-session` works correctly. The
   `--login` flag sources the agent's profile. The pane shell runs as the agent with proper
   PATH and rc files.

2. **Socket cleanup**: tmux removes the socket file when the server exits (last session killed).
   No explicit cleanup needed.

3. **Reboot persistence**: `/run` is tmpfs, cleared on reboot. tmux sessions also don't survive
   reboot. VM reinit recreates the directories. Non-issue.

4. **Root directory mode**: Changed from `2770` to `2771`. The `other` execute bit is required
   so agent users (who are not in the group) can traverse into their own subdirectory.

5. **sudo scoping**: `sudo -n cmd1 && cmd2` only applies sudo to `cmd1`. The ensure functions
   issue individual `run_command` calls so each gets its own sudo from the wrapper.

6. **Group membership and tmux server**: Existing tmux server processes don't pick up new group
   memberships. Migration requires killing the admin's tmux server and restarting. This is a
   one-time operation, not worth adding permanent workarounds (sg/sudo in wrappers).

---

## Migration procedure

1. `agentworks vm reinit` -- creates group, directories
2. Kill the admin's tmux server on the VM (e.g., `tmux kill-server` via SSH)
3. `agentworks task restart --force` for each agent-mode task
4. `agentworks vm console --recreate`
