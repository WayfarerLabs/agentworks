"""Manage SSH config entries for agentworks VMs.

When ssh_config_dir is enabled (default), all VM Host blocks are written to
a single ~/.ssh/config.d/agentworks.conf file and an Include directive is
added to the top of ~/.ssh/config. When disabled, entries are kept in a
managed section at the end of ~/.ssh/config (legacy behavior).

On first run with ssh_config_dir enabled, any legacy managed section is
cleaned up automatically.
"""

from __future__ import annotations

import errno
import re
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.atomic_write import atomic_write_text
from agentworks.errors import AgentworksError, AuthorizationError, ConfigError
from agentworks.execution import trust
from agentworks.path_rendering import format_host_path

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

    Deliberately NOT ``path_rendering.format_host_path``, despite the
    overlapping ``~`` behaviour. That one renders prose for a human; this
    one generates a token for ``ssh`` to parse, which is why it resolves
    symlinks first and forces POSIX separators on Windows. Two callers
    with genuinely different output contracts, so folding them together
    would mean one of them silently getting the other's rules.
    """
    resolved = path.resolve()
    posix = resolved.as_posix()
    home = Path.home().resolve().as_posix()
    # Check with trailing slash to avoid false matches (e.g. /home/user2)
    if posix == home or posix.startswith(home + "/"):
        posix = "~" + posix[len(home) :]
    return posix


# Glob characters, which make an Include pattern match other directories or
# none, so a directory holding one cannot be named by it.
_GLOB_CHARACTERS = frozenset("*?[")


def _include_directive(ssh_config: Path) -> str:
    """The Include directive for config.d, or a :class:`ConfigError` when ssh would misread its path.

    The pattern is written through :func:`trust.option_value`, so a directory
    with a space or an apostrophe stays one pattern. From OpenSSH 9.9, Include
    expands ``%`` tokens and environment variables, so the directory must pass
    :func:`trust.literal_path`, the rule for every path ssh is given, and hold
    no glob character.
    """
    # Resolved, as the directive has always been written, so a line an
    # earlier rebuild added still matches.
    config_d = (ssh_config.parent / _CONFIG_DIR_NAME).resolve()
    trust.literal_path(config_d, "SSH config directory")
    if any(char in _GLOB_CHARACTERS for char in config_d.as_posix()):
        raise ConfigError(
            f"SSH config directory '{format_host_path(config_d)}' holds '*', '?' or '[', "
            "which an Include pattern cannot name literally"
        )
    return f"Include {trust.option_value(_to_ssh_path(config_d) + '/*')}"


def _earlier_include(ssh_config: Path) -> str:
    """The Include line releases before 0.20 wrote: the same pattern, never quoted, whatever the directory held.

    :func:`_replace_earlier_include` finds it only where those releases wrote it.
    """
    return f"Include {_to_ssh_path(ssh_config.parent / _CONFIG_DIR_NAME)}/*"


def _replace_earlier_include(content: str, earlier: str, replacement: str | None) -> str:
    """``content`` with ``earlier`` replaced by ``replacement`` where an earlier release wrote it.

    Those releases wrote it in two shapes only, and only those are theirs: the
    ``# Added by agentworks`` line directly followed by it, at the start of a
    line, and a file holding nothing but it (left by removing the legacy
    section). The same line anywhere else may be the operator's own and is left
    alone. For ``None`` the line is removed instead, with its marker, and a file
    that held only the line is left empty. Those releases wrote in text mode, so
    every newline, one in the directory name included, is LF, or CRLF on
    Windows; the text is matched whole, as the line can span physical lines.
    """
    shapes = [(newline, earlier.replace("\n", newline)) for newline in ("\n", "\r\n")]
    # The whole-file shape is judged on the file as found, before any marked
    # line is removed from it.
    for newline, line in shapes:
        if content in (line, line + newline):
            return "" if replacement is None else replacement + content[len(line) :]
    for newline, line in shapes:
        block = f"{_INCLUDE_COMMENT}{newline}{line}{newline}"
        kept = "" if replacement is None else f"{_INCLUDE_COMMENT}{newline}{replacement}{newline}"
        content = _replace_at_line_starts(content, block, kept)
    return content


def _replace_at_line_starts(content: str, old: str, new: str) -> str:
    """``content`` with each ``old`` that starts a line replaced by ``new``, both taken literally."""
    return re.sub(f"(?m)^{re.escape(old)}", lambda _: new, content)


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


def sync_ssh_config(config: Config, db: Database, *, announce: bool = True) -> None:
    """Rebuild SSH config from current DB state.

    Emits the "SSH config synced" line by default. Pass ``announce=False``
    for a silent re-sync that still writes the file for correctness but
    adds no output line (e.g. ``vm create``'s post-initialization sync,
    which would otherwise duplicate the line already emitted as the last
    step of the Provisioning phase).
    """
    if config.operator.ssh_config_dir:
        _rebuild_config_dir(config, db)
    else:
        _legacy_rebuild(config, db)
    if announce:
        output.info("SSH config synced")


def sync_ssh_config_or_warn(config: Config, db: Database, *, announce: bool = True) -> None:
    """:func:`sync_ssh_config`, warning instead of raising.

    For a command whose VM work already succeeded: a failed write to the
    local ``~/.ssh/config`` says nothing about the VM's health, so it does not
    fail the command. The warning carries the error's hint, which is how to
    fix the write.
    """
    try:
        sync_ssh_config(config, db, announce=announce)
    except Exception as e:
        message = str(e)
        if isinstance(e, AgentworksError) and e.hint:
            message += f"{'' if message.endswith('.') else '.'} {e.hint}"
        output.warn(f"SSH config sync failed: {message}")
        output.info("VM is likely still usable.")


def _legacy_rebuild(config: Config, db: Database) -> None:
    """Legacy: rebuild the managed section from all VMs in DB.

    Emits the same per-VM admin block + per-agent blocks as
    ``_rebuild_config_dir``; only the file layout differs.
    """
    ssh_config = config.operator.ssh_config
    user_section = _read_user_section(ssh_config)
    prefix = config.operator.ssh_host_prefix
    agent_prefix = config.operator.ssh_agent_host_prefix

    identity = _identity_value(config.operator.ssh_private_key)
    host_blocks: list[str] = []
    for vm in db.list_vms():
        if not vm.tailscale_host:
            continue
        host_blocks.append(
            _format_entry(
                alias=ssh_host_alias(vm.name, prefix),
                hostname=vm.tailscale_host,
                user=vm.admin_username,
                identity=identity,
                vm_name=vm.name,
            )
        )
        # Per-agent aliases on this VM (parity with _rebuild_config_dir).
        for agent in db.list_agents(vm_name=vm.name):
            host_blocks.append(
                _format_entry(
                    alias=ssh_agent_alias(agent.name, agent_prefix),
                    hostname=vm.tailscale_host,
                    user=agent.linux_user,
                    identity=identity,
                    vm_name=vm.name,
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

    # Clean up legacy managed section if present, then ensure the Include
    # directive, so a directive inside that section is restored, not lost.
    _remove_legacy_section(ssh_config)
    _sync_include(ssh_config)

    # Build all Host blocks from DB
    blocks: list[str] = ["# Managed by agentworks -- do not edit.\n"]
    identity = _identity_value(config.operator.ssh_private_key)
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
                identity=identity,
                vm_name=vm.name,
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
                    identity=identity,
                    vm_name=vm.name,
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
    atomic_write_text(conf_path, "\n".join(blocks))


def _sync_include(ssh_config: Path) -> None:
    """Ensure the Include directive, or, when ssh would misread the directory, warn once that none is written.

    In that case :func:`_remove_earlier_include` removes the line an earlier
    release wrote for the same directory, where that release wrote it.
    Unquoted, that line can name a different or wider directory than the
    config directory (a glob character also matches its siblings, and from
    OpenSSH 9.9 ``%`` and ``$`` expand), and a double quote or a line break in
    it makes ssh reject the whole file, every host with it.
    """
    try:
        directive = _include_directive(ssh_config)
    except ConfigError as e:
        removed = _remove_earlier_include(ssh_config)
        removal = f", and the line an earlier release wrote for it, '{removed}', removed" if removed else ""
        output.warn(
            f"Include directive not written to {format_host_path(ssh_config)}{removal}, so ssh reads the managed "
            f"entries only through an Include already there: {e}"
        )
        return
    _ensure_include(ssh_config, directive)


def _ensure_include(ssh_config: Path, directive: str) -> None:
    """Ensure ``directive`` is present in ssh_config.

    Adds it at the top if no line holds it. If the user has moved it
    elsewhere in the file, their placement is respected, and a line that is
    the directive commented out (``# Include ...``, quoted or not) is the
    operator's opt-out: it counts as present and is left as it is. The file
    keeps its line endings.
    """
    ssh_config.parent.mkdir(parents=True, exist_ok=True)
    content = _read_user_config(ssh_config)
    if content is None:
        _write_user_config(ssh_config, f"{_INCLUDE_COMMENT}\n{directive}\n")
        return

    # Split at LF alone, the only line break ssh reads: str.splitlines also
    # breaks at characters such as U+2028 that a path may hold.
    lines = content.split("\n")
    # Earlier releases wrote the pattern unquoted. With an apostrophe in it,
    # ssh rejects the whole file, so a correct line beside it would fix
    # nothing; with whitespace, the line names nothing. Rewrite what they
    # wrote where it stands.
    unquoted = _earlier_include(ssh_config)
    rewritten = _replace_earlier_include(content, unquoted, directive)
    opted_out = any(_commented_out(line) in (directive, unquoted) for line in lines)
    if opted_out or any(line.strip() == directive for line in rewritten.split("\n")):
        if rewritten != content:
            _write_user_config(ssh_config, rewritten)
        return

    newline = "\r\n" if "\r\n" in content else "\n"
    _write_user_config(ssh_config, f"{_INCLUDE_COMMENT}{newline}{directive}{newline}{newline}{content}")


def _commented_out(line: str) -> str | None:
    """What ``line`` comments out, or ``None`` when it is not a comment."""
    stripped = line.strip()
    return stripped[1:].strip() if stripped.startswith("#") else None


def _remove_earlier_include(ssh_config: Path) -> str | None:
    """Remove :func:`_earlier_include` where an earlier release wrote it.

    Returns the removed line, or ``None`` when it was not there.
    """
    content = _read_user_config(ssh_config)
    if content is None:
        return None
    earlier = _earlier_include(ssh_config)
    kept = _replace_earlier_include(content, earlier, None)
    if kept == content:
        return None
    _write_user_config(ssh_config, kept)
    return earlier


def _remove_legacy_section(ssh_config: Path) -> None:
    """Remove the legacy managed section from ssh_config if present."""
    content = _read_user_config(ssh_config)
    if content is None:
        return
    marker_idx = content.find(_LEGACY_MARKER)
    if marker_idx == -1:
        return

    # Keep everything before the marker
    user_section = content[:marker_idx].rstrip("\r\n")
    newline = "\r\n" if "\r\n" in content else "\n"
    _write_user_config(ssh_config, user_section + newline if user_section else "")


def _read_user_config(ssh_config: Path) -> str | None:
    """The operator's SSH config with its line endings as written, or ``None`` when there is no such file.

    Bytes that are not UTF-8 decode to surrogates, which
    :func:`_write_user_config` writes back unchanged. A file this user may not
    read, or may not reach through a directory it may not search, is refused
    with :class:`AuthorizationError`, as one it may not write is. Opening it is
    the only check, so no separate existence test can raise first.
    """
    try:
        f = open(ssh_config, encoding="utf-8", errors="surrogateescape", newline="")  # noqa: SIM115
    except FileNotFoundError:
        return None
    except PermissionError as error:
        raise _refusal("read", ssh_config.resolve(), error, "readable and writable") from error
    with f:
        return f.read()


def _write_user_config(ssh_config: Path, content: str) -> None:
    """Write the operator's SSH config in place, through a symlink to it, line endings and undecodable bytes as given.

    Editing the file rather than replacing it keeps everything the operator
    set on it: owner, group, mode, ACL, inode and hard links, as earlier
    releases did. The write is not atomic. A file this user may not write is
    refused with :class:`AuthorizationError` before anything is truncated.
    """
    target = ssh_config.resolve()
    # Only a refused open is the operator's refusal, so the with statement
    # that closes the file stays outside the try.
    try:
        f = open(target, "w", encoding="utf-8", errors="surrogateescape", newline="")  # noqa: SIM115
    except OSError as error:
        if not isinstance(error, PermissionError) and error.errno != errno.EROFS:
            raise
        raise _refusal("update", target, error, "writable") from error
    with f:
        f.write(content)


def _refusal(action: str, target: Path, error: OSError, fix: str) -> AuthorizationError:
    """The error for an ``action`` on the operator's SSH config that the system refused, with how to lift it."""
    return AuthorizationError(
        f"cannot {action} '{format_host_path(target)}': {error.strerror}",
        hint=f"Make it {fix}, then run 'agw config sync-ssh-config'.",
    )


# -- Legacy approach (managed section in ssh_config) -----------------------


# -- Shared helpers --------------------------------------------------------


def _trust_options(vm_name: str) -> tuple[str, ...]:
    """The VM's store options, or none with a warning when its record cannot be
    read or its store path cannot be written into an SSH config. One VM's store
    problem never aborts the rebuild for every VM."""
    try:
        return trust.entry_options(vm_name)
    except AgentworksError as e:
        output.warn(f"Host-key options left out of the SSH config entries for VM '{vm_name}': {e}")
        return ()


def _identity_value(identity_file: Path) -> str | None:
    """The operator's key as an ``IdentityFile`` value: the configured path, as the execution core passes it.

    The configured path, not its symlink target, must pass
    :func:`trust.literal_path`, so both accept or refuse the same key, and it
    is written absolute, so ssh reads the same file the core does whatever its
    own idea of ``~``. A path with ``%``, ``$``, a double quote, a backslash or
    a control character, which ssh would expand or misread, is left out of the
    entries with a warning (``None``) rather than written as a line that names
    another file. Computed once per rebuild, so the warning appears once.
    """
    try:
        path = trust.literal_path(identity_file, "SSH identity file")
    except ConfigError as e:
        output.warn(
            f"IdentityFile left out of the SSH config entries: {e}. Your own ssh offers its default keys "
            "for those aliases instead; move the key to a plain path."
        )
        return None
    return trust.option_value(path)


def _format_entry(
    alias: str,
    hostname: str,
    user: str,
    identity: str | None,
    vm_name: str,
) -> str:
    """Format a single SSH config Host block for an entry on the VM ``vm_name``.

    ``identity`` is the ``IdentityFile`` value from :func:`_identity_value`;
    ``None`` leaves the line out. A VM with a host-key record gets the options
    that make the operator's own ``ssh`` verify against the store; one without
    is left as it was.
    """
    identity_line = f"    IdentityFile {identity}\n" if identity is not None else ""
    trust_lines = "".join(f"    {option}\n" for option in _trust_options(vm_name))
    return f"Host {alias}\n    HostName {hostname}\n    User {user}\n{identity_line}{trust_lines}"


def _read_user_section(ssh_config: Path) -> str:
    """The operator's part of the SSH config: everything before the legacy managed section."""
    content = _read_user_config(ssh_config) or ""
    marker_idx = content.find(_LEGACY_MARKER)
    return content if marker_idx == -1 else content[:marker_idx]


def _write_legacy(
    ssh_config: Path,
    user_section: str,
    host_blocks: list[str],
) -> None:
    """Write the SSH config file with operator-managed section + agentworks-managed section.

    ``host_blocks`` are the per-alias Host blocks (VMs and agents). The
    managed section takes the line endings the operator's section uses.
    """
    ssh_config.parent.mkdir(parents=True, exist_ok=True)
    newline = "\r\n" if "\r\n" in user_section else "\n"

    parts = [user_section.rstrip("\r\n")]

    if host_blocks:
        parts.append("")
        parts.append(_LEGACY_MARKER)
        for block in host_blocks:
            parts.append(block.rstrip("\n").replace("\n", newline))

    content = newline.join(parts)
    if not content.endswith(newline):
        content += newline

    _write_user_config(ssh_config, content)
