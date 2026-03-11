"""VM lifecycle management -- create, list, start, stop, delete."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from agentworks.config import VALID_PLATFORMS, validate_name
from agentworks.db import InitStatus, ProvisioningStatus, VMStatus
from agentworks.vms.initializer import (
    initialize_vm,
    rejoin_tailscale,
    resolve_git_credential_providers,
    run_initialization,
    verify_git_credential_auth,
    verify_tailscale_available,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.vms.base import VMProvisioner


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
    else:
        msg = f"Unknown platform: {platform}"
        raise ValueError(msg)


def create_vm(
    db: Database,
    config: Config,
    *,
    name: str,
    platform: str | None = None,
    vm_host: str | None = None,
    extra_packages: list[str] | None = None,
    git_credentials: list[str] | None = None,
    cpus: int | None = None,
    memory: int | None = None,
    disk: int | None = None,
    azure_vm_size: str | None = None,
    vm_user: str | None = None,
) -> None:
    """Create a new VM: provision + initialize."""
    # Resolve defaults
    platform = platform or config.defaults.platform or "lima"
    if platform not in VALID_PLATFORMS:
        typer.echo(f"Error: invalid platform '{platform}'", err=True)
        raise typer.Exit(1)

    vm_name = name
    validate_name(vm_name)

    if db.get_vm(vm_name) is not None:
        typer.echo(f"Error: VM '{vm_name}' already exists", err=True)
        raise typer.Exit(1)

    # Resolve VM host for Lima
    vm_host_ssh: str | None = None
    vm_host_name: str | None = None
    if platform == "lima":
        vm_host_name = vm_host or config.defaults.vm_host
        if vm_host_name:
            host_row = db.get_vm_host(vm_host_name)
            if host_row is None:
                typer.echo(f"Error: VM host '{vm_host_name}' not found", err=True)
                raise typer.Exit(1)
            vm_host_ssh = host_row.ssh_host

    # Azure config validation
    if platform == "azure" and config.azure is None:
        typer.echo("Error: [azure] config section required for azure platform", err=True)
        raise typer.Exit(1)

    # Resolve resource settings: CLI flag > config > built-in default
    resolved_cpus = cpus or config.vm.cpus
    resolved_memory = memory or config.vm.memory
    resolved_disk = disk or config.vm.disk
    resolved_azure_size = azure_vm_size or config.vm.azure_vm_size
    resolved_vm_user = vm_user or config.vm.vm_user

    # Pre-flight checks
    verify_tailscale_available()
    providers = resolve_git_credential_providers(config, git_credentials)
    verify_git_credential_auth(providers)

    # Collect secrets upfront so the user isn't interrupted mid-provisioning
    tailscale_auth_key, git_tokens = _collect_secrets(providers, vm_name)

    # Create DB record with as-provisioned resource values
    db.insert_vm(
        vm_name,
        platform=platform,
        vm_host_name=vm_host_name,
        extra_packages=extra_packages,
        cpus=resolved_cpus,
        memory_gib=resolved_memory,
        disk_gib=resolved_disk,
        vm_user=resolved_vm_user,
    )

    try:
        # Platform provisioning (use concrete types for resource kwargs)
        if platform == "lima":
            from agentworks.vms.provisioners.lima import LimaProvisioner

            lima = LimaProvisioner(vm_host_ssh=vm_host_ssh)
            result = lima.create(
                vm_name, config, extra_packages,
                cpus=resolved_cpus,
                memory=resolved_memory,
                disk=resolved_disk,
            )
        elif platform == "azure":
            from agentworks.vms.provisioners.azure import AzureProvisioner

            azure = AzureProvisioner()
            result = azure.create(
                vm_name, config, extra_packages,
                azure_vm_size=resolved_azure_size,
                vm_user=resolved_vm_user,
            )
        elif platform == "wsl2":
            from agentworks.vms.provisioners.wsl2 import WSL2Provisioner

            wsl2 = WSL2Provisioner()
            result = wsl2.create(
                vm_name, config, extra_packages,
                vm_user=resolved_vm_user,
            )
        else:
            provisioner = get_provisioner(platform, vm_host_ssh)
            result = provisioner.create(vm_name, config, extra_packages)

        # Update DB with platform-specific metadata
        if result.azure_resource_id:
            db.update_vm_azure_resource_id(vm_name, result.azure_resource_id)
        if result.wsl_distro_name:
            db.update_vm_wsl_distro_name(vm_name, result.wsl_distro_name)

        # VM initialization
        initialize_vm(
            db, config, vm_name,
            exec_target=result.exec_target,
            providers=providers,
            extra_packages=extra_packages,
            is_wsl2=(platform == "wsl2"),
            vm_user=resolved_vm_user,
            tailscale_auth_key=tailscale_auth_key,
            git_tokens=git_tokens,
        )

        # Post-init: SSH config entry + Azure public IP cleanup
        vm_row = db.get_vm(vm_name)
        if vm_row is not None:
            from agentworks.ssh_config import upsert_vm_entry

            upsert_vm_entry(config, vm_row)

            if platform == "azure":
                from agentworks.vms.provisioners.azure import AzureProvisioner as _AP

                _AP().detach_public_ip(vm_row)

    except Exception as e:
        # Write full details to init log, show only summary on console
        from agentworks.vms.init_log import InitLogger, find_init_logs

        logger = InitLogger(vm_name)
        logger.step("Error")
        detail = getattr(e, "detail", None)
        if detail:
            logger.output(detail)
        else:
            logger.output(str(e))
        logger.close()

        typer.echo(f"\nError: {e}", err=True)
        logs = find_init_logs(vm_name)
        if logs:
            typer.echo(f"Details: {logs[0]}", err=True)
        _prompt_failed_vm(db, config, vm_name)
        return

    # Final status is set by initialize_vm (COMPLETE or PARTIAL)
    vm = db.get_vm(vm_name)
    assert vm is not None
    if vm.init_status == InitStatus.PARTIAL.value:
        typer.echo(f"\nVM '{vm_name}' is ready (with warnings -- see above)")
    else:
        typer.echo(f"\nVM '{vm_name}' is ready!")


def list_vms(db: Database) -> None:
    """List all VMs with their init and runtime status."""
    vms = db.list_vms()
    if not vms:
        typer.echo("No VMs registered.")
        return

    header = (
        f"{'NAME':<20} {'PLATFORM':<10} {'HOST':<15} {'PROV':<12} {'INIT':<12} "
        f"{'RESOURCES':<15} {'TAILSCALE':<20} {'CREATED'}"
    )
    typer.echo(header)
    typer.echo("-" * 125)
    for vm in vms:
        resources = "-"
        if vm.cpus is not None:
            resources = f"{vm.cpus}c/{vm.memory_gib}G/{vm.disk_gib}G"
        typer.echo(
            f"{vm.name:<20} {vm.platform:<10} {vm.vm_host_name or '-':<15} "
            f"{vm.provisioning_status:<12} {vm.init_status:<12} {resources:<15} "
            f"{vm.tailscale_host or '-':<20} {vm.created_at}"
        )


def describe_vm(db: Database, name: str) -> None:
    """Show detailed information about a VM."""
    vm = _require_vm(db, name)

    # VM details
    typer.echo(f"Name:           {vm.name}")
    typer.echo(f"Platform:       {vm.platform}")
    typer.echo(f"VM Host:        {vm.vm_host_name or '-'}")
    typer.echo(f"VM User:        {vm.vm_user}")
    typer.echo(f"Provisioning:   {vm.provisioning_status}")
    typer.echo(f"Initialization: {vm.init_status}")
    typer.echo(f"Tailscale:      {vm.tailscale_host or '-'}")

    if vm.cpus is not None:
        typer.echo(f"Resources:      {vm.cpus} CPUs, {vm.memory_gib} GiB RAM, {vm.disk_gib} GiB disk")
    if vm.azure_resource_id:
        typer.echo(f"Azure ID:       {vm.azure_resource_id}")
    if vm.wsl_distro_name:
        typer.echo(f"WSL Distro:     {vm.wsl_distro_name}")
    if vm.extra_packages:
        typer.echo(f"Extra Packages: {', '.join(vm.extra_packages)}")

    typer.echo(f"Created:        {vm.created_at}")
    if vm.last_seen_at:
        typer.echo(f"Last Seen:      {vm.last_seen_at}")

    # Workspaces
    workspaces = db.list_workspaces(vm_name=name)
    typer.echo(f"\nWorkspaces ({len(workspaces)}):")
    if workspaces:
        for ws in workspaces:
            typer.echo(f"  {ws.name:<20} {ws.type:<10} {ws.workspace_path}")
    else:
        typer.echo("  (none)")

    # Events
    events = db.list_vm_events(name)
    typer.echo(f"\nEvents ({len(events)}):")
    if events:
        for event in events:
            detail = f"  {event.detail}" if event.detail else ""
            typer.echo(f"  {event.created_at}  {event.event}{detail}")
    else:
        typer.echo("  (none)")


def shell_vm(db: Database, config: Config, name: str) -> None:
    """Open a shell on a VM's home directory."""
    import subprocess
    import sys

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{name}' has no Tailscale IP (init may not be complete)", err=True)
        raise typer.Exit(1)

    ssh_cmd = ["ssh", "-t"]
    if config.user.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.user.ssh_private_key)])
    ssh_cmd.append(f"{vm.vm_user}@{vm.tailscale_host}")

    sys.exit(subprocess.call(ssh_cmd))


def add_git_credential(db: Database, config: Config, name: str, credential_name: str) -> None:
    """Add or update a git credential on a VM."""
    from agentworks.ssh import run as ssh_run
    from agentworks.ssh import ssh_target_for_vm

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{name}' has no Tailscale IP (init may not be complete)", err=True)
        raise typer.Exit(1)

    cred_config = config.git_credentials.get(credential_name)
    if cred_config is None:
        typer.echo(f"Error: git credential '{credential_name}' not found in config", err=True)
        raise typer.Exit(1)

    providers = resolve_git_credential_providers(config, [credential_name])
    provider = providers[credential_name]

    token = provider.obtain_token(name)
    new_lines = provider.credential_lines(token)

    target = ssh_target_for_vm(vm, config)

    # Read existing credentials, filter out entries for the same host/path
    result = ssh_run(target, "cat ~/.git-credentials 2>/dev/null || true")
    existing = result.stdout.strip().splitlines() if result.stdout.strip() else []

    # Extract host/path from new lines for matching: "https://user:tok@host/path" -> "host/path"
    new_hostpaths = {line.split("@", 1)[1] for line in new_lines if "@" in line}

    # Filter out old entries whose host/path matches any new entry
    filtered = [
        e for e in existing
        if "@" not in e or e.split("@", 1)[1] not in new_hostpaths
    ]

    # Write back filtered + new
    from agentworks.ssh import write_file

    all_lines = filtered + new_lines
    cred_content = "\n".join(all_lines) + "\n"
    write_file(target, "~/.git-credentials", cred_content, mode="600")
    ssh_run(target, "git config --global credential.helper store")

    typer.echo(f"Git credential '{credential_name}' configured on VM '{name}'")


def start_vm(db: Database, config: Config, name: str) -> None:
    """Start a stopped VM."""
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    provisioner = _get_provisioner_for_vm(db, vm)
    status = provisioner.status(vm)
    if status == VMStatus.RUNNING:
        typer.echo(f"VM '{name}' is already running")
    else:
        provisioner.start(vm)

    _ensure_tailscale(db, config, vm, provisioner)


def stop_vm(db: Database, config: Config, name: str) -> None:
    """Stop a running VM."""
    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    provisioner = _get_provisioner_for_vm(db, vm)
    status = provisioner.status(vm)
    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        typer.echo(f"VM '{name}' is already stopped")
        return
    provisioner.stop(vm)

    # Check if the Tailscale node survived the stop (ephemeral nodes disappear)
    if vm.tailscale_host and not _is_tailscale_reachable(vm.tailscale_host):
        typer.echo(f"Tailscale node for VM '{name}' is no longer reachable (ephemeral key?)")
        db.clear_vm_tailscale(name)


def delete_vm(db: Database, config: Config, name: str, *, force: bool = False) -> None:
    """Delete a VM, cleaning up all associated resources."""
    vm = _require_vm(db, name)

    # Check for workspaces
    ws_count = db.count_workspaces_on_vm(name)
    if ws_count > 0 and not force:
        typer.echo(
            f"Error: VM '{name}' has {ws_count} workspace(s). "
            f"Delete them first, or use --force.",
            err=True,
        )
        raise typer.Exit(1)

    # Platform-specific cleanup (also handles Tailscale logout)
    try:
        provisioner = _get_provisioner_for_vm(db, vm)

        # Tailscale logout (best-effort, via provisioning transport)
        if vm.tailscale_host:
            _tailscale_logout(provisioner, vm)

        provisioner.delete(vm)
    except Exception as e:
        typer.echo(f"Warning: platform cleanup failed: {e}", err=True)

    # Clean up init logs
    from agentworks.vms.init_log import delete_init_logs

    log_count = delete_init_logs(name)
    if log_count:
        typer.echo(f"Cleaned up {log_count} init log(s)")

    # Remove SSH config entry
    from agentworks.ssh_config import remove_vm_entry

    remove_vm_entry(config, name)

    # Remove from DB (cascades workspaces and agents)
    db.delete_vm(name)
    typer.echo(f"VM '{name}' deleted")


def reinit_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    git_credentials: list[str] | None = None,
) -> None:
    """Re-run initialization on a VM that has already been provisioned.

    Requires provisioning_status == complete and a valid Tailscale connection.
    """
    from agentworks.ssh import ExecTarget, ssh_target_for_vm
    from agentworks.vms.init_log import InitLogger

    vm = _require_vm(db, name)

    if vm.provisioning_status != ProvisioningStatus.COMPLETE.value:
        typer.echo(
            f"Error: VM '{name}' provisioning is '{vm.provisioning_status}', not 'complete'. "
            f"Cannot reinitialize.",
            err=True,
        )
        raise typer.Exit(1)

    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{name}' has no Tailscale IP", err=True)
        raise typer.Exit(1)

    # Pre-flight checks
    verify_tailscale_available()
    providers = resolve_git_credential_providers(config, git_credentials)
    verify_git_credential_auth(providers)

    # Collect git tokens upfront
    git_tokens: dict[str, str] = {}
    for cred_name, provider in providers.items():
        git_tokens[cred_name] = provider.obtain_token(name)

    # Build Tailscale SSH target
    ts_target = ExecTarget(ssh=ssh_target_for_vm(vm, config), default_timeout=60)

    home = f"/home/{vm.vm_user}"
    logger = InitLogger(name)

    run_initialization(
        db, config, name, ts_target, providers, home, vm.vm_user,
        vm.extra_packages or None, logger, git_tokens=git_tokens,
    )

    vm = db.get_vm(name)
    assert vm is not None
    if vm.init_status == InitStatus.PARTIAL.value:
        typer.echo(f"\nVM '{name}' reinitialized (with warnings -- see above)")
    else:
        typer.echo(f"\nVM '{name}' reinitialized successfully!")


def _prompt_failed_vm(db: Database, config: Config, vm_name: str) -> None:
    """After a failure, prompt user based on whether provisioning or init failed."""
    vm = db.get_vm(vm_name)
    if vm is None:
        return

    if vm.provisioning_status == ProvisioningStatus.FAILED.value:
        # Provisioning failed -- VM is unreachable, only delete makes sense
        typer.echo(
            "\nProvisioning failed. Delete VM? You can keep it for manual troubleshooting, "
            "but agentworks cannot use or manage it.",
            err=True,
        )
        if typer.confirm("Delete VM?", default=True):
            delete_vm(db, config, vm_name, force=True)
        else:
            typer.echo(
                f"VM '{vm_name}' kept in failed state. Only 'vm delete' is supported.",
                err=True,
            )
    elif vm.init_status == InitStatus.FAILED.value:
        # Init failed but provisioning succeeded -- reinit is an option
        typer.echo(
            "\nInitialization failed. You can re-run initialization with 'vm reinit', "
            "delete the VM, or keep it for troubleshooting.",
            err=True,
        )
        typer.echo("  [r] Reinit  [d] Delete  [k] Keep", err=True)
        choice = typer.prompt("Choice", default="r").lower()
        if choice == "d":
            delete_vm(db, config, vm_name, force=True)
        elif choice == "r":
            reinit_vm(db, config, vm_name)
        else:
            typer.echo(
                f"VM '{vm_name}' kept. Use 'vm reinit' to retry initialization.",
                err=True,
            )
    else:
        # Post-init failure (e.g. SSH config, IP cleanup) -- VM may be usable
        typer.echo(
            f"\nVM '{vm_name}' encountered a post-initialization error but may still be usable.",
            err=True,
        )


def _tailscale_logout(provisioner: VMProvisioner, vm: VMRow) -> None:
    """Best-effort: deregister from Tailscale via the provisioning transport.

    Uses the provisioner's exec_target (not Tailscale SSH) because we can't
    ask Tailscale to tear itself down over the connection it provides.
    For Azure VMs, temporarily attaches a public IP for SSH access.
    """
    from agentworks.vms.provisioners.azure import AzureProvisioner

    typer.echo("Deregistering from Tailscale...")
    try:
        if isinstance(provisioner, AzureProvisioner):
            provisioner.attach_public_ip(vm)
        exec_target = provisioner.exec_target(vm)
        exec_target.run_as_root("tailscale down", timeout=15)
        exec_target.run_as_root("tailscale logout", timeout=15)
        typer.echo("Tailscale node deregistered")
    except Exception as e:
        typer.echo(f"Warning: Tailscale logout failed (node may remain in admin console): {e}", err=True)


def _init_log_hint(vm_name: str) -> str:
    """Return a log hint suffix like ' See init log: <path>' or empty string."""
    from agentworks.vms.init_log import find_init_logs

    logs = find_init_logs(vm_name)
    return f" See init log: {logs[0]}" if logs else ""


def _guard_failed_vm(vm: VMRow) -> None:
    """Block operations on VMs with failed provisioning or initialization."""
    if vm.provisioning_status == ProvisioningStatus.FAILED.value:
        typer.echo(
            f"Error: VM '{vm.name}' has failed provisioning. "
            f"Only 'vm delete' is supported.{_init_log_hint(vm.name)}",
            err=True,
        )
        raise typer.Exit(1)
    if vm.init_status == InitStatus.FAILED.value:
        typer.echo(
            f"Error: VM '{vm.name}' has failed initialization. "
            f"Use 'vm reinit' to retry or 'vm delete' to remove.{_init_log_hint(vm.name)}",
            err=True,
        )
        raise typer.Exit(1)


def _collect_secrets(
    providers: dict,
    vm_name: str,
) -> tuple[str | None, dict[str, str]]:
    """Collect all secrets upfront before provisioning starts.

    Returns (tailscale_auth_key, git_tokens).
    """
    import os

    from agentworks.prompt import prompt_secret

    typer.echo("\nCollecting credentials...")

    # Tailscale
    ts_auth_key = os.environ.get("TAILSCALE_AUTH_KEY")
    if ts_auth_key:
        typer.echo("  Tailscale auth key found in environment")
    else:
        ts_auth_key = prompt_secret(
            "  Tailscale auth key",
            hint="Generate a key at https://login.tailscale.com/admin/settings/keys",
        )

    # Git credentials
    git_tokens: dict[str, str] = {}
    for name, provider in providers.items():
        token = provider.obtain_token(vm_name)
        git_tokens[name] = token

    typer.echo("")
    return ts_auth_key, git_tokens


def _require_vm(db: Database, name: str) -> VMRow:
    vm = db.get_vm(name)
    if vm is None:
        typer.echo(f"Error: VM '{name}' not found", err=True)
        raise typer.Exit(1)
    return vm


def _get_provisioner_for_vm(db: Database, vm: VMRow) -> VMProvisioner:
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
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _ensure_tailscale(
    db: Database,
    config: Config,
    vm: VMRow,
    provisioner: VMProvisioner,
) -> None:
    """After starting a VM, verify Tailscale connectivity and rejoin if needed."""
    # Refresh VM row in case tailscale_host was cleared on stop
    vm = _require_vm(db, vm.name)

    if vm.tailscale_host and _is_tailscale_reachable(vm.tailscale_host):
        typer.echo(f"Tailscale node reachable at {vm.tailscale_host}")
        return

    if vm.tailscale_host:
        typer.echo(f"Tailscale node {vm.tailscale_host} is not reachable")
        db.clear_vm_tailscale(vm.name)

    # For Azure, attach a temporary public IP for the rejoin
    from agentworks.vms.provisioners.azure import AzureProvisioner

    is_azure = isinstance(provisioner, AzureProvisioner)
    if is_azure:
        provisioner.attach_public_ip(vm)

    try:
        verify_tailscale_available()
        exec_target = provisioner.exec_target(vm)
        rejoin_tailscale(
            db, vm.name, exec_target,
            is_wsl2=(vm.platform == "wsl2"),
        )
    finally:
        if is_azure:
            provisioner.detach_public_ip(vm)

    # Update SSH config in case the Tailscale IP changed
    from agentworks.ssh_config import upsert_vm_entry

    vm = _require_vm(db, vm.name)
    upsert_vm_entry(config, vm)
