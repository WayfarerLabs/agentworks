"""VM lifecycle management -- create, list, start, stop, delete."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from agentworks.config import VALID_PLATFORMS, validate_name
from agentworks.db import InitStatus, VMStatus
from agentworks.vms.initializer import (
    initialize_vm,
    rejoin_tailscale,
    resolve_git_host_providers,
    verify_git_host_auth,
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
    git_hosts: list[str] | None = None,
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
    providers = resolve_git_host_providers(config, git_hosts)
    verify_git_host_auth(providers)

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
        )
    except Exception:
        db.update_vm_init_status(vm_name, InitStatus.FAILED)
        from agentworks.vms.init_log import find_init_logs

        logs = find_init_logs(vm_name)
        if logs:
            typer.echo(f"Init log: {logs[0]}", err=True)
        _prompt_delete_failed_vm(db, config, vm_name)
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
        f"{'NAME':<20} {'PLATFORM':<10} {'HOST':<15} {'INIT STATUS':<15} "
        f"{'RESOURCES':<15} {'TAILSCALE':<20} {'CREATED'}"
    )
    typer.echo(header)
    typer.echo("-" * 115)
    for vm in vms:
        resources = "-"
        if vm.cpus is not None:
            resources = f"{vm.cpus}c/{vm.memory_gib}G/{vm.disk_gib}G"
        typer.echo(
            f"{vm.name:<20} {vm.platform:<10} {vm.vm_host_name or '-':<15} "
            f"{vm.init_status:<15} {resources:<15} "
            f"{vm.tailscale_host or '-':<20} {vm.created_at}"
        )


def shell_vm(db: Database, config: Config, name: str) -> None:
    """Open a shell on a VM's home directory."""
    import os

    vm = _require_vm(db, name)
    _guard_failed_vm(vm)
    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{name}' has no Tailscale IP (init may not be complete)", err=True)
        raise typer.Exit(1)

    ssh_cmd = ["ssh"]
    if config.user.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.user.ssh_private_key)])
    ssh_cmd.append(f"{vm.vm_user}@{vm.tailscale_host}")

    os.execvp("ssh", ssh_cmd)


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

    # Remove git host keys
    keys = db.list_vm_git_host_keys(name)
    if keys:
        providers = resolve_git_host_providers(config)
        for key in keys:
            provider = providers.get(key.git_host_name)
            if provider:
                typer.echo(f"Removing SSH key from {key.git_host_name}...")
                try:
                    provider.remove_key(key.remote_key_id)
                except Exception as e:
                    typer.echo(f"Warning: failed to remove key from {key.git_host_name}: {e}", err=True)

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

    # Remove from DB (cascades workspaces and keys)
    db.delete_vm(name)
    typer.echo(f"VM '{name}' deleted")


def _prompt_delete_failed_vm(db: Database, config: Config, vm_name: str) -> None:
    """After a fatal init failure, prompt user to delete or keep the VM."""
    typer.echo(
        "\nInit failed. Delete VM? (You can keep it for manual troubleshooting, "
        "but agentworks cannot manage it.)",
        err=True,
    )
    if typer.confirm("Delete VM?", default=True):
        delete_vm(db, config, vm_name, force=True)
    else:
        typer.echo(
            f"VM '{vm_name}' kept in 'failed' state. Only 'vm delete' is supported.",
            err=True,
        )


def _tailscale_logout(provisioner: VMProvisioner, vm: VMRow) -> None:
    """Best-effort: deregister from Tailscale via the provisioning transport.

    Uses the provisioner's exec_target (not Tailscale SSH) because we can't
    ask Tailscale to tear itself down over the connection it provides.
    """
    typer.echo("Deregistering from Tailscale...")
    try:
        exec_target = provisioner.exec_target(vm)
        exec_target.run_as_root("tailscale down", timeout=15)
        exec_target.run_as_root("tailscale logout", timeout=15)
        typer.echo("Tailscale node deregistered")
    except Exception as e:
        typer.echo(f"Warning: Tailscale logout failed (node may remain in admin console): {e}", err=True)


def _guard_failed_vm(vm: VMRow) -> None:
    """Block operations on VMs in 'failed' state."""
    if vm.init_status == InitStatus.FAILED.value:
        from agentworks.vms.init_log import find_init_logs

        logs = find_init_logs(vm.name)
        log_hint = f" See init log: {logs[0]}" if logs else ""
        typer.echo(
            f"Error: VM '{vm.name}' is in 'failed' state. "
            f"Only 'vm delete' is supported.{log_hint}",
            err=True,
        )
        raise typer.Exit(1)


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

    # Re-join via the provisioning transport
    verify_tailscale_available()
    exec_target = provisioner.exec_target(vm)
    rejoin_tailscale(
        db, vm.name, exec_target,
        is_wsl2=(vm.platform == "wsl2"),
    )
