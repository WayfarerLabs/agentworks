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

# Hard OS limits the name-derived identifiers must fit inside. util-linux
# ``useradd`` / ``groupadd`` cap the login name at 32 characters (the historic
# utmp ``ut_user`` field width; ``LOGIN_NAME_MAX`` is 256 in glibc but useradd
# enforces the smaller value, and ``useradd -U`` creates a same-named group, so
# the group name is bounded identically). These are the ceilings the derived
# ``agt-<agent>`` username and ``ws-<workspace>`` group name must satisfy; the
# per-kind caps below are derived FROM them at the prefix's module so a prefix
# change cannot reintroduce an over-limit identifier.
LINUX_USERNAME_MAX_LENGTH = 32
LINUX_GROUPNAME_MAX_LENGTH = 32

# VM names flow into TWO composed downstream identifiers, and the cap is the
# MIN over both sinks so neither can overflow. Both prepend the system slug
# (capped at 20 by ``validate_slug`` in ``vms/manager/_helpers.py``) and a
# joining dash:
#
#   - Hostname sink: ``{slug}-{vm}`` becomes the OS hostname / Tailscale
#     MagicDNS label (``vms/manager/lifecycle.py``). A DNS label caps at 63,
#     leaving 63 - 1 - 20 = 42 for the VM name.
#   - Azure vnet sink: the azure platform derives the virtual-network
#     subresource name ``{slug}-{vm}-vnet`` (``plugins/azure/platform.py``).
#     Microsoft.Network/virtualNetworks caps names at 64, leaving
#     64 - 20 - 1 - 5 = 38 for the VM name.
#
# The vnet sink is the tighter one (its ``-vnet`` suffix costs 5 characters the
# bare hostname does not), so 38 is what binds, NOT the 63-char hostname and
# NOT Azure's 64-char computer-name limit. Keeping the arithmetic visible means
# a slug-length or suffix change reshapes the cap here rather than overflowing
# opaquely on Azure; a pinned test asserts the worst-case vnet name is exactly
# 64 at the cap.
DNS_LABEL_MAX_LENGTH = 63
AZURE_VNET_NAME_MAX_LENGTH = 64
MAX_SYSTEM_SLUG_LENGTH = 20  # validate_slug (vms/manager/_helpers.py)
_AZURE_VNET_SUFFIX = "-vnet"  # plugins/azure/platform.py
# hostname sink: 63 - 1 - 20 = 42
_HOSTNAME_SINK_CAP = DNS_LABEL_MAX_LENGTH - len("-") - MAX_SYSTEM_SLUG_LENGTH
# Azure vnet sink: 64 - 20 - 1 - 5 = 38 (the binding one)
_AZURE_VNET_SINK_CAP = AZURE_VNET_NAME_MAX_LENGTH - MAX_SYSTEM_SLUG_LENGTH - len("-") - len(_AZURE_VNET_SUFFIX)
MAX_VM_NAME_LENGTH = min(_HOSTNAME_SINK_CAP, _AZURE_VNET_SINK_CAP)  # -> 38

# Console / vm-site names hit no OS-level identifier limit: they land in tmux
# window labels, a registry key, and display strings / paths only. (Session
# names look freeform too but are NOT: they embed in a bounded AF_UNIX socket
# path and get their own tighter ``MAX_SESSION_NAME_LENGTH`` in
# ``sessions/tmux.py``.) These still get a bound (not unbounded) so a
# pathological name cannot blow out list tables; 64 is generous while staying
# table-friendly.
MAX_FREEFORM_NAME_LENGTH = 64

# Secret names are never derived into Linux usernames, so the username-driven
# caps do not apply to them. We keep a bound (not unbounded) and adopt the k8s
# DNS-subdomain ceiling as a well-understood, non-arbitrary limit: it is
# generous enough for the ``git-token-<credential-name>`` default token secret
# (the case that motivated lifting the cap, issue #275) and everything else.
MAX_SECRET_NAME_LENGTH = 253


def validate_name(name: str, *, allow_double_hyphen: bool = False, max_length: int = MAX_FREEFORM_NAME_LENGTH) -> None:
    """Validate a resource name, raising ValidationError on failure.

    Rules: lowercase alphanumeric, hyphens, underscores. Must start and end
    with alphanumeric. All character rules stay identical regardless of
    ``max_length``; only the length cap varies by kind.

    There is no single correct name-length cap: each resource kind has its own
    downstream sink, so each caller MUST pass the cap derived for its kind. Each
    cap is derived at (and imported from) the module that owns its sink, so a
    change to the sink reshapes the cap at its source:

    - **agent** -> ``MAX_AGENT_NAME_LENGTH`` (``agents/manager/_common.py``).
      The name is derived into the Linux username ``agt-<name>`` (and, via
      ``useradd -U``, a same-named group), so the cap is the 32-char Linux limit
      minus the ``agt-`` prefix.
    - **workspace** -> ``MAX_WORKSPACE_NAME_LENGTH`` (``agents/grants.py``). The
      name is derived into the Linux group ``ws-<name>``, so the cap is the same
      32-char limit minus the ``ws-`` prefix.
    - **vm** -> ``MAX_VM_NAME_LENGTH`` (above). The name composes into both the
      ``{slug}-{vm}`` hostname / DNS label and the ``{slug}-{vm}-vnet`` Azure
      virtual-network name; the cap is the MIN over both sinks (the vnet sink
      binds).
    - **session** -> ``MAX_SESSION_NAME_LENGTH`` (``sessions/tmux.py``). The
      name embeds in the per-agent tmux AF_UNIX socket path, whose length is
      bounded by ``sun_path``; the cap is what remains under a max-length agent
      username.
    - **console / vm-site** -> ``MAX_FREEFORM_NAME_LENGTH`` (64). These hit no
      OS identifier limit (tmux labels, a registry key, display strings / paths
      only); 64 is a table-friendly bound, not an OS ceiling.
    - **secret** -> ``MAX_SECRET_NAME_LENGTH`` (253). Never derived into a
      username; bounded by the k8s DNS-subdomain ceiling (issue #275).

    The default is ``MAX_FREEFORM_NAME_LENGTH`` (64): a caller that forgets to
    pass a cap gets the generous freeform bound, never a silently-wrong OS cap.
    Callers whose name feeds a bounded downstream identifier (agent, workspace,
    vm, session) MUST pass their derived cap so an over-limit identifier is
    rejected at the CLI boundary rather than failing opaquely downstream.

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

    if len(name) > max_length:
        raise ValidationError(f"name '{name}' is too long ({len(name)} chars, max {max_length})")
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

    from agentworks.errors import ConfigError

    normalized = posixpath.normpath(path)
    if normalized == "/home" or normalized.startswith("/home/"):
        raise ConfigError(
            f"paths.vm_workspaces must not be at or under /home (got {path!r}, "
            f"normalized to {normalized!r}). /home is the Linux user-home namespace "
            "on the VM and will collide with a future 'useradd -m'. Use the default "
            "'/opt/agentworks/workspaces', or mount a data volume at that path (or "
            "symlink it there), rather than nesting workspaces under /home."
        )
