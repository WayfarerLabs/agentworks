"""Manage SSH config entries for agentworks VMs.

When ssh_config_dir is enabled (default), all VM Host blocks are written to
a single ~/.ssh/config.d/agentworks.conf file and an Include directive is
added to the top of ~/.ssh/config. When disabled, entries are kept in a
managed section at the end of ~/.ssh/config (legacy behavior).

On first run with ssh_config_dir enabled, any legacy managed section is
cleaned up automatically.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow

_LEGACY_MARKER = "# --- Managed by agentworks. Do not edit below this line. ---"
_INCLUDE_COMMENT = "# Added by agentworks"
_CONFIG_DIR_NAME = "config.d"
_MANAGED_CONF = "agentworks.conf"


def _include_directive(ssh_config: Path) -> str:
    """Build the Include directive using the absolute path to config.d.

    Uses forward slashes on all platforms since OpenSSH expects POSIX-style
    paths even on Windows.
    """
    config_d = ssh_config.parent / _CONFIG_DIR_NAME
    return f"Include {config_d.as_posix()}/*"


def ssh_host_alias(vm_name: str, prefix: str = "awvm--") -> str:
    """Return the SSH host alias for a VM."""
    return f"{prefix}{vm_name}"


def upsert_vm_entry(config: Config, vm: VMRow, db: Database) -> None:
    """Add or update an SSH config entry for a VM, then rebuild."""
    if not vm.tailscale_host:
        return

    if config.user.ssh_config_dir:
        _rebuild_config_dir(config, db)
    else:
        _legacy_upsert(config, vm)

    alias = ssh_host_alias(vm.name, config.user.ssh_host_prefix)
    typer.echo(f"  SSH config: {alias} -> {vm.tailscale_host}")


def remove_vm_entry(config: Config, vm_name: str, db: Database) -> None:
    """Remove an SSH config entry for a VM, then rebuild."""
    if config.user.ssh_config_dir:
        _rebuild_config_dir(config, db)
    else:
        _legacy_remove(config, vm_name)

    alias = ssh_host_alias(vm_name, config.user.ssh_host_prefix)
    typer.echo(f"  SSH config: removed {alias}")


# -- config.d approach -----------------------------------------------------


def _rebuild_config_dir(config: Config, db: Database) -> None:
    """Declaratively rebuild ~/.ssh/config.d/agentworks.conf from DB state.

    Writes a single file containing Host blocks for all VMs with Tailscale
    hosts. Ensures the Include directive is present in ~/.ssh/config.
    Also cleans up any legacy managed section on first encounter.
    """
    ssh_config = config.user.ssh_config
    config_d = ssh_config.parent / _CONFIG_DIR_NAME
    config_d.mkdir(parents=True, exist_ok=True)
    prefix = config.user.ssh_host_prefix

    # Ensure Include directive at top of ssh_config
    _ensure_include(ssh_config)

    # Clean up legacy managed section if present
    _remove_legacy_section(ssh_config)

    # Build all Host blocks from DB
    blocks: list[str] = ["# Managed by agentworks -- do not edit.\n"]
    for vm in db.list_vms():
        if not vm.tailscale_host:
            continue
        alias = ssh_host_alias(vm.name, prefix)
        blocks.append(_format_entry(
            alias=alias,
            hostname=vm.tailscale_host,
            user=vm.vm_user,
            identity_file=config.user.ssh_private_key,
        ))

    conf_path = config_d / _MANAGED_CONF
    if len(blocks) > 1:
        _atomic_write(conf_path, "\n".join(blocks))
    elif conf_path.exists():
        conf_path.unlink()


def _ensure_include(ssh_config: Path) -> None:
    """Ensure the Include directive is the first non-empty line in ssh_config."""
    ssh_config.parent.mkdir(parents=True, exist_ok=True)
    directive = _include_directive(ssh_config)

    include_block = f"{_INCLUDE_COMMENT}\n{directive}"

    if not ssh_config.exists():
        ssh_config.write_text(f"{include_block}\n")
        return

    content = ssh_config.read_text()
    if directive in content:
        return  # already present (with or without comment)

    # Insert at top
    ssh_config.write_text(f"{include_block}\n\n{content}")


def _remove_legacy_section(ssh_config: Path) -> None:
    """Remove the legacy managed section from ssh_config if present."""
    if not ssh_config.exists():
        return

    content = ssh_config.read_text()
    marker_idx = content.find(_LEGACY_MARKER)
    if marker_idx == -1:
        return

    # Keep everything before the marker
    user_section = content[:marker_idx].rstrip("\n")
    directive = _include_directive(ssh_config)
    if user_section:
        ssh_config.write_text(user_section + "\n")
    else:
        ssh_config.write_text(f"{directive}\n")


# -- Legacy approach (managed section in ssh_config) -----------------------


def _legacy_upsert(config: Config, vm: VMRow) -> None:
    """Legacy: add/update entry in managed section of ssh_config."""
    if not vm.tailscale_host:
        return

    ssh_config = config.user.ssh_config
    user_section, entries = _read_managed(ssh_config)
    prefix = config.user.ssh_host_prefix

    alias = ssh_host_alias(vm.name, prefix)
    entries[alias] = _format_entry(
        alias=alias,
        hostname=vm.tailscale_host,
        user=vm.vm_user,
        identity_file=config.user.ssh_private_key,
    )

    _write_legacy(ssh_config, user_section, entries)


def _legacy_remove(config: Config, vm_name: str) -> None:
    """Legacy: remove entry from managed section of ssh_config."""
    ssh_config = config.user.ssh_config
    if not ssh_config.exists():
        return

    user_section, entries = _read_managed(ssh_config)
    alias = ssh_host_alias(vm_name, config.user.ssh_host_prefix)
    if alias not in entries:
        return

    del entries[alias]
    _write_legacy(ssh_config, user_section, entries)


# -- Shared helpers --------------------------------------------------------


def _format_entry(
    alias: str,
    hostname: str,
    user: str,
    identity_file: Path,
) -> str:
    """Format a single SSH config Host block."""
    # Use forward slashes (POSIX-style) and quote if path contains spaces
    id_str = identity_file.as_posix()
    if " " in id_str:
        id_str = f'"{id_str}"'
    return (
        f"Host {alias}\n"
        f"    HostName {hostname}\n"
        f"    User {user}\n"
        f"    IdentityFile {id_str}\n"
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically via temp file + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(fd, "w") as f:
            f.write(content)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_managed(ssh_config: Path) -> tuple[str, dict[str, str]]:
    """Read the SSH config, splitting into user section and managed entries."""
    entries: dict[str, str] = {}

    if not ssh_config.exists():
        return "", entries

    content = ssh_config.read_text()
    marker_idx = content.find(_LEGACY_MARKER)

    if marker_idx == -1:
        return content, entries

    user_section = content[:marker_idx]
    managed_section = content[marker_idx + len(_LEGACY_MARKER) :]

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


def _write_legacy(
    ssh_config: Path,
    user_section: str,
    entries: dict[str, str],
) -> None:
    """Write the SSH config file with user section + managed section."""
    ssh_config.parent.mkdir(parents=True, exist_ok=True)

    parts = [user_section.rstrip("\n")]

    if entries:
        parts.append("")
        parts.append(_LEGACY_MARKER)
        for block in entries.values():
            parts.append(block.rstrip("\n"))

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"

    ssh_config.write_text(content)
