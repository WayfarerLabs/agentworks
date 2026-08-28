"""Config-path constants and the config file's own validators.

The resource naming rule and the per-kind length caps moved to
``agentworks.naming``, a top-level leaf, because a declared row carries
its ``name`` as a model field and a model resolves its field annotations
at class-definition time; importing anything under ``agentworks.config``
runs this package's ``__init__``, which loads modules that import the very
rows that would be reaching for the rule.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview). This module has
no dependency on any other ``agentworks.config`` submodule, so it is safe to
import first from anywhere in the package.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from pathlib import Path

from agentworks.errors import ConfigError
from agentworks.naming import VM_USER_RE

CONFIG_DIR = Path.home() / ".config" / "agentworks"
CONFIG_PATH = CONFIG_DIR / "config.toml"


def validate_admin_username(admin_username: str) -> None:
    """Validate an admin username for shell and OS safety."""
    from agentworks.output import ValidationError

    if not VM_USER_RE.match(admin_username):
        raise ValidationError(
            f"invalid admin_username '{admin_username}'. Must be a valid Linux username "
            "(lowercase, alphanumeric/hyphens/underscores, max 32 chars)"
        )


def validate_vm_workspaces(path: str) -> None:
    """Reject a ``paths.vm_workspaces`` value that lives at or under ``/home``.

    ``/home`` is the Linux user-home namespace on the VM. The admin and agent
    users each own ``/home/<user>``, and those homes are locked to mode 0750
    (agent-private, admin-private) for cross-user isolation. A workspace tree
    nested under ``/home`` would either collide with a future ``useradd -m``
    home or force one of those homes back to world-traversable so agents could
    reach the shared workspace, defeating the isolation. Keeping workspaces
    outside ``/home`` is the single source of truth that makes the 0750 homes
    safe, so we reject the misconfiguration at load time rather than warn at
    provisioning time.

    The path is normalized first (``//`` collapsed, trailing slash and ``.``
    segments removed) so ``/home``, ``/home/``, ``/home/foo``, and
    ``/home/foo/bar`` are all rejected. The check is ``== "/home"`` or a
    ``/home/`` prefix on the normalized path, NOT a bare ``/home`` prefix, so
    sibling paths that merely start with those characters (``/homelab``,
    ``/home2/ws``) are accepted. Raises ``ConfigError`` (the type ``_load_paths``
    already raises) with a migration hint.

    Normalization uses ``posixpath`` explicitly, not ``os.path``. This value is
    always a VM-side POSIX path regardless of the operator's host OS, and
    agentworks runs natively on Windows, where ``os.path`` is ``ntpath``:
    ``ntpath.normpath('/home/foo')`` returns ``'\\home\\foo'``, which would
    match neither branch below and silently accept every ``/home`` path.
    ``posixpath`` is the house choice for VM paths across the codebase.
    """
    import posixpath

    normalized = posixpath.normpath(path)
    if normalized == "/home" or normalized.startswith("/home/"):
        raise ConfigError(
            f"paths.vm_workspaces must not be at or under /home (got {path!r}, "
            f"normalized to {normalized!r}). /home is the Linux user-home namespace "
            "on the VM and will collide with a future 'useradd -m'. Use the default "
            "'/opt/agentworks/workspaces', or mount a data volume at that path (or "
            "symlink it there), rather than nesting workspaces under /home."
        )


_MISE_DURATION_RE = re.compile(r"^[1-9][0-9]*[dhwmy]$")


class MiseSettingsErrorKind(StrEnum):
    """Stable categories for mise model-validation failures."""

    PACKAGE_SYNTAX = "package-syntax"
    LOCKFILE = "lockfile"
    INSTALL_BEFORE = "install-before"


class MiseSettingsError(ValueError):
    """A mise validation failure with a stable machine-readable category."""

    def __init__(self, kind: MiseSettingsErrorKind, message: str) -> None:
        self.kind = kind
        super().__init__(message)


def _has_unsafe_mise_component_char(value: str) -> bool:
    return any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char in {'"', "\\"} for char in value)


def check_mise_settings(packages: list[str], lockfile: str | None, install_before: str) -> None:
    """Validate mise inputs before they reach config rendering or
    provisioning, raising ``ValueError`` with a FIELD-relative message.

    ``ValueError`` rather than ``ConfigError`` because this is what a
    model validator raises: pydantic re-raises anything that is neither a
    ``ValueError`` nor an ``AssertionError``, so a ``ConfigError`` here
    would escape ``model_validate`` and bypass the error bridge.
    """
    from agentworks.sources import SourceRefError, parse_source_ref

    for package in packages:
        name, separator, version = package.rpartition("@")
        if (
            not separator
            or not name
            or not version
            or "@" in version
            or _has_unsafe_mise_component_char(name)
            or _has_unsafe_mise_component_char(version)
        ):
            raise MiseSettingsError(
                MiseSettingsErrorKind.PACKAGE_SYNTAX,
                "mise_packages entries must use non-empty name@version syntax",
            )

    if lockfile is not None:
        try:
            parse_source_ref(lockfile, default_filename="mise.lock")
        except SourceRefError as exc:
            raise MiseSettingsError(MiseSettingsErrorKind.LOCKFILE, f"mise_lockfile is invalid: {exc}") from exc

    if _MISE_DURATION_RE.fullmatch(install_before):
        return
    try:
        date.fromisoformat(install_before)
    except ValueError as exc:
        raise MiseSettingsError(
            MiseSettingsErrorKind.INSTALL_BEFORE,
            "mise_install_before must be a positive duration such as '7d' or an ISO date",
        ) from exc
