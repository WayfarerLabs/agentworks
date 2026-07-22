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

from agentworks import output

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database

_LEGACY_MARKER = "# --- Managed by agentworks. Do not edit below this line. ---"
_INCLUDE_COMMENT = "# Added by agentworks"
_CONFIG_DIR_NAME = "config.d"
_MANAGED_CONF = "agentworks.conf"


def _to_ssh_path(path: Path) -> str:
    """Convert a Path to an SSH config-safe string.

    Uses ~ for the home directory prefix and forward slashes on all platforms
    since OpenSSH expects POSIX-style paths even on Windows.
    """
    resolved = path.resolve()
    posix = resolved.as_posix()
    home = Path.home().resolve().as_posix()
    # Check with trailing slash to avoid false matches (e.g. /home/user2)
    if posix == home or posix.startswith(home + "/"):
        posix = "~" + posix[len(home) :]
    return posix


def _include_directive(ssh_config: Path) -> str:
    """Build the Include directive for config.d."""
    config_d = ssh_config.parent / _CONFIG_DIR_NAME
    return f"Include {_to_ssh_path(config_d)}/*"


def ssh_host_alias(vm_name: str, prefix: str = "awvm--") -> str:
    """Return the SSH host alias for a VM."""
    return f"{prefix}{vm_name}"


def ssh_agent_alias(agent_name: str, prefix: str = "awagent--") -> str:
    """Return the SSH host alias for an agent.

    Keyed on the operator-facing ``agent.name`` rather than on the
    underlying Linux user, since the Linux-user shape (``agt-<name>``,
    legacy ``agt--<name>``, etc.) is an implementation detail operators
    shouldn't have to remember. Globally unique because ``agents.name``
    is the primary key on the agents table; the VM the agent lives on is
    looked up by SSH config via the per-agent block's ``HostName``.
    """
    return f"{prefix}{agent_name}"


def sync_ssh_config(config: Config, db: Database) -> None:
    """Rebuild SSH config from current DB state."""
    if config.operator.ssh_config_dir:
        _rebuild_config_dir(config, db)
    else:
        _legacy_rebuild(config, db)
    output.info("SSH config synced")


def _legacy_rebuild(config: Config, db: Database) -> None:
    """Legacy: rebuild the managed section from all VMs in DB.

    Emits the same per-VM admin block + per-agent blocks as
    ``_rebuild_config_dir``; only the file layout differs.
    """
    ssh_config = config.operator.ssh_config
    user_section, _old_entries = _read_managed(ssh_config)
    prefix = config.operator.ssh_host_prefix
    agent_prefix = config.operator.ssh_agent_host_prefix

    host_blocks: list[str] = []
    for vm in db.list_vms():
        if not vm.tailscale_host:
            continue
        host_blocks.append(
            _format_entry(
                alias=ssh_host_alias(vm.name, prefix),
                hostname=vm.tailscale_host,
                user=vm.admin_username,
                identity_file=config.operator.ssh_private_key,
            )
        )
        # Per-agent aliases on this VM (parity with _rebuild_config_dir).
        for agent in db.list_agents(vm_name=vm.name):
            host_blocks.append(
                _format_entry(
                    alias=ssh_agent_alias(agent.name, agent_prefix),
                    hostname=vm.tailscale_host,
                    user=agent.linux_user,
                    identity_file=config.operator.ssh_private_key,
                )
            )

    _write_legacy(ssh_config, user_section, host_blocks)


# -- config.d approach -----------------------------------------------------


def _managed_conf_name(db: Database) -> str:
    """Managed-file name: ``agentworks-{system_slug}.conf`` when the
    install has a slug, the legacy ``agentworks.conf`` otherwise. The
    existing ``Include ~/.ssh/config.d/*`` directive matches both.
    """
    from agentworks.db import SYSTEM_SLUG_KEY

    slug = db.get_setting(SYSTEM_SLUG_KEY) or None
    return f"agentworks-{slug}.conf" if slug else _MANAGED_CONF


def _rebuild_config_dir(config: Config, db: Database) -> None:
    """Declaratively rebuild the managed config.d file from DB state.

    Writes a single file containing Host blocks for all VMs with Tailscale
    hosts. Ensures the Include directive is present in ~/.ssh/config.
    Also cleans up any legacy managed section on first encounter.
    """
    ssh_config = config.operator.ssh_config
    config_d = ssh_config.parent / _CONFIG_DIR_NAME
    config_d.mkdir(parents=True, exist_ok=True)
    prefix = config.operator.ssh_host_prefix
    agent_prefix = config.operator.ssh_agent_host_prefix

    # Ensure Include directive at top of ssh_config
    _ensure_include(ssh_config)

    # Clean up legacy managed section if present
    _remove_legacy_section(ssh_config)

    # Build all Host blocks from DB
    blocks: list[str] = ["# Managed by agentworks -- do not edit.\n"]
    host_entries: list[str] = []
    for vm in db.list_vms():
        if not vm.tailscale_host:
            continue
        vm_alias = ssh_host_alias(vm.name, prefix)
        # Admin alias for this VM.
        host_entries.append(
            _format_entry(
                alias=vm_alias,
                hostname=vm.tailscale_host,
                user=vm.admin_username,
                identity_file=config.operator.ssh_private_key,
            )
        )
        # Per-agent aliases on this VM. Same HostName / IdentityFile as the
        # VM block; only User and the alias differ. The alias is a
        # top-level ``<agent_prefix><agent.name>`` (not nested under the
        # VM alias) because agents belong to exactly one VM and the
        # operator-facing handle is the agent name, not the Linux user.
        for agent in db.list_agents(vm_name=vm.name):
            host_entries.append(
                _format_entry(
                    alias=ssh_agent_alias(agent.name, agent_prefix),
                    hostname=vm.tailscale_host,
                    user=agent.linux_user,
                    identity_file=config.operator.ssh_private_key,
                )
            )

    conf_name = _managed_conf_name(db)
    conf_path = config_d / conf_name
    # First sync after the slug is set (the slug arrives at first
    # vm create, not at DB migration): remove the old slug-less file so
    # stale aliases cannot shadow fresh ones. Idempotent on later syncs.
    if conf_name != _MANAGED_CONF:
        (config_d / _MANAGED_CONF).unlink(missing_ok=True)

    if not host_entries:
        conf_path.unlink(missing_ok=True)
        return

    blocks.extend(host_entries)
    _atomic_write(conf_path, "\n".join(blocks))


def _ensure_include(ssh_config: Path) -> None:
    """Ensure the Include directive is present in ssh_config.

    Adds it at the top if not present. If the user has moved it elsewhere
    in the file, their placement is respected.
    """
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


# -- Shared helpers --------------------------------------------------------


def _format_entry(
    alias: str,
    hostname: str,
    user: str,
    identity_file: Path,
) -> str:
    """Format a single SSH config Host block."""
    id_str = _to_ssh_path(identity_file)
    if " " in id_str:
        id_str = f'"{id_str}"'
    return f"Host {alias}\n    HostName {hostname}\n    User {user}\n    IdentityFile {id_str}\n"


def _atomic_write(path: Path, content: str) -> None:
    """Write content to a file atomically via temp file + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_managed(ssh_config: Path) -> tuple[str, dict[str, str]]:
    """Read the SSH config, splitting into operator-managed section and auto-managed entries."""
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
    host_blocks: list[str],
) -> None:
    """Write the SSH config file with operator-managed section + agentworks-managed section.

    ``host_blocks`` are the per-alias Host blocks (VMs and agents).
    """
    ssh_config.parent.mkdir(parents=True, exist_ok=True)

    parts = [user_section.rstrip("\n")]

    if host_blocks:
        parts.append("")
        parts.append(_LEGACY_MARKER)
        for block in host_blocks:
            parts.append(block.rstrip("\n"))

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"

    ssh_config.write_text(content)
