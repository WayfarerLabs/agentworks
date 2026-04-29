# Provisioner shell and exec model cleanup -- implementation plan

## Phase 1: Renames

### 1.1 exec_target -> admin_exec_target (operator via IDE)

- [x] `cli/agentworks/vms/base.py`: rename abstract method.
- [x] All provisioner implementations (lima.py, azure.py, wsl2.py, proxmox.py): rename method.
- [x] All call sites: `provisioner.exec_target(` -> `provisioner.admin_exec_target(`.
- [x] `ProvisionResult.exec_target` field -> `ProvisionResult.admin_exec_target`.

### 1.2 ssh_target_for_vm -> admin_exec_target (operator via IDE)

- [x] `cli/agentworks/ssh.py`: rename function.
- [x] All call sites.

### 1.3 admin_exec_target returns ExecTarget instead of SSHTarget

- [x] `cli/agentworks/ssh.py`: change `admin_exec_target()` to return `ExecTarget` wrapping
  the SSHTarget internally. Added optional `logger` and `default_timeout` params.
- [x] Update all call sites that manually wrapped the result in `ExecTarget(ssh=...)` to use
  the return value directly.
- [x] Added `_unwrap_ssh()` shim to standalone SSH functions for migration compatibility.

---

## Phase 2: New run() method

### 2.1 Add run (formerly run_new) to ExecTarget

- [x] `cli/agentworks/ssh.py`: single `run(command, *, sudo=False, tty=None, check=True,
  timeout=None)` method.
- [x] `sudo=True` wraps command in `sudo -n bash -c {shlex.quote(command)}`.
- [x] `tty` parameter: None = transport default, True = request TTY, False = suppress TTY.
- [x] SSH transport: resolve tty (None -> SSHTarget.force_tty, True -> always, False -> never).
- [x] Lima, Remote Lima, WSL2: accept and ignore tty parameter.

### 2.2 Migrate call sites

- [x] ~80 call sites migrated across 7 source files and 2 test files.
- [x] `_run_logged()` callers migrated to `target.run()` / `target.run(sudo=True)`.
- [x] `run_detached` force_tty hack replaced with `tty=False`.
- [x] Manual `sudo -n` in remote_exec.py replaced with `sudo=True`.

### 2.3 Delete old methods

- [x] Deleted `ExecTarget.run()` (old per-transport dispatch).
- [x] Deleted `ExecTarget.run_as_root()` (old per-transport sudo dispatch).
- [x] Deleted `_run_logged()` in initializer.py.

### 2.4 Rename run_new -> run

- [x] Renamed across all files.

Note: standalone SSH functions (`run`, `run_as_root`) remain for the sessions/tmux `RunCommand`
callback pattern. These use the `_unwrap_ssh()` shim and will be migrated in a future cleanup.

---

## Phase 3: Proxmox admin_exec_target stub

- [x] `cli/agentworks/vms/provisioners/proxmox.py`: raises `NotImplementedError`.
- [x] `_tailscale_logout` already catches with `except Exception` and warns.

---

## Phase 4: VM rekey command

- [x] `cli/agentworks/vms/manager.py`: `rekey_vm()` function.
- [x] `cli/agentworks/cli.py`: `vm rekey` command with `--wait-for-share`.
- [x] `cli/agentworks/completions/spec.py`: added rekey to completion tree.
- [ ] `cli/README.md`: document `vm rekey`. (deferred to PR description)

---

## Phase 5: Testing

### 5.1 Unit tests

- [x] Test `run(sudo=True)` wraps with `sudo -n bash -c`.
- [x] Test `tty` parameter resolution (None/True/False) for SSH transport.
- [x] Test Proxmox admin_exec_target raises NotImplementedError.
- [x] Lima ignores tty parameter.
- [x] Sudo escapes single quotes correctly.

### 5.2 Manual testing

- [ ] `agentworks vm rekey <vm> --wait-for-share` on a Lima VM.
- [ ] `agentworks vm start <vm>` after ephemeral key expiry (rejoin still works).
- [ ] `agentworks vm delete <vm>` (Tailscale logout still works).
- [ ] Verify `run_detached` backup still works (tty=False path).
- [ ] Verify VM init/reinit works end-to-end (largest consumer of run calls).
