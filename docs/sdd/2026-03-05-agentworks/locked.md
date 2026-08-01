# Locked: 2026-03-28

## Summary

The core agentworks SDD is complete. All planned phases have been implemented or explicitly
deferred.

### Completed

- **Phase 1**: VM workspaces and core CLI (provisioning, initialization, lifecycle, git credentials,
  workspace templates, shell completions, init resilience and logging)
- **Phase 2**: Local workspaces (delivered in Phase 1)
- **Phase 4**: Agents (database schema, lifecycle, tmuxinator integration, CLI commands)
- **Future: VM templates**: Implemented as `[vm_templates.*]` with inheritance, replacing the
  original `[vm.config]` section
- **Future: Workspace move**: Superseded by `workspace copy`

### Deferred

- **Phase 3: File templating**: Deferred indefinitely. Rulesync and dotfiles cover the primary use
  cases for now.

### Remaining future items (not planned)

- VM initialization plugins
- Agent install commands / agent templates
- Non-VM workspace hosts (Kubernetes, containers)
- Azure auto-suspend
- Auto-authentication

### Manual testing gaps

- E2E testing items in 1.13 and 4.6 are unchecked (manual, not code)

### Related SDDs

Work that continued from this SDD in separate feature directories:

- `2026-03-08-nerfed-commands` - nerfed agent commands
- `2026-03-08-user-based-security` - user isolation and RBAC
- `2026-03-15-install-enhancements` - installer catalog system
- `2026-03-16-resilient-provisioning` - provisioning error handling
- `2026-03-17-nerf-tools` - nerftools CLI
- `2026-03-23-tasks` - task management
- `2026-03-26-mise-integration` - mise tool manager, source refs, config restructuring

## Revision: 2026-06-04 (WSL2 path rehabilitation)

The WSL2 platform had drifted into a non-working state. Bringing it back required revising the
behavior described in `vm-provisioning-lld.md`. The LLD itself remains locked; the canonical
behavior is described in code (`bootstrap_script.py`, `provisioners/wsl2.py`) and summarized here.

### Superseded prose in `vm-provisioning-lld.md`

- **Lines 274-277 ("WSL2 and Tailscale" paragraph)** and **line 318 (Phase A step 7 note)** and
  **line 403 (rejoin step e note)** all describe `tailscale up --userspace-networking` as the WSL2
  path. That flag is invalid - `--userspace-networking` is a `tailscaled` daemon flag, not a
  `tailscale` client flag, and the original code never worked. WSL2 now runs in standard kernel-tun
  mode like every other platform. Modern WSL2 kernels (5.10+) expose `/dev/net/tun`; the daemon's
  package defaults are correct as-is.

  If a future WSL2 kernel ever lacks tun, the documented recovery path is a systemd drop-in at
  `/etc/systemd/system/tailscaled.service.d/10-userspace.conf` setting
  `Environment="FLAGS=--tun=userspace-networking"`. The bootstrap script comment records this recipe
  in-line and explicitly warns against overwriting `/etc/default/tailscaled` (which would clobber
  `PORT` and prevent tailscaled from starting). See `cli/agentworks/vms/bootstrap_script.py` Step 6
  comment.

- **Phase A** for WSL2 no longer matches the generic "platform-native transport" description
  literally. The bootstrap is run synchronously via a single
  `wsl --user agentworks -- bash -lc 'setsid sudo -n /bin/bash {script} </dev/null 2>&1'` call.
  Reasons (each individually load-bearing): (a) `wsl.exe` is a local subprocess with no disconnect
  risk, so the `run_detached` nohup-poll pattern used for Lima/Azure is unnecessary and actively
  broken under WSL2+systemd (KillUserProcesses reaps the backgrounded process); (b) `setsid` strips
  the controlling TTY so sudo's `use_pty` default doesn't SIGTTIN-stop apt mid- `dist-upgrade`; (c)
  `</dev/null` defends against any read-from-stdin in maintainer scripts.

- The WSL2 provisioner now strips `/usr/sbin/policy-rc.d` from the imported Docker rootfs before any
  apt-get runs. The Debian Docker base ships this file (returning 101) to refuse service starts
  during image build; without removing it, apt-installed services like `tailscaled` would never
  start.

- Path resolution: `%LOCALAPPDATA%` is now resolved in Python before any PowerShell or `wsl.exe`
  invocation (PowerShell does not expand `%VAR%` env-var syntax; the literal string was being passed
  through to `wsl --import`, creating mis-rooted install directories). See `_local_app_data`,
  `_wsl_base_path`, `_cache_dir`, `_ps_quote` in `provisioners/wsl2.py`.

### New: `VMProvisioner.vm_active(vm, *, config) -> AbstractContextManager[None]`

Adds a provisioner-level hook for "hold this VM in an active, reachable state for the duration of a
context". Base default is `nullcontext()`. WSL2 overrides to spawn
`wsl --distribution NAME -- sleep infinity` as a background subprocess (anchoring against
`vmIdleTimeout`), bound to a Win32 Job Object with `KILL_ON_JOB_CLOSE` so a hard-killed CLI cannot
leak an orphan anchor, and -- if the VM has already joined Tailscale -- wait for SSH reachability
before yielding. Every manager-layer function that touches a VM wraps in this context via the
`keep_vm_active` / `keep_vms_active` helpers in `vms/manager.py`; the deliberate exceptions
(`stop_vm`, the `describe_*` family, multi_console best-effort ops) are documented at the
`vm_active` docstring. This is consistent with the two-phase lifecycle (Phase B still runs over
Tailscale SSH); the keepalive just prevents WSL2 from idle-shutting the distro while a command is
mid-flight.

> **Superseded by PR #271 (2026-07-26):** `vm_active` is now a pure power-hold on every platform.
> The WSL2 override no longer waits for SSH reachability before yielding (the "wait for SSH
> reachability before yielding" clause above is retired), and `config` is no longer used for any
> reconnect wait. Connectivity verification is handled uniformly by the shared paths
> (`_ensure_tailscale`, the activation gate), which run inside the hold. The subprocess anchor and
> stopped-distro boot are unchanged. See `capabilities/vm_platform/wsl2.py::_keepalive`.

### Forward-looking note

The symmetric "Azure auto-deallocate when idle" feature listed under "Remaining future items" maps
to the same activity-counter primitive that the WSL2 keepalive hints at. A future design document
will treat both as instances of "per-VM activity tracking + platform-specific lifecycle reaction."

## Revision: 2026-07-31 (Azure public IP kept for life)

`vm-provisioning-lld.md`'s statement that the Azure public IP "can optionally be removed after
Tailscale is up" (and the detach mechanism later built on it) is superseded. Driver: Microsoft is
retiring default outbound access, which made VMs with a detached public IP go offline. Azure VMs now
keep their public IP for the VM's whole lifetime; the NSG carries a permanent deny-all-inbound
baseline, and SSH ingress happens only through an ephemeral allow rule scoped to the operator's
egress IP, opened for bootstrap and for each native route and deleted after. Living reference:
`cli/agentworks/plugins/azure/platform.py`, `cli/agentworks/plugins/azure/network.py`, and ADR 0003.
