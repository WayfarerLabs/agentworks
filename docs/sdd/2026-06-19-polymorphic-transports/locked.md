# Polymorphic transports -- Lockfile

## 2026-06-24

All plan items are complete. The polymorphic-transports refactor shipped in PR #130 across the
five-phase plan, plus follow-up cleanup and one Windows-specific regression fix:

- `Transport` ABC defined at `cli/agentworks/transports/base.py` with the operator I/O surface as
  abstract methods (`run`, `interactive`, `copy_to`, `copy_from`, `call_streaming`, `describe`) and
  two concrete defaults (`copy_dir_to`, `write_file`) that compose the abstract surface.
  Per-transport classes (`SSHTransport`, `LimaTransport`, `RemoteLimaTransport`, `WSL2Transport`)
  each live in one file under `cli/agentworks/transports/`. R1 / R4.
- Three named factories plus the low-level helper expose the new shape: `transport(vm, config)`
  (canonical admin), `agent_transport(vm, config, agent)` (canonical per-agent),
  `provisioner_transport(db, vm, config, *, stack)` (platform-native opt-in),
  `transport_for_user(vm, config, *, user, ...)` (mid-create case). All in
  `cli/agentworks/transports/__init__.py`. R2.
- The factories never fall back: `transport()` raising never leads to `provisioner_transport()`
  being called. A test pins this invariant. R3.
- `VMProvisioner.admin_exec_target` renamed to `provisioner_transport` (returning a `Transport`);
  `ProvisionResult.admin_exec_target` renamed to `provisioner_transport`. R5.
- `VMProvisioner.transient_route(vm) -> AbstractContextManager[None]` (default `nullcontext`)
  absorbs the per-platform ExitStack-shaped lifecycle. Azure overrides to attach/detach a transient
  public IP. Replaces the old `isinstance(prov, AzureProvisioner)` branches across `shell_vm`,
  `rekey_vm`, `_tailscale_logout`, and `_ensure_tailscale`.
- `VMProvisioner.post_tailscale_ready(vm)` (default no-op) absorbs the asynchronous post-bootstrap
  cleanup hook used by `create_vm._on_tailscale_ready`. Azure overrides to detach the cloud-init
  public IP at the Tailscale-ready moment.
- `_unwrap_ssh` shim deleted; `vms/backup.py` consumes the polymorphic `Transport.copy_from`.
- Operator-visible behavior preserved per R6, with the carve-outs the FRD enumerated: misleading
  error text improved (e.g. `SSHError("Lima command failed...")` raises from the Lima transport
  now), the in-`exec_target_for_user` assert promoted to a typed `StateError` at the factory entry
  point.
- Per-transport contract tests under `cli/tests/transports/` (76 tests across `test_abc.py`,
  `test_ssh.py`, `test_lima.py`, `test_remote_lima.py`, `test_wsl2.py`, `test_factories.py`). R7.

`cli/agentworks/ssh.py` shrank from 1004 lines to 343 lines (-66%): `ExecTarget`,
`admin_exec_target`, `agent_exec_target`, `exec_target_for_user`, `_unwrap_ssh`, per-transport
helpers (`lima_run`, `remote_lima_run`, `wsl2_run`, `_lima_interactive`, ...), and the legacy
`wait_for_reconnect` are gone. The module retains `SSHTarget` / `SSHResult` / `SSHError` /
`SSHLogger` / `LOG_DIR` plus module-level `run` / `copy_to` for the two remaining bare-`SSHTarget`
callers (`vm_hosts/manager.py`, `vms/provisioners/lima.py`'s host control plane).

These specs are accurate as of this date but are now locked and will not be updated to reflect
further changes to the implementation.

## Follow-up

The Phase 3 reviewer flagged a class of `isinstance` narrows in `vms/initializer.py` that the
refactor cleared at the operation boundary (`Transport.describe()`, `Transport.logger`, polymorphic
`copy_dir_to` / `write_file`). The `/simplify` cleanup pass closed those during the same PR window;
no follow-up issue.

Two small post-merge fixes shipped alongside the refactor:

- PR #131: operation-level exception tracebacks now land in the per-op `SSHLogger` log instead of
  the shared `~/.config/agentworks/logs/error.log`. `SSHLogger.close()` introspects `sys.exc_info()`
  when called from an `except` block. Pre-existing behavior; not refactor related.
- PR #132: agent's `~/.agentworks-rc.sh` is now written unconditionally during agent setup, matching
  the admin pattern. Closes a hole where default-config agents (no `mise_packages`, no
  `mise_lockfile`) hit "No such file or directory" on every interactive login because the defensive
  source-line write outpaced the conditional file write. Pre-existing; first surfaced while testing
  the refactor on Windows.
