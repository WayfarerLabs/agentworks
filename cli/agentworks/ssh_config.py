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

# ControlMaster multiplexes subsequent SSH calls to the same (user, host) over
# the master's existing channel, dropping per-call latency from ~150-300ms
# (fresh handshake) to ~20-50ms. ``vm create`` / ``vm reinit`` / ``agent
# create`` / ``agent reinit`` each issue 30+ sequential SSH calls; the
# savings are several seconds per operation.
#
# The block keys on the VM's Tailscale IP, not the ``awvm--*`` / ``awagent--*``
# aliases, because every agentworks-internal SSH call uses the IP directly
# (``SSHTransport.host`` is ``vm.tailscale_host``). ``Host`` patterns match
# the command-line argument literally; an alias wildcard would only help
# operators who type ``ssh awvm--myvm`` interactively, not the bulk of the
# SSH traffic this is meant to speed up. The aliases stay an operator
# convenience and don't carry ControlMaster directives.
#
# ``%C`` is a stable hash of (local-user, hostname, port, remote-user);
# requires OpenSSH 6.7+ (every supported platform). The path is namespaced
# (``agentworks-cm-``) so it cannot collide with a pre-existing operator
# ControlMaster setup. ``ControlPersist 60s`` covers a single reinit without
# letting NAT-idle-kill churn the connection.
#
# If the master socket can't bind (read-only ``~/.ssh``, weird mounts), OpenSSH
# falls back transparently to a fresh handshake per call -- no regression.
# Operators who want different settings can layer their own ``Host *`` block
# above the agentworks ``Include`` directive; ssh_config's first-match-wins
# semantics apply.
# OpenSSH expands ``~`` client-side at connect time; no need to route this
# literal through ``_to_ssh_path`` like the IdentityFile paths.
_CONTROL_PATH = "~/.ssh/agentworks-cm-%C"
_CONTROL_PERSIST = "60s"  # OpenSSH accepts a unit suffix; the ``s`` makes it self-documenting.


def _format_ip_controlmaster_block(tailscale_host: str) -> str:
    """Return a ``Host <tailscale_ip>`` ControlMaster block.

    One block per unique VM IP; admin and agents on the same VM share one
    block because their SSH calls hit the same IP and ``%C`` keys on the
    remote user, so separate master sockets per user happen automatically.
    """
    return (
        f"Host {tailscale_host}\n"
        "    ControlMaster auto\n"
        f"    ControlPath {_CONTROL_PATH}\n"
        f"    ControlPersist {_CONTROL_PERSIST}\n"
    )


def _controlmaster_supported() -> bool:
    """Whether the local OpenSSH client supports ControlMaster reliably.

    Returns ``False`` on Windows. Windows OpenSSH implements ControlMaster
    via named pipes instead of Unix sockets, and the multiplex code path
    has long-standing bugs around ``getsockname`` calls against pipe
    handles. The symptom is ``getsockname failed: Not a socket`` followed
    by a hang on every IP-form ssh: alias-form ssh works because it never
    matches a ``Host`` block with ``ControlMaster``. Operators on Windows
    therefore get today's per-call fresh-handshake behavior (no regression
    from pre-#113); Linux and macOS operators get the multiplex speedup.

    Sniffing the OpenSSH version on the client would let us re-enable on
    a fixed Windows build, but the bug has been present across every
    Windows OpenSSH release we've observed. Keep it simple: ``win32`` off,
    everything else on. Flip when Microsoft ships a verified fix.
    """
    import sys

    return sys.platform != "win32"


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
    output.detail("SSH config synced")


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
    cm_blocks: list[str] = []
    seen_ips: set[str] = set()
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
        # One IP-keyed ControlMaster block per VM. Admin and agents on the
        # same VM share it because their SSH calls hit the same IP and
        # ``%C`` keys on the remote user. Skipped on Windows -- see
        # ``_controlmaster_supported``.
        if _controlmaster_supported() and vm.tailscale_host not in seen_ips:
            seen_ips.add(vm.tailscale_host)
            cm_blocks.append(_format_ip_controlmaster_block(vm.tailscale_host))

    _write_legacy(ssh_config, user_section, host_blocks, cm_blocks)


# -- config.d approach -----------------------------------------------------


def _rebuild_config_dir(config: Config, db: Database) -> None:
    """Declaratively rebuild ~/.ssh/config.d/agentworks.conf from DB state.

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
    cm_entries: list[str] = []
    seen_ips: set[str] = set()
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
        # One IP-keyed ControlMaster block per VM. Admin and agents on the
        # same VM share it because their SSH calls hit the same IP and
        # ``%C`` keys on the remote user, so each user still gets its own
        # master socket automatically. Skipped on Windows -- see
        # ``_controlmaster_supported``.
        if _controlmaster_supported() and vm.tailscale_host not in seen_ips:
            seen_ips.add(vm.tailscale_host)
            cm_entries.append(_format_ip_controlmaster_block(vm.tailscale_host))

    conf_path = config_d / _MANAGED_CONF
    if not host_entries:
        conf_path.unlink(missing_ok=True)
        return

    blocks.extend(host_entries)
    blocks.extend(cm_entries)
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
    cm_blocks: list[str] | None = None,
) -> None:
    """Write the SSH config file with operator-managed section + agentworks-managed section.

    ``host_blocks`` are the per-alias Host blocks (VMs and agents);
    ``cm_blocks`` are the per-VM-IP ControlMaster blocks (one per
    unique Tailscale IP). They're written in that order so the file
    layout mirrors ``_rebuild_config_dir``: all aliases first, then
    all ControlMaster blocks.
    """
    ssh_config.parent.mkdir(parents=True, exist_ok=True)

    parts = [user_section.rstrip("\n")]

    if host_blocks:
        parts.append("")
        parts.append(_LEGACY_MARKER)
        for block in host_blocks:
            parts.append(block.rstrip("\n"))
        for block in cm_blocks or []:
            parts.append(block.rstrip("\n"))

    content = "\n".join(parts)
    if not content.endswith("\n"):
        content += "\n"

    ssh_config.write_text(content)
