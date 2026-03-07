"""VM lifecycle management -- create, list, start, stop, delete."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import typer

from agentworks.config import NAME_RE, VALID_PLATFORMS
from agentworks.db import InitStatus
from agentworks.vms.initializer import initialize_vm, resolve_git_host_providers, verify_git_host_auth

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


def _generate_name() -> str:
    return secrets.token_hex(4)[:7]


def create_vm(
    db: Database,
    config: Config,
    *,
    name: str | None = None,
    platform: str | None = None,
    vm_host: str | None = None,
    extra_packages: list[str] | None = None,
    git_hosts: list[str] | None = None,
) -> None:
    """Create a new VM: provision + initialize."""
    # Resolve defaults
    platform = platform or config.defaults.platform or "lima"
    if platform not in VALID_PLATFORMS:
        typer.echo(f"Error: invalid platform '{platform}'", err=True)
        raise typer.Exit(1)

    vm_name = name or _generate_name()
    if not NAME_RE.match(vm_name):
        typer.echo(f"Error: invalid name '{vm_name}'. Must match [a-z0-9\\-_.]", err=True)
        raise typer.Exit(1)

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

    # Pre-flight: verify git host auth
    providers = resolve_git_host_providers(config, git_hosts)
    verify_git_host_auth(providers)

    # Create DB record
    db.insert_vm(
        vm_name,
        platform=platform,
        vm_host_name=vm_host_name,
        extra_packages=extra_packages,
    )

    try:
        # Platform provisioning
        provisioner = get_provisioner(platform, vm_host_ssh)
        result = provisioner.create(vm_name, config, extra_packages)

        # Update DB with platform-specific metadata
        if result.azure_resource_id:
            db._conn.execute(
                "UPDATE vms SET azure_resource_id = ? WHERE name = ?",
                (result.azure_resource_id, vm_name),
            )
            db._conn.commit()
        if result.wsl_distro_name:
            db._conn.execute(
                "UPDATE vms SET wsl_distro_name = ? WHERE name = ?",
                (result.wsl_distro_name, vm_name),
            )
            db._conn.commit()

        # VM initialization
        initialize_vm(
            db, config, vm_name,
            exec_target=result.exec_target,
            providers=providers,
            extra_packages=extra_packages,
            is_wsl2=(platform == "wsl2"),
        )
    except Exception:
        db.update_vm_init_status(vm_name, InitStatus.FAILED)
        raise

    typer.echo(f"\nVM '{vm_name}' is ready!")


def list_vms(db: Database) -> None:
    """List all VMs with their init and runtime status."""
    vms = db.list_vms()
    if not vms:
        typer.echo("No VMs registered.")
        return

    typer.echo(f"{'NAME':<20} {'PLATFORM':<10} {'HOST':<15} {'INIT STATUS':<15} {'TAILSCALE':<20} {'CREATED'}")
    typer.echo("-" * 100)
    for vm in vms:
        typer.echo(
            f"{vm.name:<20} {vm.platform:<10} {vm.vm_host_name or '-':<15} "
            f"{vm.init_status:<15} {vm.tailscale_host or '-':<20} {vm.created_at}"
        )


def start_vm(db: Database, config: Config, name: str) -> None:
    """Start a stopped VM."""
    vm = _require_vm(db, name)
    provisioner = _get_provisioner_for_vm(db, vm)
    status = provisioner.status(vm)
    if status.value == "running":
        typer.echo(f"VM '{name}' is already running")
        return
    provisioner.start(vm)


def stop_vm(db: Database, config: Config, name: str) -> None:
    """Stop a running VM."""
    vm = _require_vm(db, name)
    provisioner = _get_provisioner_for_vm(db, vm)
    status = provisioner.status(vm)
    if status.value in ("stopped", "deallocated"):
        typer.echo(f"VM '{name}' is already stopped")
        return
    provisioner.stop(vm)


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

    # Platform-specific cleanup
    try:
        provisioner = _get_provisioner_for_vm(db, vm)
        provisioner.delete(vm)
    except Exception as e:
        typer.echo(f"Warning: platform cleanup failed: {e}", err=True)

    # Remove from DB (cascades workspaces and keys)
    db.delete_vm(name)
    typer.echo(f"VM '{name}' deleted")


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
