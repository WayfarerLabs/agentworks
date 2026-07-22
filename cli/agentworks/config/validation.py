"""Config-path constants and name/username validators.

Split out of the former monolithic ``agentworks/config.py`` (see
``agentworks/config/__init__.py`` for the package overview). This module has
no dependency on any other ``agentworks.config`` submodule, so it is safe to
import first from anywhere in the package.
"""

from __future__ import annotations

import re
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "agentworks"
CONFIG_PATH = CONFIG_DIR / "config.toml"

NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")
# Linux username: alphanumeric, hyphens, underscores; 1-32 chars
VM_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
# SSH host prefix: alphanumeric, hyphens, underscores, dots
SSH_HOST_PREFIX_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

MAX_NAME_LENGTH = 30


def validate_name(name: str, *, allow_double_hyphen: bool = False) -> None:
    """Validate a resource name, raising ValidationError on failure.

    Rules: lowercase alphanumeric, hyphens, underscores. Must start and end
    with alphanumeric. Max 30 characters (leaves room for agent username
    derivation within the 32-character Linux username limit).

    Consecutive hyphens (``--``) are rejected by default because they are
    reserved for the ``<workspace>--<agent>`` separator used by the legacy
    agent-derivation scheme; new resource names need headroom for that.
    Pass ``allow_double_hyphen=True`` only when validating a name that is
    being used to *look up* an existing entity (the DB is the ultimate
    arbiter of existence; the validator only sanitizes characters). Legacy
    sessions predating the rule use ``--`` in their names and still need to
    be deletable / attachable / addable to consoles.
    """
    from agentworks.output import ValidationError

    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(f"name '{name}' is too long ({len(name)} chars, max {MAX_NAME_LENGTH})")
    if not NAME_RE.match(name) or (not allow_double_hyphen and "--" in name):
        suffix = "" if allow_double_hyphen else ", and cannot contain consecutive hyphens (--)"
        raise ValidationError(
            f"invalid name '{name}'. Names must be lowercase alphanumeric "
            "with hyphens or underscores, must start and end with a letter or "
            f"digit{suffix}."
        )


def validate_admin_username(admin_username: str) -> None:
    """Validate an admin username for shell and OS safety."""
    from agentworks.output import ValidationError

    if not VM_USER_RE.match(admin_username):
        raise ValidationError(
            f"invalid admin_username '{admin_username}'. Must be a valid Linux username "
            "(lowercase, alphanumeric/hyphens/underscores, max 32 chars)"
        )
