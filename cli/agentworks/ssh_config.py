"""Manage SSH config entries for agentworks VMs.

When ssh_config_dir is enabled (default), all VM Host blocks are written to
a single ~/.ssh/config.d/agentworks.conf file and an Include directive is
added to the top of ~/.ssh/config. When disabled, entries are kept in a
managed section at the end of ~/.ssh/config (legacy behavior).

On first run with ssh_config_dir enabled, any legacy managed section is
cleaned up automatically.

Every VM entry verifies the VM's host key against Agentworks' host-key store
(``agentworks.execution.trust``), so the operator's own ``ssh`` trusts exactly
what Agentworks' connections trust and refuses a VM with no recorded key.
"""

from __future__ import annotations

import errno
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.atomic_write import atomic_write_text
from agentworks.errors import AgentworksError, AuthorizationError, ConfigError, ValidationError
from agentworks.execution import ssh_process, trust
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import Database, VMRow

_LEGACY_MARKER = "# --- Managed by agentworks. Do not edit below this line. ---"
_INCLUDE_COMMENT = "# Added by agentworks"
_CONFIG_DIR_NAME = "config.d"
_MANAGED_CONF = "agentworks.conf"

# On Windows the ssh shipped with Git for Windows may read the managed entries
# too, and it accepts neither the quoted nor the bare form of a value with an
# apostrophe and no whitespace.
_GIT_FOR_WINDOWS_MAY_READ = sys.platform == "win32"


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

    Only the line directly below a ``# Added by agentworks`` line, each at the
    start of a line, is theirs. They left the bare line alone in a file only
    where the operator had already written it, so the same line anywhere
    without the marker is the operator's own and is left alone. A run of
    markers directly above the line is one block: the replacement keeps the
    run as written and replaces the line, and for ``None`` the run goes with
    the line, so no marker of the run is left above anything. Where the
    directory name holds a line break, an operator's own copies of the line's
    halves written around a block can join into another block, which is
    removed too.

    Those releases wrote in text mode, so every newline, one in the directory
    name included, is LF, or CRLF on Windows; the text is matched whole, as the
    line can span physical lines. They also read in universal-newline mode, so
    a CR in the directory name came back as LF and the next sync prepended the
    line again: the LF form counts with CR and CRLF read as LF, too.
    """
    universal = earlier.replace("\r\n", "\n").replace("\r", "\n")
    shapes = [("\n", earlier), ("\n", universal), ("\r\n", earlier.replace("\n", "\r\n"))]
    marker = re.escape(_INCLUDE_COMMENT)

    def kept(block: re.Match[str]) -> str:
        """The run of markers with ``replacement`` in place of the line, or nothing for ``None``."""
        return "" if replacement is None else f"{block['markers']}{replacement}{block['newline']}"

    for newline, line in shapes:
        # The markers above the one directly above the line may end in either
        # newline; that one ends in the line's own.
        run = rf"(?P<markers>(?:{marker}\r?\n)*{marker}(?P<newline>{re.escape(newline)}))"
        content = re.sub(rf"(?m)^{run}{re.escape(line + newline)}", kept, content)
    return content


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
        output.warn(f"SSH config sync failed: {_with_hint(e)}")
        output.info("VM is likely still usable.")


def _legacy_rebuild(config: Config, db: Database) -> None:
    """Legacy: rebuild the managed section from all VMs in DB.

    Emits the same per-VM admin block + per-agent blocks as
    ``_rebuild_config_dir``; only the file layout differs.
    """
    ssh_config = config.operator.ssh_config
    user_section = _read_user_section(ssh_config)
    _write_legacy(ssh_config, user_section, _host_entries(config, db))


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
    _make_directory(config_d)

    # Clean up legacy managed section if present, then ensure the Include
    # directive, so a directive inside that section is restored, not lost.
    _remove_legacy_section(ssh_config)
    _sync_include(ssh_config)

    # Build all Host blocks from DB
    blocks: list[str] = ["# Managed by agentworks -- do not edit.\n"]
    host_entries = _host_entries(config, db)

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
    _make_directory(ssh_config.parent)
    content = _read_user_config(ssh_config)
    if content is None:
        _write_user_config(ssh_config, f"{_INCLUDE_COMMENT}\n{directive}\n")
        return

    # Earlier releases wrote the pattern unquoted. With an apostrophe in it,
    # ssh rejects the whole file, so a correct line beside it would fix
    # nothing; with whitespace, the line names nothing. Rewrite what they
    # wrote where it stands.
    unquoted = _earlier_include(ssh_config)
    rewritten = _replace_earlier_include(content, unquoted, directive)
    # Split at LF alone, the only line break ssh reads: str.splitlines also
    # breaks at characters such as U+2028 that a path may hold.
    lines = rewritten.split("\n")
    opted_out = any(_commented_out(line) in (directive, unquoted) for line in lines)
    if opted_out or any(line.strip() == directive for line in lines):
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
        raise _refusal("read", ssh_config.resolve(), error, "it readable and writable") from error
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
        if not _refused(error):
            raise
        raise _refusal("update", target, error, "it writable") from error
    with f:
        f.write(content)


def _make_directory(directory: Path) -> None:
    """Create ``directory`` and its parents as needed, refused as a write is when the system will not.

    The error names the directory the failing call was given, which may be a parent.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        if not _refused(error):
            raise
        # The error Path.mkdir raises, from os.mkdir or from the os.stat in
        # is_dir, names the directory it was raised for.
        refused = Path(error.filename)
        fix = "its parent directory writable and every directory above it searchable"
        raise _refusal("create", refused.resolve(), error, fix) from error


def _refused(error: OSError) -> bool:
    """Whether ``error`` is the system refusing this user, not a fault: no permission, or a read-only mount."""
    return isinstance(error, PermissionError) or error.errno == errno.EROFS


def _refusal(action: str, target: Path, error: OSError, fix: str) -> AuthorizationError:
    """The error for an ``action`` on ``target`` that the system refused; ``fix`` completes "Make ..." in the hint."""
    return AuthorizationError(
        f"cannot {action} '{format_host_path(target)}': {error.strerror}",
        hint=f"Make {fix}, then run 'agw config sync-ssh-config'.",
    )


# -- Legacy approach (managed section in ssh_config) -----------------------


# -- Shared helpers --------------------------------------------------------


def _host_entries(config: Config, db: Database) -> list[str]:
    """Every managed Host block, in database order, for both layouts.

    The store directory is checked once, as the key is: a VM name adds no
    character an entry value refuses (``trust._SAFE_NAME``), so every VM's
    store file is refused exactly when the directory is.
    """
    identity = _identity_value(config.operator.ssh_private_key)
    try:
        _entry_value(trust.STORE_DIR, "host-key store directory")
    except ConfigError as e:
        output.warn(f"SSH config entries left out for every VM: {_with_hint(e)}")
        return []
    prefix = config.operator.ssh_host_prefix
    agent_prefix = config.operator.ssh_agent_host_prefix
    entries: list[str] = []
    for vm in db.list_vms():
        entries.extend(_vm_entries(db, vm, identity, prefix, agent_prefix))
    return entries


def _vm_entries(db: Database, vm: VMRow, identity: str | None, prefix: str, agent_prefix: str) -> list[str]:
    """The VM's admin entry and one entry per agent on it, verifying its host key against the store.

    Every entry carries the verification options, record or not, so the
    operator's own ``ssh`` never accepts the VM's key on first use (see
    :func:`_verification_lines`). A VM whose entries cannot be written safely
    gets none, with a warning, rather than entries that would trust whatever
    answers or a line that says something else; it never aborts the rebuild
    for every other VM. A VM with no tailnet address has nothing to connect to
    and gets none either.

    Agent entries share the VM entry's ``HostName``, ``IdentityFile`` and
    verification options; only ``User`` and the alias differ. The agent alias
    is a top-level ``<agent_prefix><agent.name>``, not nested under the VM
    alias, because an agent belongs to exactly one VM and the operator-facing
    handle is the agent name, not the Linux user.
    """
    if not vm.tailscale_host:
        return []
    agents = db.list_agents(vm_name=vm.name)
    try:
        # Boundary: these are persisted values, and each becomes a token of a line ssh parses. The host and the
        # users follow the execution core's own rule for the same rows; an agent's name is also its alias.
        _checked(ssh_process.check_host, vm.tailscale_host, "its tailnet address")
        _checked(ssh_process.check_user, vm.admin_username, "its admin user")
        for agent in agents:
            _checked(ssh_process.check_user, agent.linux_user, f"agent {agent.name!r}'s user")
            _require_alias_token(agent.name)
        verification = _verification_lines(vm.name)
    except AgentworksError as e:
        output.warn(f"SSH config entries for VM '{vm.name}' left out: {_with_hint(e)}")
        return []
    entries = [
        _format_entry(ssh_host_alias(vm.name, prefix), vm.tailscale_host, vm.admin_username, identity, verification)
    ]
    for agent in agents:
        entries.append(
            _format_entry(
                ssh_agent_alias(agent.name, agent_prefix), vm.tailscale_host, agent.linux_user, identity, verification
            )
        )
    return entries


def _checked(check: Callable[[str], None], value: str, what: str) -> None:
    """Run one of ``ssh_process``'s checks, the rule every connection to the row applies, naming ``what`` failed."""
    try:
        check(value)
    except ValidationError as e:
        raise ValidationError(f"{what} {value!r}: {e}") from e


# What an alias's name part may hold: nothing ssh would split, unquote, match as a pattern or end the line at.
_ALIAS_TOKEN = re.compile(r"[A-Za-z0-9._-]+")


def _require_alias_token(name: str) -> None:
    if not _ALIAS_TOKEN.fullmatch(name):
        raise ValidationError(f"{name!r} cannot be part of an SSH host alias")


def _with_hint(error: Exception) -> str:
    """``error``'s message, then the hint of an Agentworks error that has one, after exactly one period."""
    message = str(error)
    hint = error.hint if isinstance(error, AgentworksError) else None
    return f"{message}{'' if message.endswith('.') else '.'} {hint}" if hint else message


def _verification_lines(vm_name: str) -> str:
    """The option lines that make the operator's own ``ssh`` verify the VM's host key, as entry text.

    The store file is the entry's ``GlobalKnownHostsFile`` and the user
    known-hosts file is the null device. ssh writes only to the user file, so
    no host-key checking or update setting of the operator's that takes
    effect first (an earlier ``Host *``, or ``-o`` on the command line, with
    ``StrictHostKeyChecking accept-new`` or ``no`` or ``UpdateHostKeys yes``)
    can write into the store. A known-hosts file setting of the operator's
    read first takes precedence over these: what ssh accepts then goes to
    their own file, or the store is skipped and the VM fails closed. A VM with no record has no key
    in its store, so strict checking refuses it; its entries also carry a
    comment naming the commands that record one. A record that cannot be read
    gets no comment: ssh reports the read failure itself.

    The other pins match the execution core's (:func:`trust.pinned_options`).
    Raises :class:`~agentworks.errors.AgentworksError` when the store path
    cannot be written into an entry.
    """
    options = (
        f"HostKeyAlias {trust.host_key_alias(vm_name)}",
        f"GlobalKnownHostsFile {_entry_value(trust.store_file(vm_name), 'host-key store path')}",
        f"UserKnownHostsFile {trust.NULL_DEVICE}",
        "StrictHostKeyChecking yes",
        *(f"{name} {value}" for name, value in trust.pinned_options()),
    )
    try:
        missing = not trust.has_record(vm_name)
    except AgentworksError:
        missing = False
    if missing:
        options += (
            f"# No host key is recorded for VM '{vm_name}' yet, so ssh refuses it. Record one with "
            f"'agw vm import-host-keys {vm_name}' or 'agw vm recover {vm_name}'.",
        )
    return "".join(f"    {option}\n" for option in options)


def _entry_value(path: Path, what: str) -> str:
    """``path`` as a value in a managed entry, as the execution core passes it, or a ``ConfigError``.

    The path as given, not its symlink target, must pass
    :func:`trust.literal_path`, the rule the execution core applies to the
    same files, so both accept or refuse the same file: a path with ``%``,
    ``$``, a double quote, a backslash or a control character, which ssh would
    expand or misread, is refused rather than written as a line that names
    another file. It is written absolute, so ssh reads the file the core does
    whatever its own idea of ``~``, which is the passwd home and need not be
    ``$HOME``. On Windows, where the ssh shipped with Git for Windows may read
    the entries and accepts no form of a value with an apostrophe and no
    whitespace, such a path is refused too, with a hint to rename it unless the
    apostrophe is in the home directory.
    """
    value = trust.literal_path(path, what)
    if _GIT_FOR_WINDOWS_MAY_READ and "'" in value and not any(char in value for char in " \t"):
        home = Path.home()
        if "'" in home.as_posix() and path.is_relative_to(home):
            # Named in full: shortening it to ``~`` would hide the apostrophe, and renaming it is no fix.
            raise ConfigError(
                f"{what} {format_host_path(path)} cannot be written into an SSH config entry: the ssh shipped "
                f"with Git for Windows reads no form of this path, which is under the home directory {home}, "
                "and has an apostrophe (') and no whitespace"
            )
        raise ConfigError(
            f"{what} {format_host_path(path)} cannot be written into an SSH config entry: the ssh shipped "
            "with Git for Windows reads no form of a path with an apostrophe (') and no whitespace",
            hint="Rename the directory or file so its path has no apostrophe.",
        )
    return trust.option_value(value)


def _identity_value(identity_file: Path) -> str | None:
    """The operator's key as an ``IdentityFile`` value (:func:`_entry_value`), or ``None`` with a warning.

    A key path the entries cannot carry is left out of them rather than
    refused: the operator's own ``ssh`` then offers its default keys for those
    aliases. Computed once per rebuild, so the warning appears once.
    """
    try:
        return _entry_value(identity_file, "SSH identity file")
    except ConfigError as e:
        fix = e.hint or "Move the key to a plain path."
        output.warn(
            f"IdentityFile left out of the SSH config entries: {e}. Your own ssh offers its default keys "
            f"for those aliases instead. {fix}"
        )
        return None


def _format_entry(alias: str, hostname: str, user: str, identity: str | None, verification: str) -> str:
    """Format a single SSH config Host block.

    ``identity`` is the ``IdentityFile`` value from :func:`_identity_value`;
    ``None`` leaves the line out. ``verification`` is the VM's
    :func:`_verification_lines`.
    """
    identity_line = f"    IdentityFile {identity}\n" if identity is not None else ""
    return f"Host {alias}\n    HostName {hostname}\n    User {user}\n{identity_line}{verification}"


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
    _make_directory(ssh_config.parent)
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
