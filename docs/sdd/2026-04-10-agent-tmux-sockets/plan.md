# Agent tmux sockets -- implementation plan

## Phase 1: Infrastructure helpers

### 1.1 Constants and socket path helper

Add to `cli/agentworks/tasks/tmux.py`:

- [x] Constant `AGENT_SOCKET_ROOT = "/run/agentworks/agent-tmux-sockets"`.
- [x] Constant `AGENT_SOCKET_GROUP = "tmux-agent-access"`.
- [x] Function `agent_socket_path(linux_user: str, workspace_name: str, task_name: str) -> str`
  that returns `{AGENT_SOCKET_ROOT}/{linux_user}/{workspace}--{task}.sock`. Uses Linux username
  since the VM layer operates entirely in terms of Linux users.

### 1.2 Idempotent ensure helpers

Added to `cli/agentworks/tasks/tmux.py`:

- [x] Function `ensure_agent_socket_root(run_command, admin_username: str)`:
  - Creates group `tmux-agent-access` if it does not exist.
  - Adds `admin_username` to the group.
  - Creates `/run/agentworks/agent-tmux-sockets/` owned by `root:tmux-agent-access` mode `2770`.
  - All steps idempotent.
- [x] Function `ensure_agent_socket_dir(run_command, linux_user: str)`:
  - Creates `/run/agentworks/agent-tmux-sockets/<linux_user>/` owned by
    `<linux_user>:tmux-agent-access` mode `2770`.
  - Idempotent.

---

## Phase 2: Integrate into provisioning flows

### 2.1 VM init/reinit

- [x] Call `ensure_agent_socket_root` during Phase B setup in `_phase_b_setup()`.
- [x] Loop over all agents on the VM and call `ensure_agent_socket_dir` for each.

### 2.2 Agent create

- [x] Call `ensure_agent_socket_dir` in `_create_agent_on_vm()` after user creation.

### 2.3 Agent reinit

- [x] Covered by 2.2 since `_create_agent_on_vm()` is reused.

---

## Phase 3: Task session refactoring

### 3.1 Refactor `create_task_session` for agent mode

- [x] Agent-mode sessions created as the agent user via
  `sudo --login -u <user> tmux -S <socket> new-session ...`.
- [x] Socket permissions fixed with `chmod g+rwx`.
- [x] `server-access` granted to all `tmux-agent-access` group members.
- [x] Admin mode unchanged.
- [x] Added `_tmux_cmd()` helper and `_grant_server_access()` helper.
- [x] Added `send_keys()` function for socket-aware key sending.

### 3.2 Add socket awareness to session utility functions

- [x] `kill_task_session()`, `session_exists()`, `capture_output()`, `send_keys()` all accept
  optional `socket_path` parameter.

### 3.3 Update call sites in task manager

- [x] Added `_socket_path_for_task(db, task)` helper.
- [x] `stop_task()` uses socket-aware `send_keys`, `session_exists`, `kill_task_session`.
- [x] `restart_task()` uses socket-aware `session_exists` and `kill_task_session`.
- [x] `delete_task()` uses socket-aware `session_exists` and `kill_task_session`.
- [x] `_reconcile_status()` uses socket-aware `session_exists`.
- [x] `attach_task()` uses `_tmux_cmd` for socket-aware attach.
- [x] `task_logs()` passes socket path to `capture_output`.
- [x] `create_task` and `restart_task` pass socket path to `add_task_to_console`.
- [x] `_regenerate_tmuxinator` builds socket path map for `generate_config`.
- [x] Agent deletion in `agents/manager.py` uses socket-aware `kill_task_session`.

### 3.4 Update console wrapper

- [x] `_add_task_window()` accepts `socket_path` and uses `_tmux_cmd()` for wrapper commands.
- [x] `add_task_to_console()` threads socket path through.
- [x] `create_console()` and `recreate_console()` accept `socket_paths` dict.
- [x] `attach_console()` builds socket path map for all running tasks.

### 3.5 Update tmuxinator workspace console config

- [x] `generate_config()` accepts `socket_paths` dict and uses `_tmux_cmd()` in wrappers.

---

## Phase 4: Clean up old agent-mode code path

### 4.1 Remove sudo su --login code path

- [x] Replaced by the socket-based path in 3.1. The old `sudo su --login` agent code path
  has been fully removed from `create_task_session()`.

### 4.2 Remove window-size / aggressive-resize workaround

- [x] Kept as defense-in-depth. These settings are harmless and provide a fallback for
  multi-client scenarios.

---

## Phase 5: Testing

### 5.1 Manual testing checklist

- [ ] Create an admin-mode task. Verify it works as before (no socket, no regression).
- [ ] Create an agent-mode task. Verify:
  - The tmux session is owned by the agent user (`ps aux | grep tmux` shows agent).
  - The socket exists at the expected path with correct ownership and permissions.
  - The shell inside the pane is running as the agent user (`whoami`).
  - Terminal resize propagates correctly (resize terminal, verify `stty size` updates).
- [ ] Attach to an agent task via `agentworks task attach`. Verify it works.
- [ ] View agent task in the VM console. Verify resize works.
- [ ] Stop and restart an agent task. Verify session cleanup and recreation.
- [ ] Delete an agent task. Verify session is killed.
- [ ] Run `agentworks vm reinit`. Verify group and directories are correct.
- [ ] Run `agentworks agent reinit`. Verify socket directory is repaired if missing.
- [ ] View task logs for an agent task. Verify scrollback capture works.

### 5.2 Unit tests

- [x] Test `agent_socket_path()` returns the expected path.
- [x] Test that `generate_config()` produces correct wrapper scripts for agent tasks.
- [x] Test that admin tasks do not include `-S` in wrapper commands.
- [x] Existing tests pass (no regressions).

---

## Open questions

1. **Login environment**: Does `sudo --login -u <agent> tmux new-session` give the pane a proper
   login environment (PATH, rc files sourced)? Using `sudo --login` which should source the
   agent's profile. Needs verification during manual testing.

2. **Socket cleanup on task kill**: When a task session is killed, does tmux automatically remove
   the socket file? If the session is the only one on that server, tmux should exit and clean up.
   Verify during testing. If not, add explicit cleanup.

3. **Reboot persistence**: `/run` is a tmpfs cleared on reboot. The directories are recreated by
   VM init/reinit, but if the VM reboots without running reinit, the directories will be gone. tmux
   sessions also do not survive reboot, so this is likely a non-issue (tasks are re-created on
   restart). Verify this assumption.
