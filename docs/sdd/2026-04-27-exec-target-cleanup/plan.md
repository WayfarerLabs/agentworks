# Provisioner shell and exec model cleanup -- implementation plan

## Phase 1: Renames

### 1.1 exec_target -> admin_exec_target (operator via IDE)

- [ ] `cli/agentworks/vms/base.py`: rename abstract method.
- [ ] All provisioner implementations (lima.py, azure.py, wsl2.py, proxmox.py): rename method.
- [ ] All call sites: `provisioner.exec_target(` -> `provisioner.admin_exec_target(`.
- [ ] `ProvisionResult.exec_target` field -> `ProvisionResult.admin_exec_target`.

### 1.2 ssh_target_for_vm -> admin_exec_target (operator via IDE)

- [ ] `cli/agentworks/ssh.py`: rename function.
- [ ] All call sites.

### 1.3 admin_exec_target returns ExecTarget instead of SSHTarget

- [ ] `cli/agentworks/ssh.py`: change `admin_exec_target()` to return `ExecTarget` wrapping
  the SSHTarget internally.
- [ ] Update all call sites that manually wrapped the result in `ExecTarget(ssh=...)` to use
  the return value directly.
- [ ] Call sites that passed `default_timeout` or `logger` when wrapping: pass as params to
  the function or use `dataclasses.replace()`.

**Done when:** All references use the new names. `admin_exec_target()` in ssh.py returns
ExecTarget. Tests pass.

---

## Phase 2: New run() method (run_new)

### 2.1 Add run_new to ExecTarget

- [ ] `cli/agentworks/ssh.py`: add `run_new(command, *, sudo=False, tty=None, check=True,
  timeout=None)` to ExecTarget.
- [ ] `sudo=True` wraps command in `sudo -n bash -c {shlex.quote(command)}`.
- [ ] `tty` parameter: None = transport default, True = request TTY, False = suppress TTY.
- [ ] SSH transport: resolve tty (None -> SSHTarget.force_tty, True -> always, False -> never).
- [ ] Lima, Remote Lima, WSL2: accept and ignore tty parameter.
- [ ] Logging via `self.logger` (already wired into transport-level functions).

### 2.2 Migrate call sites incrementally

Migrate all of the following to `run_new()`:

- [ ] `ExecTarget.run()` call sites -> `run_new()`.
- [ ] `ExecTarget.run_as_root()` call sites -> `run_new(sudo=True)`.
- [ ] `_run_logged()` call sites -> `run_new()` or `run_new(sudo=True)`.
  - `_run_logged(target, cmd, logger)` -> `target.run_new(cmd)`
  - `_run_logged(target, cmd, logger, as_root=True)` -> `target.run_new(cmd, sudo=True)`
- [ ] Manual `sudo -n` / `sudo` in command strings -> `run_new(sudo=True)`.
  - `initializer.py`: `sudo -n /bin/bash`, `sudo chown -R`
  - `azure.py`: `sudo tailscale ip -4`
  - `tmux.py`: `sudo -n tmux`, `sudo rm -f`, `sudo mkdir -p`, `sudo tee`
  - `sessions/manager.py`: `sudo rm -f`
  - Leave `sudo -u <user>` in tmux.py as-is (agent user switching, not root).
- [ ] `run_detached` force_tty hack -> `run_new(tty=False)`.

### 2.3 Delete old methods

- [ ] Delete `ExecTarget.run()` (old version).
- [ ] Delete `ExecTarget.run_as_root()`.
- [ ] Delete standalone `run_as_root()` function in ssh.py.
- [ ] Delete `_run_logged()` in initializer.py.
- [ ] Delete the force_tty target replacement hack in remote_exec.py.

### 2.4 Rename run_new -> run

- [ ] Rename `run_new()` -> `run()` across ExecTarget and all call sites.

**Done when:** Single `run()` method with `sudo` and `tty` params. No `run_as_root`,
`_run_logged`, or manual sudo in command strings. All tests pass.

---

## Phase 3: Proxmox admin_exec_target stub

### 3.1 Stub admin_exec_target

- [ ] `cli/agentworks/vms/provisioners/proxmox.py`: change `admin_exec_target()` to raise
  `NotImplementedError` with a message about the guest agent gap.
- [ ] Wrap `_tailscale_logout` Proxmox path in try/except NotImplementedError with warn.

**Done when:** Proxmox admin_exec_target raises a clear error. Existing Proxmox operations that
used it (Tailscale logout on delete) degrade gracefully with a warning.

---

## Phase 4: VM rekey command

### 4.1 rekey_vm() function

- [ ] `cli/agentworks/vms/manager.py`: new `rekey_vm()` function.
  - Validate VM exists and is running.
  - Get provisioner and admin_exec_target (provisioning shell).
  - For Azure: attach public IP.
  - Collect new auth key from TAILSCALE_AUTH_KEY env var or prompt.
  - `target.run("tailscale down && tailscale logout", sudo=True)`
  - `target.run("tailscale up --auth-key <key>", sudo=True)`
  - `result = target.run("tailscale ip -4", sudo=True)`
  - If wait_for_share: prompt, verify connectivity.
  - Update DB, sync SSH config.
  - For Azure: detach public IP.
  - Log event.

### 4.2 CLI command

- [ ] `cli/agentworks/cli.py`: register `vm rekey` with:
  - `name` (required, positional)
  - `--wait-for-share` (optional flag)

### 4.3 Completions

- [ ] `cli/agentworks/completions/`: add `rekey` to VM command completion tree.

### 4.4 Documentation

- [ ] `cli/README.md`: document `vm rekey`.

**Done when:** `agentworks vm rekey <name>` switches Tailscale account on a Lima VM.
`--wait-for-share` pauses for manual sharing and verifies connectivity.

---

## Phase 5: Testing

### 5.1 Unit tests

- [ ] Test `run(sudo=True)` wraps with `sudo -n bash -c`.
- [ ] Test `tty` parameter resolution (None/True/False) for SSH transport.
- [ ] Test Proxmox admin_exec_target raises NotImplementedError.
- [ ] Test rekey_vm logic (mock admin_exec_target, verify DB update).

### 5.2 Manual testing

- [ ] `agentworks vm rekey <vm> --wait-for-share` on a Lima VM.
- [ ] `agentworks vm start <vm>` after ephemeral key expiry (rejoin still works).
- [ ] `agentworks vm delete <vm>` (Tailscale logout still works).
- [ ] Verify `run_detached` backup still works (tty=False path).
- [ ] Verify VM init/reinit works end-to-end (largest consumer of run calls).

---

## Files to modify

| File | Change |
|------|--------|
| `cli/agentworks/vms/base.py` | Rename exec_target -> admin_exec_target |
| `cli/agentworks/ssh.py` | Add run_new with sudo/tty, delete run_as_root, rename |
| `cli/agentworks/remote_exec.py` | Drop force_tty hack, use tty=False |
| `cli/agentworks/vms/provisioners/*.py` | Rename method in all provisioners |
| `cli/agentworks/vms/provisioners/proxmox.py` | Stub admin_exec_target |
| `cli/agentworks/vms/manager.py` | Rename call sites, add rekey_vm() |
| `cli/agentworks/vms/initializer.py` | Delete _run_logged, migrate all call sites |
| `cli/agentworks/sessions/tmux.py` | Migrate manual sudo call sites |
| `cli/agentworks/sessions/manager.py` | Migrate manual sudo call sites |
| `cli/agentworks/agents/manager.py` | Rename call sites |
| `cli/agentworks/cli.py` | Register vm rekey command |
| `cli/agentworks/completions/` | Add rekey to completion tree |
| `cli/README.md` | Document vm rekey |
