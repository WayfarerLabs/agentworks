"""Manage SSH config entries for agentworks VMs.

Entries are kept in a managed section at the end of the user's SSH config
file, delimited by a marker comment. The managed section is rewritten
in full on each update.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import VMRow

MARKER = "# --- Managed by agentworks. Do not edit below this line. ---"
HOST_PREFIX = "awvm--"


def ssh_host_alias(vm_name: str) -> str:
    """Return the SSH host alias for a VM."""
    return f"{HOST_PREFIX}{vm_name}"


def upsert_vm_entry(config: Config, vm: VMRow) -> None:
    """Add or update an SSH config entry for a VM."""
    if not vm.tailscale_host:
        return

    ssh_config = config.user.ssh_config
    user_section, entries = _read_managed(ssh_config)

    alias = ssh_host_alias(vm.name)
    entries[alias] = _format_entry(
        alias=alias,
        hostname=vm.tailscale_host,
        user=vm.vm_user,
        identity_file=config.user.ssh_private_key,
    )

    _write(ssh_config, user_section, entries)
    typer.echo(f"  SSH config: {alias} -> {vm.tailscale_host}")


def remove_vm_entry(config: Config, vm_name: str) -> None:
    """Remove an SSH config entry for a VM."""
    ssh_config = config.user.ssh_config
    if not ssh_config.exists():
        return

    user_section, entries = _read_managed(ssh_config)
    alias = ssh_host_alias(vm_name)
    if alias not in entries:
        return

    del entries[alias]
    _write(ssh_config, user_section, entries)
    typer.echo(f"  SSH config: removed {alias}")


def _format_entry(
    alias: str,
    hostname: str,
    user: str,
    identity_file: Path,
) -> str:
    """Format a single SSH config Host block."""
    return (
        f"Host {alias}\n"
        f"    HostName {hostname}\n"
        f"    User {user}\n"
        f"    IdentityFile {identity_file}\n"
    )


def _read_managed(ssh_config: Path) -> tuple[str, dict[str, str]]:
    """Read the SSH config, splitting into user section and managed entries.

    Returns (user_section, entries) where entries is {alias: block_text}.
    """
    entries: dict[str, str] = {}

    if not ssh_config.exists():
        return "", entries

    content = ssh_config.read_text()
    marker_idx = content.find(MARKER)

    if marker_idx == -1:
        return content, entries

    user_section = content[:marker_idx]
    managed_section = content[marker_idx + len(MARKER) :]

    # Parse Host blocks from managed section
    current_alias = ""
    current_lines: list[str] = []

    for line in managed_section.splitlines():
        if line.startswith("Host "):
            if current_alias:
                entries[current_alias] = "\n".join(current_lines) + "\n"
            current_alias = line.split()[1] if len(line.split()) > 1 else ""
            current_lines = [line]
        elif current_alias:
            current_lines.append(line)

    if current_alias:
        entries[current_alias] = "\n".join(current_lines) + "\n"

    return user_section, entries


def _write(
    ssh_config: Path,
    user_section: str,
    entries: dict[str, str],
) -> None:
    """Write the SSH config file with user section + managed section."""
    ssh_config.parent.mkdir(parents=True, exist_ok=True)

    parts = [user_section.rstrip("\n")]

    if entries:
        parts.append("")
        parts.append(MARKER)
        for block in entries.values():
            parts.append(block.rstrip("\n"))

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"

    ssh_config.write_text(content)
