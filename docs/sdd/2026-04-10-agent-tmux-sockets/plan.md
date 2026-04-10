# Agent tmux sockets -- implementation plan

## Phase 1: Infrastructure helpers

### 1.1 Constants and socket path helper

Add to `cli/agentworks/tasks/tmux.py`:

- [ ] Constant `AGENT_SOCKET_ROOT = "/run/agentworks/agent-tmux-sockets"`.
- [ ] Constant `AGENT_SOCKET_GROUP = "tmux-agent-access"`.
- [ ] Function `agent_socket_path(linux_user: str, workspace_name: str, task_name: str) -> str`
  that returns `{AGENT_SOCKET_ROOT}/{linux_user}/{workspace}--{task}.sock`. Uses Linux username
  since the VM layer operates entirely in terms of Linux users.

Definition of done: function is importable and returns the expected path string. Unit-testable with
no side effects.

### 1.2 Idempotent ensure helpers

Add to `cli/agentworks/tasks/tmux.py` (or a new `cli/agentworks/agents/socket_dir.py` if it makes
more sense to keep agent infra separate from tmux session management -- use judgment):

- [ ] Function `ensure_agent_socket_root(run_command, admin_username: str)`:
  - Creates group `tmux-agent-access` if it does not exist.
  - Adds `admin_username` to the group.
  - Creates `/run/agentworks/agent-tmux-sockets/` owned by `root:tmux-agent-access` mode `2770`.
  - All steps idempotent.
- [ ] Function `ensure_agent_socket_dir(run_command, linux_user: str)`:
  - Creates `/run/agentworks/agent-tmux-sockets/<linux_user>/` owned by
    `<linux_user>:tmux-agent-access` mode `2770`.
  - Idempotent.

Definition of done: running either function twice produces no errors and the directory/group state
is correct.

---

## Phase 2: Integrate into provisioning flows

### 2.1 VM init/reinit

Location: `cli/agentworks/vms/initializer.py`, within `_phase_b_setup()`.

- [ ] Call `ensure_agent_socket_root(run_command, admin_username)` during Phase B setup.
  Place it near the existing workspace directory ACL setup (around line 1167) since it is similar
  infrastructure work.
- [ ] After creating the root directory, loop over all agents on this VM and call
  `ensure_agent_socket_dir(run_command, agent.linux_user)` for each.

Definition of done: `agentworks vm reinit` creates the group, root directory, and per-agent
directories. Running it again is a no-op.

### 2.2 Agent create

Location: `cli/agentworks/agents/manager.py`, within `_create_agent_on_vm()`.

- [ ] Call `ensure_agent_socket_dir(run_command, linux_user)` after user creation (around
  line 594).

Definition of done: `agentworks agent create` creates the per-agent socket directory. The root
directory and group must already exist (created during VM init).

### 2.3 Agent reinit

Location: `cli/agentworks/agents/manager.py`, within `reinit_agent()` (which calls
`_create_agent_on_vm()`).

- [ ] Since `_create_agent_on_vm()` is reused, the socket dir creation from 2.2 applies here
  automatically.

Definition of done: `agentworks agent reinit` ensures the socket directory exists.

---

## Phase 3: Task session refactoring

### 3.1 Refactor `create_task_session` for agent mode

Location: `cli/agentworks/tasks/tmux.py`, `create_task_session()`.

- [ ] When `is_admin=False`, instead of wrapping the command in `sudo su --login`:
  1. Derive socket path via `agent_socket_path(linux_user, workspace_name, task_name)`.
     The `linux_user` parameter is already available on the function.
  2. Build the shell command for the pane. The agent's tmux server runs as the agent user, so the
     pane shell is already the agent's shell. Use `$SHELL -lic "cd <path> && <command>"` (same
     pattern as admin mode). Need to verify that `sudo -u <agent> tmux new-session` gives the right
     `$SHELL`. May need to use an explicit shell path or `sudo --login -u <agent>`.
  3. Create the session:
     `sudo -u <linux_user> tmux -S <socket> new-session -d -s <session> ...`.
  4. Fix socket permissions: `chmod g+rwx <socket>`.
  5. Grant tmux server-access to all members of the `tmux-agent-access` group. Enumerate via
     `getent group tmux-agent-access`, then call `server-access -a` for each member. This is
     robust to future changes (additional operators, monitoring users, etc.).
- [ ] Admin mode remains unchanged.

The socket setup (steps 3-5) runs on every call to `create_task_session` for agent-mode tasks.
Since both `create_task` and `restart_task` call `create_task_session`, the socket is built on
both task creation and task restart.

Definition of done: agent-mode task sessions are created with the agent's tmux server. The socket
is accessible by all `tmux-agent-access` group members. The agent's shell runs directly on the
tmux pane PTY (no sudo/su in the process tree).

### 3.2 Add socket awareness to session utility functions

Location: `cli/agentworks/tasks/tmux.py`.

- [ ] Add `socket_path: str | None = None` parameter to `kill_task_session()`,
  `session_exists()`, and `capture_output()`.
- [ ] When `socket_path` is provided, prepend `-S <socket_path>` to the tmux command.

Definition of done: all three functions work with both default-server (admin) and custom-socket
(agent) sessions.

### 3.3 Update call sites in task manager

Location: `cli/agentworks/tasks/manager.py`.

- [ ] In `stop_task()`: look up the task's agent_name. If agent-mode, derive socket path and pass
  to `send-keys`, `session_exists`, and `kill_task_session`. The `send-keys` call (line 313) is
  currently inline -- either extract to a function or add socket awareness inline.
- [ ] In `restart_task()`: socket-awareness for the `session_exists` and `kill_task_session` calls
  that precede the `create_task_session` call. The create call itself handles socket setup (3.1).
- [ ] In `delete_task()`: pass socket path to `kill_task_session` and `session_exists`.
- [ ] In `describe_task()` and `list_tasks()` (via `_reconcile_status`): pass socket path to
  `session_exists`.
- [ ] In `attach_task()`: build the tmux attach command with `-S <socket>` for agent tasks.
- [ ] In `task_logs()`: pass socket path to `capture_output`.

Helper approach: add a small private function like `_socket_path_for_task(db, task)` that returns
the socket path if the task is agent-mode, or None for admin tasks. For agent-mode tasks, it looks
up the agent's `linux_user` from the database via `task.agent_name`, then calls
`agent_socket_path(linux_user, task.workspace_name, task.name)`.

Definition of done: all task manager operations work correctly for both admin and agent tasks.

### 3.4 Update console wrapper

Location: `cli/agentworks/tasks/console.py`, `_add_task_window()`.

- [ ] The wrapper shell script needs the socket path for has-session and attach. Add
  `socket_path: str | None = None` parameter.
- [ ] When socket_path is provided, the wrapper becomes:

  ```bash
  unset TMUX;
  while tmux -S <socket> has-session -t <session> 2>/dev/null; do
    tmux -S <socket> attach -t <session>;
    sleep 0.5;
  done
  ```

- [ ] Update `add_task_to_console()` (the public entry point) to accept and thread through the
  socket path.

Definition of done: console windows for agent tasks attach via the socket. No `sudo` in the attach
path.

### 3.5 Update tmuxinator workspace console config

Location: `cli/agentworks/workspaces/tmuxinator.py`, `generate_config()`.

- [ ] The wrapper script in the generated YAML needs the socket path for agent tasks. The function
  already receives task objects -- it needs to derive the socket path for agent-mode tasks.
- [ ] The function needs the Linux username for agent-mode tasks to derive the socket path. Either
  look up agent -> linux_user at config generation time, or pass it through from the caller. The
  task objects carry `agent_name`; the DB maps that to `linux_user`.

Definition of done: workspace console windows for agent tasks use the socket path.

---

## Phase 4: Clean up old agent-mode code path

### 4.1 Remove sudo su --login code path

Location: `cli/agentworks/tasks/tmux.py`, `create_task_session()`.

- [ ] The old agent-mode code path (`sudo su --login`) can be removed once the socket-based path is
  confirmed working. This is effectively done in 3.1 but called out here as a checkpoint.

### 4.2 Remove window-size / aggressive-resize workaround

Location: `cli/agentworks/tasks/tmux.py`, `generate_restricted_config()`.

- [ ] The `window-size latest` and `aggressive-resize on` settings (lines 56-57) were added to work
  around the resize issue. Evaluate whether they are still needed. They should not cause harm, so
  keeping them is acceptable. Recommend keeping them as defense-in-depth.

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

- [ ] Test `agent_socket_path()` returns the expected path.
- [ ] Test that `generate_config()` (tmuxinator) produces correct wrapper scripts for agent tasks.
- [ ] Update existing `test_tmuxinator.py` tests if the output format changes.

---

## Open questions

1. **Login environment**: Does `sudo -u <agent> tmux new-session` give the pane a proper login
   environment (PATH, rc files sourced)? If not, we may need `sudo --login -u <agent>` or to run
   `exec $SHELL -l` as the pane command. Needs testing in Phase 3.1.

2. **Socket cleanup on task kill**: When a task session is killed, does tmux automatically remove
   the socket file? If the session is the only one on that server, tmux should exit and clean up.
   Verify during testing. If not, add explicit cleanup.

3. **Reboot persistence**: `/run` is a tmpfs cleared on reboot. The directories are recreated by
   VM init/reinit, but if the VM reboots without running reinit, the directories will be gone. tmux
   sessions also do not survive reboot, so this is likely a non-issue (tasks are re-created on
   restart). Verify this assumption.
