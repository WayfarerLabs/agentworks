"""VM host management -- add, list, remove, OS detection."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from agentworks.config import validate_name
from agentworks.ssh import SSHError, SSHTarget, run

if TYPE_CHECKING:
    from agentworks.db import Database


def detect_os(ssh_host: str) -> str | None:
    """Detect the OS of a remote host via SSH."""
    try:
        result = run(
            SSHTarget(host=ssh_host, user=None, login_shell=True),
            "uname -s",
            timeout=15,
        )
        raw = result.stdout.strip().lower()
        if "darwin" in raw:
            return "darwin"
        if "linux" in raw:
            return "linux"
        return raw or None
    except (SSHError, TimeoutError):
        return None


def add_vm_host(db: Database, name: str, ssh_host: str, platform: str = "lima") -> None:
    """Register a new VM host."""
    validate_name(name)

    if platform != "lima":
        typer.echo(f"Error: only 'lima' platform is supported for VM hosts, got: {platform}", err=True)
        raise typer.Exit(1)

    if db.get_vm_host(name) is not None:
        typer.echo(f"Error: VM host '{name}' already exists", err=True)
        raise typer.Exit(1)

    typer.echo(f"Detecting OS on {ssh_host}...")
    detected_os = detect_os(ssh_host)
    if detected_os:
        typer.echo(f"Detected OS: {detected_os}")
    else:
        typer.echo("Warning: could not detect OS (SSH connection may have failed)")

    db.insert_vm_host(name, ssh_host, platform=platform, os=detected_os)
    typer.echo(f"VM host '{name}' added ({ssh_host})")


def list_vm_hosts(db: Database) -> None:
    """List all registered VM hosts."""
    hosts = db.list_vm_hosts()
    if not hosts:
        typer.echo("No VM hosts registered.")
        return

    typer.echo(f"{'NAME':<20} {'SSH HOST':<30} {'PLATFORM':<10} {'OS':<10} {'LAST SEEN'}")
    typer.echo("-" * 90)
    for h in hosts:
        typer.echo(f"{h.name:<20} {h.ssh_host:<30} {h.platform:<10} {h.os or '-':<10} {h.last_seen_at or 'never'}")


def remove_vm_host(db: Database, name: str, *, force: bool = False) -> None:
    """Remove a VM host. Refuses if VMs reference it unless --force."""
    host = db.get_vm_host(name)
    if host is None:
        typer.echo(f"Error: VM host '{name}' not found", err=True)
        raise typer.Exit(1)

    vm_count = db.count_vms_on_host(name)
    if vm_count > 0 and not force:
        typer.echo(
            f"Error: VM host '{name}' has {vm_count} VM(s). Delete them first, or use --force.",
            err=True,
        )
        raise typer.Exit(1)

    if vm_count > 0:
        # Nullify vm_host_name on VMs referencing this host to prevent dangling FK
        for vm in db.list_vms():
            if vm.vm_host_name == name:
                db.update_vm_host_ref(vm.name, None)
        typer.echo(f"Warning: cleared VM host reference on {vm_count} VM(s)", err=True)

    db.delete_vm_host(name)
    typer.echo(f"VM host '{name}' removed")
