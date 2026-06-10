"""VM lifecycle management -- create, list, start, stop, delete."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import VALID_PLATFORMS, validate_admin_username, validate_name
from agentworks.db import InitStatus, ProvisioningStatus, VMStatus
from agentworks.errors import (
    AlreadyExistsError,
    ConfigError,
    ConnectivityError,
    ExternalError,
    NotFoundError,
    ProvisionerError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.vms.initializer import (
    initialize_vm,
    rejoin_tailscale,
    resolve_git_credential_providers,
    run_initialization,
    verify_git_credential_auth,
    verify_tailscale_available,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.git_credentials.base import GitCredentialProvider
    from agentworks.vms.base import VMProvisioner


@contextlib.contextmanager
def keep_vm_active(db: Database, config: Config, vm: VMRow) -> Iterator[None]:
    """Hold a VM in an active, reachable state for the duration of the context.

    Dispatches to the platform provisioner's ``vm_active`` hook. Default is a
    no-op (Lima/Azure/Proxmox VMs don't disappear on us). WSL2 spawns a
    ``wsl --distribution NAME -- sleep infinity`` subprocess to anchor the
    distro against ``vmIdleTimeout``, booting it if stopped, and waits for
    Tailscale SSH to be reachable before yielding.

    Wrap any manager-layer function that touches the VM (issues SSH, runs
    tmux ops, transfers files, etc.) in this context. For commands that
    touch more than one VM, use :func:`keep_vms_active`.
    """
    provisioner = get_provisioner_for_vm(db, vm, config)
    with provisioner.vm_active(vm, config=config):
        yield


@contextlib.contextmanager
def keep_vms_active(
    db: Database, config: Config, vms: Iterable[VMRow]
) -> Iterator[None]:
    """Multi-VM variant of :func:`keep_vm_active`.

    Enters one ``vm_active`` per VM via ``ExitStack`` so a command that
    touches multiple VMs (``session list --status``, ``workspace copy``
    across hosts) keeps all of them anchored for its duration. Duplicate
    VMs are deduplicated by name.
    """
    seen: set[str] = set()
    with contextlib.ExitStack() as stack:
        for vm in vms:
            if vm.name in seen:
                continue
            seen.add(vm.name)
            stack.enter_context(keep_vm_active(db, config, vm))
        yield


def get_provisioner(platform: str, vm_host_ssh: str | None = None) -> VMProvisioner:
    """Get the appropriate provisioner for a platform."""
    if platform == "lima":
        from agentworks.vms.provisioners.lima import LimaProvisioner

        return LimaProvisioner(vm_host_ssh=vm_host_ssh)
    elif platform == "azure":
        from agentworks.vms.provisioners.azure import AzureProvisioner

        return AzureProvisioner()
    elif platform == "wsl2":
        from agentworks.vms.provisioners.wsl2 import WSL2Provisioner

        return WSL2Provisioner()
    elif platform == "proxmox":

        # ProxmoxProvisioner requires config; caller must use create_vm flow
        raise ValueError("Use create_vm for proxmox provisioning")
    else:
        msg = f"Unknown platform: {platform}"
        raise ValueError(msg)


def create_vm(
    db: Database,
    config: Config,
    *,
    name: str,
    template: str | None = None,
    platform: str | None = None,
    vm_host: str | None = None,
    cpus: int | None = None,
    memory: int | None = None,
    disk: int | None = None,
    azure_vm_size: str | None = None,
    admin_username: str | None = None,
) -> None:
    """Create a new VM: provision + initialize."""
    from dataclasses import replace as _replace

    from agentworks.vms.templates import resolve_template

    vm_tmpl = resolve_template(config, template)

    # Replace config.vm with the resolved template so downstream code
    # (initializer, provisioners) uses the right template values.
    if template is not None:
        config = _replace(config, vm=vm_tmpl)

    # Resolve defaults
    platform = platform or config.defaults.platform or "lima"
    if platform not in VALID_PLATFORMS:
        raise ValidationError(f"invalid platform '{platform}'", entity_kind="vm")

    vm_name = name
    validate_name(vm_name)

    if db.get_vm(vm_name) is not None:
        raise AlreadyExistsError(
            f"VM '{vm_name}' already exists",
            entity_kind="vm",
            entity_name=vm_name,
        )

    # Resolve VM host for Lima
    vm_host_ssh: str | None = None
    vm_host_name: str | None = None
    if platform == "lima":
        vm_host_name = vm_host or config.defaults.vm_host
        if vm_host_name:
            host_row = db.get_vm_host(vm_host_name)
            if host_row is None:
                raise NotFoundError(
                    f"VM host '{vm_host_name}' not found",
                    entity_kind="vm-host",
                    entity_name=vm_host_name,
                )
            vm_host_ssh = host_row.ssh_host

    # Azure config validation
    if platform == "azure" and config.azure is None:
        raise ConfigError("[azure] config section required for azure platform")

    # Proxmox config validation
    if platform == "proxmox" and config.proxmox is None:
        raise ConfigError("[proxmox] config section required for proxmox platform")

    # Resolve resource settings: CLI flag > template > built-in default
    resolved_cpus = cpus if cpus is not None else vm_tmpl.cpus
    resolved_memory = memory if memory is not None else vm_tmpl.memory
    resolved_disk = disk if disk is not None else vm_tmpl.disk
    resolved_azure_size = azure_vm_size or vm_tmpl.azure_vm_size
    resolved_admin_username = admin_username or config.admin.username
    validate_admin_username(resolved_admin_username)

    # Pre-flight checks
    verify_tailscale_available()
    providers = resolve_git_credential_providers(config, config.admin.git_credentials)
    verify_git_credential_auth(providers)

    # Collect secrets upfront so the user isn't interrupted mid-provisioning
    tailscale_auth_key, git_tokens = _collect_secrets(providers, vm_name)

    # Create DB record with as-provisioned resource values
    db.insert_vm(
        vm_name,
        platform=platform,
        vm_host_name=vm_host_name,
        template=vm_tmpl.name,
        cpus=resolved_cpus,
        memory_gib=resolved_memory,
        disk_gib=resolved_disk,
        swap_gib=vm_tmpl.swap,
        admin_username=resolved_admin_username,
    )

    # -- Provisioning --
    # If this fails, nothing was created on the remote host (or the remote
    # couldn't be reached), so we clean up the DB record.
    def _safe_delete_vm_row() -> None:
        # Best-effort rollback: a DB error here (e.g. lock contention) must
        # not mask the KeyboardInterrupt / provisioning exception that
        # triggered the rollback.
        try:
            db.delete_vm(vm_name)
        except Exception as cleanup_err:
            output.warn(
                f"rollback: failed to delete DB record for vm '{vm_name}': {cleanup_err}"
            )

    try:
        if platform == "lima":
            from agentworks.vms.provisioners.lima import LimaProvisioner

            lima = LimaProvisioner(vm_host_ssh=vm_host_ssh)
            result = lima.create(
                vm_name,
                config,
                cpus=resolved_cpus,
                memory=resolved_memory,
                disk=resolved_disk,
                tailscale_auth_key=tailscale_auth_key,
            )
        elif platform == "azure":
            from agentworks.vms.provisioners.azure import AzureProvisioner

            azure = AzureProvisioner()
            result = azure.create(
                vm_name,
                config,
                azure_vm_size=resolved_azure_size,
                disk=resolved_disk,
                admin_username=resolved_admin_username,
                tailscale_auth_key=tailscale_auth_key,
            )
        elif platform == "wsl2":
            from agentworks.vms.provisioners.wsl2 import WSL2Provisioner

            wsl2 = WSL2Provisioner()
            result = wsl2.create(
                vm_name,
                config,
                admin_username=resolved_admin_username,
            )
        elif platform == "proxmox":
            from agentworks.vms.provisioners.proxmox import ProxmoxProvisioner

            proxmox = ProxmoxProvisioner(config.proxmox)  # type: ignore[arg-type]
            result = proxmox.create(
                vm_name,
                config,
                cpus=resolved_cpus,
                memory=resolved_memory,
                disk=resolved_disk,
                admin_username=resolved_admin_username,
                tailscale_auth_key=tailscale_auth_key,
            )
        else:
            msg = f"Unknown platform: {platform}"
            raise ValueError(msg)
    except KeyboardInterrupt:
        output.warn(f"Cancelling vm create '{vm_name}'... rolling back.")
        _safe_delete_vm_row()
        raise
    except Exception as e:
        _safe_delete_vm_row()
        raise ProvisionerError(
            f"provisioning failed: {e}",
            entity_kind="vm",
            entity_name=vm_name,
        ) from e

    # Update DB with platform-specific metadata
    if result.azure_resource_id:
        db.update_vm_azure_resource_id(vm_name, result.azure_resource_id)
    if result.wsl_distro_name:
        db.update_vm_wsl_distro_name(vm_name, result.wsl_distro_name)
    if result.proxmox_vmid:
        db.update_vm_proxmox_vmid(vm_name, result.proxmox_vmid)

    # -- Initialization --
    # If this fails, the VM exists on the remote host and may be debuggable.
    # Keep the DB record so the user can reinit or delete.
    # Build a callback to detach the Azure public IP once Tailscale is up
    # (before Phase B starts). This minimizes the window where the VM has
    # a public IP exposed to the internet.
    def _on_tailscale_ready() -> None:
        if platform == "azure":
            from agentworks.vms.provisioners.azure import AzureProvisioner as _AP

            _created_vm = db.get_vm(vm_name)
            assert _created_vm is not None
            _AP().detach_public_ip(_created_vm)

    try:
        initialize_vm(
            db,
            config,
            vm_name,
            exec_target=result.admin_exec_target,
            providers=providers,
            admin_username=resolved_admin_username,
            tailscale_auth_key=tailscale_auth_key,
            git_tokens=git_tokens,
            bootstrap_complete=result.bootstrap_complete,
            tailscale_ip=result.tailscale_ip,
            on_tailscale_ready=_on_tailscale_ready,
        )
    except KeyboardInterrupt:
        output.warn(
            f"Cancelling vm create '{vm_name}' during initialization. "
            f"The VM exists but is partially initialized. "
            f"Use 'vm reinit {vm_name}' to retry, or 'vm delete {vm_name} --force' to remove it."
        )
        raise
    except Exception as e:
        from agentworks.ssh import LOG_DIR

        log_hint = ""
        logs = sorted(LOG_DIR.glob(f"{vm_name}-*-vm-create.log"), reverse=True)
        if logs:
            log_hint = f"\nDetails: {logs[0]}"

        vm = db.get_vm(vm_name)
        if vm is not None and vm.provisioning_status == ProvisioningStatus.FAILED.value:
            raise ProvisionerError(
                f"provisioning failed: {e}{log_hint}",
                entity_kind="vm",
                entity_name=vm_name,
                hint=f"VM '{vm_name}' is in a failed state. Use 'vm delete {vm_name}' to clean up.",
            ) from e
        else:
            raise ExternalError(
                f"initialization failed: {e}{log_hint}",
                entity_kind="vm",
                entity_name=vm_name,
                hint=f"VM '{vm_name}' may still be usable. Use 'vm reinit {vm_name}' to retry.",
            ) from e

    # -- Post-init: SSH config --
    try:
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db)
    except Exception as e:
        output.warn(f"SSH config sync failed: {e}")
        output.detail("VM is likely still usable.")

    # Final status is set by initialize_vm (COMPLETE or PARTIAL)
    vm = db.get_vm(vm_name)
    assert vm is not None
    if vm.init_status == InitStatus.PARTIAL.value:
        output.info(f"VM '{vm_name}' is ready (with warnings -- see above)")
    else:
        output.info(f"VM '{vm_name}' is ready!")


def list_vms(db: Database) -> None:
    """List all VMs with their init and runtime status."""
    vms = db.list_vms()
    if not vms:
        output.info("No VMs registered.")
        return

    header = (
        f"{'NAME':<20} {'PLATFORM':<10} {'TEMPLATE':<12} {'HOST':<15} {'PROV':<12} {'INIT':<12} "
        f"{'WS/AG/TS':<10} {'TAILSCALE':<20} {'CREATED'}"
    )
    output.info(header)
    output.info("-" * len(header))
    for vm in vms:
        ws = db.count_workspaces_on_vm(vm.name)
        ag = db.count_agents_on_vm(vm.name)
        ts = db.count_sessions_on_vm(vm.name)
        counts = f"{ws}/{ag}/{ts}"
        output.info(
            f"{vm.name:<20} {vm.platform:<10} {vm.template or '-':<12} {vm.vm_host_name or '-':<15} "
            f"{vm.provisioning_status:<12} {vm.init_status:<12} "
            f"{counts:<10} {vm.tailscale_host or '-':<20} {vm.created_at}"
        )


def describe_vm(db: Database, config: Config, name: str) -> None:
    """Show detailed information about a VM."""
    vm = _require_vm(db, name)

    # VM details
    output.info(f"Name:           {vm.name}")
    output.info(f"Created:        {vm.created_at}")
    output.info(f"Platform:       {vm.platform}")
    output.info(f"Template:       {vm.template or '-'}")
    output.info(f"VM Host:        {vm.vm_host_name or '-'}")
    output.info(f"Admin User:     {vm.admin_username}")
    output.info(f"Provisioning:   {vm.provisioning_status}")
    output.info(f"Initialization: {vm.init_status}")
    output.info(f"Tailscale IP:   {vm.tailscale_host or '-'}")

    # Resources table: Initial / Current / Used (Used%)
    live = None
    if vm.tailscale_host is not None:
        live = _query_live_resources(vm, config)

    if vm.cpus is not None or live is not None:
        output.info(f"\n{'Resources':<16}{'Provisioned':<14}{'Current':<14}{'Used'}")
        output.detail(
            f"{'CPU':<16}"
            f"{str(vm.cpus) if vm.cpus else '-':<14}"
            f"{live['cpus'] if live else '-':<14}"
            f"{'load ' + live['load_avg'] if live else '-'}"
        )
        output.detail(
            f"{'Memory':<16}"
            f"{str(vm.memory_gib) + 'G' if vm.memory_gib else '-':<14}"
            f"{live['mem_total'] if live else '-':<14}"
            f"{live['mem_used'] + ' (' + live['mem_pct'] + ')' if live else '-'}"
        )
        output.detail(
            f"{'Swap':<16}"
            f"{str(vm.swap_gib) + 'G' if vm.swap_gib else '-':<14}"
            f"{live['swap_total'] if live else '-':<14}"
            f"{live['swap_used'] + ' (' + live['swap_pct'] + ')' if live else '-'}"
        )
        output.detail(
            f"{'Disk':<16}"
            f"{str(vm.disk_gib) + 'G' if vm.disk_gib else '-':<14}"
            f"{live['disk_total'] if live else '-':<14}"
            f"{live['disk_used'] + ' (' + live['disk_pct'] + ')' if live else '-'}"
        )

    if vm.azure_resource_id:
        output.info(f"Azure ID:       {vm.azure_resource_id}")
    if vm.wsl_distro_name:
        output.info(f"WSL Distro:     {vm.wsl_distro_name}")
    if vm.proxmox_vmid:
        output.info(f"Proxmox VMID:   {vm.proxmox_vmid}")
    if vm.last_seen_at:
        output.info(f"Last Seen:      {vm.last_seen_at}")

    # Agents on this VM
    agents = db.list_agents(vm_name=name)
    output.info(f"\nAgents ({len(agents)}):")
    if agents:
        for agent in agents:
            grant_count = db.count_agent_grants(agent.name)
            grant_label = "all" if agent.grant_all else str(grant_count)
            output.detail(f"{agent.name}  (user: {agent.linux_user}, grants: {grant_label})")
    else:
        output.detail("(none)")

    # Workspaces with sessions
    workspaces = db.list_workspaces(vm_name=name)
    output.info(f"\nWorkspaces ({len(workspaces)}):")
    if workspaces:
        for ws in workspaces:
            output.detail(f"{ws.name}  ({ws.workspace_path})")

            sessions = db.list_sessions(workspace_name=ws.name)
            if sessions:
                output.detail(f"Sessions ({len(sessions)}):", indent=2)
                for s in sessions:
                    mode_label = f"agent:{s.agent_name}" if s.agent_name else "admin"
                    output.detail(f"{s.name}  [{s.template}]  {mode_label}", indent=3)
            else:
                output.detail("(no sessions)", indent=2)
    else:
        output.detail("(none)")

    # Events
    events = db.list_vm_events(name)
    output.info(f"\nEvents ({len(events)}):")
    if events:
        for event in events:
            evt_detail = f"  {event.detail}" if event.detail else ""
            output.detail(f"{event.created_at}  {event.event}{evt_detail}")
    else:
        output.detail("(none)")


def shell_vm(db: Database, config: Config, name: str) -> None:
    """Open a shell on a VM's home directory."""
    import subprocess
    import sys

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    ssh_cmd = ["ssh", "-t"]
    if config.operator.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.operator.ssh_private_key)])
    ssh_cmd.append(f"{vm.admin_username}@{vm.tailscale_host}")

    with keep_vm_active(db, config, vm):
        sys.exit(subprocess.call(ssh_cmd))


def exec_vm(db: Database, config: Config, name: str, command: list[str]) -> int:
    """Execute a command on a VM via direct admin SSH.

    Uses inherited stdio for streaming output without buffering.
    Returns the remote exit code.
    """
    import shlex

    from agentworks.ssh import admin_exec_target

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    # admin_exec_target asserts tailscale_host is not None; guard first so
    # the operator gets an actionable StateError instead of an AssertionError
    # (which also disappears under python -O).
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )
    target = admin_exec_target(vm, config)

    remote_cmd = command[0] if len(command) == 1 else shlex.join(command)
    with keep_vm_active(db, config, vm):
        return target.call_streaming(remote_cmd)


def add_git_credential(db: Database, config: Config, name: str, credential_name: str) -> None:
    """Add or update a git credential on a VM."""
    from agentworks.ssh import admin_exec_target

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    cred_config = config.git_credentials.get(credential_name)
    if cred_config is None:
        raise NotFoundError(
            f"git credential '{credential_name}' not found in config",
            entity_kind="git-credential",
            entity_name=credential_name,
        )

    providers = resolve_git_credential_providers(config, [credential_name])
    provider = providers[credential_name]

    token = provider.obtain_token(name)
    new_lines = provider.credential_lines(token)

    with keep_vm_active(db, config, vm):
        target = admin_exec_target(vm, config)

        # Read existing credentials, filter out entries for the same host/path
        result = target.run("cat ~/.git-credentials 2>/dev/null || true")
        existing = result.stdout.strip().splitlines() if result.stdout.strip() else []

        # Extract host/path from new lines for matching: "https://user:tok@host/path" -> "host/path"
        new_hostpaths = {line.split("@", 1)[1] for line in new_lines if "@" in line}

        # Filter out old entries whose host/path matches any new entry
        filtered = [e for e in existing if "@" not in e or e.split("@", 1)[1] not in new_hostpaths]

        # Write back filtered + new
        all_lines = filtered + new_lines
        cred_content = "\n".join(all_lines) + "\n"
        target.write_file("~/.git-credentials", cred_content, mode="600")
        target.run("git config --global credential.helper store")

    output.info(f"Git credential '{credential_name}' configured on VM '{name}'")


def start_vm(db: Database, config: Config, name: str) -> None:
    """Start a stopped VM."""
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    provisioner = get_provisioner_for_vm(db, vm)
    # Probe status and issue the start BEFORE entering keep_vm_active: the
    # WSL2 keepalive subprocess boots a stopped distro as a side effect,
    # which would make status() report RUNNING and mislabel the VM as
    # "already running". The keepalive then anchors the (now running) VM
    # through the Tailscale verification.
    status = provisioner.status(vm)
    if status == VMStatus.RUNNING:
        output.info(f"VM '{name}' is already running")
    else:
        provisioner.start(vm)

    # Tailscale verification runs inside the keepalive so a freshly booted
    # WSL2 distro doesn't idle-shut while we wait for tailscaled to come up.
    with keep_vm_active(db, config, vm):
        _ensure_tailscale(db, config, vm, provisioner)
    # Only emit "is ready" on the path that actually started the VM. When
    # status was already RUNNING we already said so above, and Tailscale
    # verification is usually a no-op (handshake already valid), so an
    # extra "is ready" line is just noise. On the real-work path it
    # confirms tailscaled finished its handshake.
    if status != VMStatus.RUNNING:
        output.info(f"VM '{name}' is ready")


def stop_vm(db: Database, config: Config, name: str) -> None:
    """Stop a running VM."""
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    provisioner = get_provisioner_for_vm(db, vm)
    status = provisioner.status(vm)
    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        output.info(f"VM '{name}' is already stopped")
        return
    # No keep_vm_active here: stop is the inverse of what the keepalive
    # is for. The platform stop call doesn't need SSH to the VM, and
    # holding a wsl.exe sleep subprocess open would fight `wsl --terminate`.
    provisioner.stop(vm)
    output.info(f"VM '{name}' stopped")


def rekey_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    wait_for_share: bool = False,
    ignore_env: bool = False,
) -> None:
    """Assign a new Tailscale auth key to a VM (logout + rejoin).

    Useful for rotating keys, switching tailnets, or recovering from
    expired ephemeral keys. Uses the provisioner's admin_exec_target
    (out-of-band transport) since Tailscale connectivity drops during
    the operation.
    """
    import ipaddress
    import os
    import shlex
    import time

    from agentworks.ssh import SSHError, admin_exec_target, wait_for_reconnect
    from agentworks.ssh_config import sync_ssh_config
    from agentworks.vms.provisioners.azure import AzureProvisioner

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)

    provisioner = get_provisioner_for_vm(db, vm, config)
    status = provisioner.status(vm)
    if status != VMStatus.RUNNING:
        raise StateError(
            f"VM '{name}' is not running (status: {status.value})",
            entity_kind="vm",
            entity_name=name,
        )

    # Collect new auth key
    ts_auth_key = os.environ.get("TAILSCALE_AUTH_KEY") if not ignore_env else None
    if ts_auth_key:
        output.detail("Tailscale auth key found in environment")
    else:
        ts_auth_key = output.prompt_secret(
            "Tailscale auth key",
            hint="Generate a key at https://login.tailscale.com/admin/settings/keys",
        )

    output.info(f"Rekeying '{name}'...")

    azure_provisioner = provisioner if isinstance(provisioner, AzureProvisioner) else None

    with contextlib.ExitStack() as _stack:
        # Holds the VM in an active state for the duration of the rekey.
        # No-op for Lima/Azure/Proxmox; WSL2 anchors the distro against
        # vmIdleTimeout so per-step `time.sleep`s can't let it idle out.
        _stack.enter_context(keep_vm_active(db, config, vm))
        # For Azure, attach a temporary public IP for out-of-band access;
        # detach on exit no matter how the body unwinds.
        if azure_provisioner is not None:
            azure_provisioner.attach_public_ip(vm)
            _stack.callback(azure_provisioner.detach_public_ip, vm)

        exec_target = provisioner.admin_exec_target(vm, config=config)

        # Wait for the provisioning transport to be reachable
        output.detail("Waiting for provisioning transport...")
        for attempt in range(6):
            try:
                exec_target.run("echo ok", timeout=10)
                break
            except SSHError:
                if attempt == 5:
                    raise
                output.detail(f"Attempt {attempt + 1} failed, retrying...")
                time.sleep(5)
        output.detail("Connected.")

        # Restart, logout, login, restart. The initial restart clears any
        # stale daemon state (a previous interrupted rekey can leave the
        # daemon in a state where `tailscale logout` hangs waiting for a
        # control plane response that never comes). The final restart
        # fixes a Tailscale bug where the node registers but peers can't
        # reach it after rekeying to a different tailnet.
        # All platforms run systemd (WSL2 enables it via /etc/wsl.conf during
        # provisioning); tailscaled is always a systemd unit. Daemon-side
        # flags like --tun=userspace-networking live in /etc/default/tailscaled,
        # not on `tailscale up`.
        restart_cmd = "systemctl restart tailscaled"
        stabilize_secs = 15  # pause between steps for daemon/network stability

        output.detail("Restarting Tailscale daemon...")
        exec_target.run(restart_cmd, sudo=True, timeout=15)
        time.sleep(stabilize_secs)

        output.detail("Logging out of current tailnet...")
        exec_target.run("tailscale logout", sudo=True, timeout=30)
        time.sleep(stabilize_secs)

        output.detail("Joining new tailnet...")
        quoted_key = shlex.quote(ts_auth_key)
        exec_target.run(f"tailscale up --auth-key {quoted_key}", sudo=True, timeout=30)
        time.sleep(stabilize_secs)

        output.detail("Restarting Tailscale daemon...")
        exec_target.run(restart_cmd, sudo=True, timeout=15)
        time.sleep(stabilize_secs)

        output.detail("Reading new Tailscale IP...")
        result = exec_target.run("tailscale ip -4", sudo=True, timeout=15)
        raw_ip = result.stdout.strip()
        new_ip = raw_ip.splitlines()[0].strip() if raw_ip else ""
        try:
            ipaddress.IPv4Address(new_ip)
        except ValueError:
            raise SSHError(
                f"tailscale ip -4 returned invalid address: {new_ip!r}\nfull output: {raw_ip}"
            ) from None
        output.detail(f"Tailscale IP: {new_ip}")

        # Update DB and SSH config with the new IP (correct regardless of
        # reachability -- the old IP is definitely dead after logout)
        db.update_vm_tailscale(name, new_ip)
        sync_ssh_config(config, db)
        db.insert_vm_event(name, "rekey", f"new_ip={new_ip}")

        # If the operator needs to share the VM back, pause before connectivity check
        if wait_for_share:
            output.pause(
                "Share the VM back to your tailnet, then press Enter to verify connectivity..."
            )

        # Always verify Tailscale SSH connectivity to the new IP
        output.detail(f"Verifying SSH to {new_ip}...")
        from dataclasses import replace

        ts_target = admin_exec_target(vm, config)
        assert ts_target.ssh is not None
        ts_target = replace(ts_target, ssh=replace(ts_target.ssh, host=new_ip))
        if wait_for_reconnect(ts_target):
            output.info(f"VM '{name}' rekeyed successfully. Tailscale IP: {new_ip}")
        else:
            output.warn(
                f"VM '{name}' rekeyed but {new_ip} is not reachable via SSH. "
                "Check tailnet sharing/ACLs. Run 'vm rekey' again to retry."
            )


def delete_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Delete a VM, cleaning up all associated resources."""
    vm = _require_vm(db, name)

    # Check for workspaces (which contain agents and sessions)
    ws_count = db.count_workspaces_on_vm(name)
    ag_count = db.count_agents_on_vm(name)
    ts_count = db.count_sessions_on_vm(name)
    has_children = ws_count > 0

    if has_children and not force:
        parts = [f"{ws_count} workspace(s)"]
        if ag_count > 0:
            parts.append(f"{ag_count} agent(s)")
        if ts_count > 0:
            parts.append(f"{ts_count} session(s)")
        raise StateError(
            f"VM '{name}' has {', '.join(parts)}.",
            entity_kind="vm",
            entity_name=name,
            hint="Delete them first, or pass --force to also delete the children.",
        )

    if not yes and not force:
        msg = f"Delete VM '{name}'?"
        if has_children:
            parts = [f"{ws_count} workspace(s)"]
            if ag_count > 0:
                parts.append(f"{ag_count} agent(s)")
            if ts_count > 0:
                parts.append(f"{ts_count} session(s)")
            msg += f" ({', '.join(parts)} will also be deleted)"
        if not output.confirm(msg):
            raise UserAbort("delete cancelled")

    # Platform-specific cleanup (also handles Tailscale logout)
    try:
        provisioner = get_provisioner_for_vm(db, vm)

        # Tailscale logout (best-effort, via provisioning transport).
        # Wrap only this step: the logout needs the VM alive, but
        # provisioner.delete is the inverse and would conflict with a
        # WSL2 keepalive subprocess.
        if vm.tailscale_host:
            with keep_vm_active(db, config, vm):
                _tailscale_logout(provisioner, vm, config)

        provisioner.delete(vm)
    except Exception as e:
        output.warn(f"platform cleanup failed: {e}")

    # Clean up logs
    from agentworks.ssh import LOG_DIR

    vm_logs = list(LOG_DIR.glob(f"{name}-*.log")) if LOG_DIR.exists() else []
    for log in vm_logs:
        log.unlink(missing_ok=True)
    if vm_logs:
        output.info(f"Cleaned up {len(vm_logs)} log(s)")

    # Remove from DB (cascades workspaces and agents), then rebuild SSH config
    db.delete_vm(name)

    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)
    output.info(f"VM '{name}' deleted")


def reinit_vm(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Re-run initialization on a VM that has already been provisioned.

    Requires provisioning_status == complete and a valid Tailscale connection.
    """
    from agentworks.ssh import admin_exec_target

    vm = _require_vm(db, name)

    # Resolve the VM's template so init uses the right values
    if vm.template and vm.template != "default":
        from dataclasses import replace as _replace

        from agentworks.vms.templates import resolve_template

        config = _replace(config, vm=resolve_template(config, vm.template))

    if vm.provisioning_status != ProvisioningStatus.COMPLETE.value:
        raise StateError(
            f"VM '{name}' provisioning is '{vm.provisioning_status}', not 'complete'. "
            f"Cannot reinitialize.",
            entity_kind="vm",
            entity_name=name,
        )

    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
        )

    # Pre-flight checks
    verify_tailscale_available()
    providers = resolve_git_credential_providers(config, config.admin.git_credentials)
    verify_git_credential_auth(providers)

    # Collect git tokens upfront
    git_tokens: dict[str, str] = {}
    for cred_name, provider in providers.items():
        git_tokens[cred_name] = provider.obtain_token(name)

    # Build Tailscale SSH target with logging
    from agentworks.ssh import SSHLogger

    logger = SSHLogger(name, "vm-reinit")
    for token in git_tokens.values():
        logger.add_redaction(token)
    ts_target = admin_exec_target(vm, config, default_timeout=60, logger=logger)

    home = f"/home/{vm.admin_username}"

    # Outer try/finally ensures the SSH logger is closed exactly once, AFTER
    # any warning output. Matches the pattern used by agent create / reinit
    # and workspace create / rehome.
    try:
        with keep_vm_active(db, config, vm):
            try:
                run_initialization(
                    db,
                    config,
                    name,
                    ts_target,
                    providers,
                    home,
                    vm.admin_username,
                    logger,
                    git_tokens=git_tokens,
                )
            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling vm reinit '{name}'. The VM may be in a partial state. "
                    f"Re-run 'vm reinit {name}' to retry. Log: {logger.path}"
                )
                raise
            except Exception:
                output.warn(f"Log: {logger.path}")
                raise
    finally:
        logger.close()

    refreshed_vm = db.get_vm(name)
    assert refreshed_vm is not None
    if refreshed_vm.init_status == InitStatus.PARTIAL.value:
        output.info(f"VM '{name}' reinitialized (with warnings, see above)")
        output.detail(f"Log: {logger.path}")
    else:
        output.info(f"VM '{name}' reinitialized successfully!")


def _tailscale_logout(provisioner: VMProvisioner, vm: VMRow, config: Config) -> None:
    """Best-effort: deregister from Tailscale via the provisioning transport.

    Uses the provisioner's admin_exec_target (not Tailscale SSH) because we
    can't ask Tailscale to tear itself down over the connection it provides.
    For Azure VMs, temporarily attaches a public IP for SSH access.
    Proxmox raises NotImplementedError (guest agent not yet wired in).
    """
    import time

    from agentworks.ssh import SSHError as _SSHError
    from agentworks.vms.provisioners.azure import AzureProvisioner

    output.info("Deregistering from Tailscale...")
    try:
        azure_provisioner = provisioner if isinstance(provisioner, AzureProvisioner) else None
        if azure_provisioner is not None:
            azure_provisioner.attach_public_ip(vm)
        exec_target = provisioner.admin_exec_target(vm, config=config)

        # Wait for SSH to be reachable (public IP may have just been attached)
        for attempt in range(6):
            try:
                exec_target.run("echo ok", timeout=10)
                break
            except (_SSHError, Exception):
                if attempt == 5:
                    raise
                time.sleep(5)

        # Fire and forget: tailscale down + logout can disrupt networking
        # on the VM, killing SSH-based transports before they get a response.
        # Lima/WSL2 use local transports and are unaffected, but the nohup
        # approach works universally.
        exec_target.run(
            "nohup sh -c 'tailscale down && tailscale logout' >/dev/null 2>&1 &",
            sudo=True,
            timeout=10,
        )
        output.info("Tailscale node deregistered")
    except Exception as e:
        output.warn(f"Tailscale logout failed (node may remain in admin console): {e}")


def _init_log_hint(vm_name: str) -> str:
    """Return a log hint suffix like ' See log: <path>' or empty string."""
    from agentworks.ssh import LOG_DIR

    if not LOG_DIR.exists():
        return ""
    logs = sorted(LOG_DIR.glob(f"{vm_name}-*.log"), reverse=True)
    return f" See log: {logs[0]}" if logs else ""


def _guard_failed_vm(vm: VMRow) -> None:
    """Block operations on VMs with failed provisioning or initialization."""
    if vm.provisioning_status == ProvisioningStatus.FAILED.value:
        raise StateError(
            f"VM '{vm.name}' has failed provisioning.{_init_log_hint(vm.name)}",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Only 'vm delete' is supported on a failed-provisioning VM.",
        )
    if vm.init_status == InitStatus.FAILED.value:
        raise StateError(
            f"VM '{vm.name}' has failed initialization.{_init_log_hint(vm.name)}",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Use 'vm reinit' to retry or 'vm delete' to remove.",
        )


def _collect_secrets(
    providers: dict[str, GitCredentialProvider],
    vm_name: str,
) -> tuple[str | None, dict[str, str]]:
    """Collect all secrets upfront before provisioning starts.

    Returns (tailscale_auth_key, git_tokens).
    """
    import os

    output.info("Collecting credentials...")

    # Tailscale
    ts_auth_key = os.environ.get("TAILSCALE_AUTH_KEY")
    if ts_auth_key:
        output.detail("Tailscale auth key found in environment")
    else:
        ts_auth_key = output.prompt_secret(
            "  Tailscale auth key",
            hint="Generate a key at https://login.tailscale.com/admin/settings/keys",
        )

    # Git credentials
    git_tokens: dict[str, str] = {}
    for name, provider in providers.items():
        token = provider.obtain_token(vm_name)
        git_tokens[name] = token

    return ts_auth_key, git_tokens


def _query_live_resources(vm: VMRow, config: Config) -> dict[str, str] | None:
    """Query live resource usage from a VM over SSH."""
    from agentworks.ssh import admin_exec_target, run

    target = admin_exec_target(vm, config)
    cmd = (
        "nproc && "
        "uptime | grep -oP 'load average: \\K[^,]+' && "
        "free -b | awk '/^Mem:/{print $2,$3} /^Swap:/{print $2,$3}' && "
        "df -h / | awk 'NR==2{print $2,$3,$5}'"
    )

    try:
        result = run(target, cmd, check=False, retries=3)
    except Exception:
        return None

    if not result.ok:
        return None

    lines = result.stdout.strip().splitlines()
    if len(lines) < 5:
        return None

    try:
        cpus = lines[0].strip()
        load_avg = lines[1].strip()
        mem_parts = lines[2].split()
        swap_parts = lines[3].split()
        disk_parts = lines[4].split()

        mem_total_b = int(mem_parts[0])
        mem_used_b = int(mem_parts[1])
        swap_total_b = int(swap_parts[0])
        swap_used_b = int(swap_parts[1])

        mem_pct = f"{mem_used_b * 100 // mem_total_b}%" if mem_total_b > 0 else "0%"
        swap_pct = f"{swap_used_b * 100 // swap_total_b}%" if swap_total_b > 0 else "0%"

        return {
            "cpus": cpus,
            "load_avg": load_avg,
            "mem_total": _human_bytes(mem_total_b),
            "mem_used": _human_bytes(mem_used_b),
            "mem_pct": mem_pct,
            "swap_total": _human_bytes(swap_total_b),
            "swap_used": _human_bytes(swap_used_b),
            "swap_pct": swap_pct,
            "disk_total": disk_parts[0],
            "disk_used": disk_parts[1],
            "disk_pct": disk_parts[2],
        }
    except (IndexError, ValueError):
        return None


def _human_bytes(b: int) -> str:
    """Format bytes as a human-readable string (e.g. 494M, 8.0G)."""
    if b < 1024:
        return f"{b}B"
    for unit in ("K", "M", "G", "T"):
        b_f = b / 1024
        if b_f < 1024 or unit == "T":
            return f"{b_f:.1f}{unit}" if b_f >= 10 else f"{b_f:.2f}{unit}"
        b = int(b_f)
    return f"{b}T"


def _require_vm(db: Database, name: str) -> VMRow:
    vm = db.get_vm(name)
    if vm is None:
        raise NotFoundError(
            f"VM '{name}' not found",
            entity_kind="vm",
            entity_name=name,
        )
    return vm


def get_provisioner_for_vm(db: Database, vm: VMRow, config: Config | None = None) -> VMProvisioner:
    if vm.platform == "proxmox":
        from agentworks.vms.provisioners.proxmox import ProxmoxProvisioner

        if config is None:
            from agentworks.config import load_config
            config = load_config()
        return ProxmoxProvisioner(config.proxmox)  # type: ignore[arg-type]

    vm_host_ssh: str | None = None
    if vm.vm_host_name:
        host = db.get_vm_host(vm.vm_host_name)
        if host:
            vm_host_ssh = host.ssh_host
    return get_provisioner(vm.platform, vm_host_ssh)


def _is_tailscale_reachable(tailscale_host: str) -> bool:
    """Quick check whether a Tailscale IP is still reachable."""
    import subprocess

    try:
        result = subprocess.run(
            ["tailscale", "ping", "--timeout=5s", "-c=1", tailscale_host],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def port_forward_vm(
    db: Database,
    config: Config,
    name: str,
    ports: list[str],
    address: str = "localhost",
    verbose: bool = False,
) -> None:
    """Forward one or more local ports to a VM via SSH tunnels.

    Each port spec is either REMOTE_PORT (local defaults to same) or
    LOCAL_PORT:REMOTE_PORT, matching kubectl port-forward syntax.
    """
    import signal
    import subprocess
    import sys

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    # Parse port specs
    forwards: list[tuple[int, int]] = []  # (local_port, remote_port)
    for spec in ports:
        parts = spec.split(":")
        if len(parts) == 1:
            try:
                port = int(parts[0])
            except ValueError:
                raise ValidationError(
                    f"invalid port '{spec}'",
                    entity_kind="vm",
                    entity_name=name,
                ) from None
            forwards.append((port, port))
        elif len(parts) == 2:
            try:
                local_port = int(parts[0])
                remote_port = int(parts[1])
            except ValueError:
                raise ValidationError(
                    f"invalid port spec '{spec}'",
                    entity_kind="vm",
                    entity_name=name,
                ) from None
            forwards.append((local_port, remote_port))
        else:
            raise ValidationError(
                f"invalid port spec '{spec}' (expected [LOCAL:]REMOTE)",
                entity_kind="vm",
                entity_name=name,
            )

    # Validate port ranges
    for local_port, remote_port in forwards:
        for label, port in [("local", local_port), ("remote", remote_port)]:
            if port < 1 or port > 65535:
                raise ValidationError(
                    f"{label} port {port} out of range (1-65535)",
                    entity_kind="vm",
                    entity_name=name,
                )

    # Build SSH command with -L flags for each forward
    ssh_cmd = ["ssh", "-N", "-o", "StrictHostKeyChecking=accept-new"]
    if config.operator.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.operator.ssh_private_key)])
    for local_port, remote_port in forwards:
        ssh_cmd.extend(["-L", f"{address}:{local_port}:localhost:{remote_port}"])
    if verbose:
        ssh_cmd.append("-v")
    ssh_cmd.append(f"{vm.admin_username}@{vm.tailscale_host}")

    # Print forwarding info
    for local_port, remote_port in forwards:
        output.info(f"Forwarding {address}:{local_port} -> {vm.tailscale_host}:{remote_port}")
    if not verbose:
        output.info("Use --verbose for detailed SSH output.")

    # Run in foreground until interrupted
    with keep_vm_active(db, config, vm):
        try:
            proc = subprocess.Popen(ssh_cmd)

            # Forward SIGINT/SIGTERM to the SSH process for clean shutdown
            def _handle_signal(sig: int, _frame: object) -> None:
                proc.terminate()

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)

            rc = proc.wait()
            sys.exit(rc)
        except OSError as e:
            raise ConnectivityError(
                f"failed to start SSH: {e}",
                entity_kind="vm",
                entity_name=name,
            ) from e


def _ensure_tailscale(
    db: Database,
    config: Config,
    vm: VMRow,
    provisioner: VMProvisioner,
) -> None:
    """After starting a VM, verify Tailscale connectivity and rejoin if needed."""
    from agentworks.ssh import admin_exec_target, wait_for_reconnect

    # Refresh VM row in case tailscale_host was cleared on stop
    vm = _require_vm(db, vm.name)

    # If we have a known Tailscale host, wait for it to reconnect after boot.
    # This avoids unnecessarily attaching a public IP on Azure.
    if vm.tailscale_host:
        if wait_for_reconnect(admin_exec_target(vm, config)):
            return

        # Tailscale didn't reconnect (ephemeral key expired, etc.)
        output.info(f"Tailscale node {vm.tailscale_host} did not reconnect, rejoining...")
        db.clear_vm_tailscale(vm.name)

    # For Azure, attach a temporary public IP for the rejoin
    from agentworks.vms.provisioners.azure import AzureProvisioner

    azure_provisioner = provisioner if isinstance(provisioner, AzureProvisioner) else None
    if azure_provisioner is not None:
        azure_provisioner.attach_public_ip(vm)

    try:
        verify_tailscale_available()
        exec_target = provisioner.admin_exec_target(vm, config=config)
        rejoin_tailscale(db, vm.name, exec_target)
    finally:
        if azure_provisioner is not None:
            azure_provisioner.detach_public_ip(vm)

            # Wait for Tailscale SSH to reconnect after IP change
            from agentworks.ssh import admin_exec_target, wait_for_reconnect

            refreshed = db.get_vm(vm.name)
            if refreshed and refreshed.tailscale_host:
                wait_for_reconnect(admin_exec_target(refreshed, config))

    # Update SSH config in case the Tailscale IP changed
    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)
