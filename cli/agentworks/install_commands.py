"""System and user install commands: two install-command declarable kinds.

Two first-class Registry kinds live here next to the code that loads them:

- ``system-install-command`` (``SystemInstallCommandEntry``): a
  system-level (root) install command run during VM init.
- ``user-install-command`` (``UserInstallCommandEntry``): a per-user
  install command run during admin/agent init.

Both are ``declarable`` kinds under the ``error`` miss policy: a typo'd
reference (an unknown command named by a vm-template, admin-template, or
agent-template) surfaces as a framework ``ConfigError`` at
``build_registry`` time citing the reference's source. Built-in entries
ship as bundled manifests under ``manifests/builtin/``; operators may add
or override entries via YAML manifests (or the deprecated TOML surface,
published by ``publish_to`` below).

``agentworks.resources.kinds.__init__`` imports this module so the two
kinds self-register into ``KIND_REGISTRY`` at load.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ConfigError
from agentworks.resource_loading import (
    _SYNTHESIZED_DECLS,
    _require_field,
    _require_list,
)
from agentworks.resources.kind import KIND_REGISTRY, synthesize_no_default

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config, _SectionLineMap
    from agentworks.resources import Registry
    from agentworks.resources.reference import ResourceReference


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SystemInstallCommandEntry(DeclaredResource):
    # System-declared entry; uniform metadata from ``DeclaredResource``.
    description: str = field()  # required (see AptSourceEntry field() note)
    command: str
    path: list[str] = field(default_factory=list)
    test_exec: str | None = None
    test_file: str | None = None
    test_dir: str | None = None


@dataclass(frozen=True, kw_only=True)
class UserInstallCommandEntry(DeclaredResource):
    # System-declared entry; uniform metadata from ``DeclaredResource``.
    description: str = field()  # required (see AptSourceEntry field() note)
    command: str
    path: list[str] = field(default_factory=list)
    test_exec: str | None = None
    test_file: str | None = None
    test_dir: str | None = None


# -- Loading -------------------------------------------------------------------


class _TestFields(TypedDict):
    test_exec: str | None
    test_file: str | None
    test_dir: str | None


def _load_test_fields(data: dict[str, object], ctx: str) -> _TestFields:
    """Load and validate test_exec/test_file/test_dir fields. At most one may be set."""
    if "test" in data:
        raise ConfigError(f"{ctx}: 'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'.")
    fields: _TestFields = {"test_exec": None, "test_file": None, "test_dir": None}
    for key in ("test_exec", "test_file", "test_dir"):
        raw = str(data[key]).strip() if key in data else None
        fields[key] = raw if raw else None  # type: ignore[literal-required,unused-ignore]
    set_count = sum(1 for v in fields.values() if v is not None)
    if set_count > 1:
        raise ConfigError(f"{ctx}: at most one of test_exec, test_file, test_dir may be set")
    return fields


def _load_system_commands(
    raw: dict[str, object],
    decls: _SectionLineMap = _SYNTHESIZED_DECLS,
) -> dict[str, SystemInstallCommandEntry]:
    entries: dict[str, SystemInstallCommandEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ConfigError(f"system_install_commands.{name} must be a table")
        ctx = f"system_install_commands.{name}"
        tests = _load_test_fields(data, ctx)
        entries[name] = SystemInstallCommandEntry(
            name=name,
            description=str(data.get("description", "")),
            command=str(_require_field(data, "command", ctx)),
            path=_require_list(data, "path", ctx) if "path" in data else [],
            declared_at=decls.lookup("system_install_commands", name),
            **tests,
        )
    return entries


def _load_user_commands(
    raw: dict[str, object],
    decls: _SectionLineMap = _SYNTHESIZED_DECLS,
) -> dict[str, UserInstallCommandEntry]:
    entries: dict[str, UserInstallCommandEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ConfigError(f"user_install_commands.{name} must be a table")
        ctx = f"user_install_commands.{name}"
        tests = _load_test_fields(data, ctx)
        entries[name] = UserInstallCommandEntry(
            name=name,
            description=str(data.get("description", "")),
            command=str(_require_field(data, "command", ctx)),
            path=_require_list(data, "path", ctx) if "path" in data else [],
            declared_at=decls.lookup("user_install_commands", name),
            **tests,
        )
    return entries


def publish_to(registry: Registry, config: Config | None = None) -> None:
    """Publish operator-declared TOML install-command entries into the registry.

    Built-in install commands no longer publish here: they ship as bundled
    YAML manifests under ``manifests/builtin/`` and land via
    ``builtin_manifests.publish_to`` (which runs first in
    ``build_registry``), with ``Origin.built_in`` and a shipped-file
    source. This function now carries only the operator's deprecated TOML
    surface for these two kinds (retired separately under ADR 0016).

    When ``config`` is provided, operator-declared entries
    (``[system_install_commands.<name>]``,
    ``[user_install_commands.<name>]`` in the operator's TOML) publish with
    ``Origin.operator_declared(...)``. Publish order + the kinds'
    ``builtin_override = "allow"`` policy is what lets the operator row
    replace the built-in at ``Registry.add``: the built-in manifests
    publish first, then this operator publisher. Config-side publishing
    lives here (rather than in ``Config.publish_to``) because parsing
    operator install-command entries is this module's expertise; Config
    just stashes the raw TOML dicts.

    ``declared_at`` falls through to the loaders' default synthesized shim
    here (the deprecated TOML surface does not carry the section-line map);
    manifest entries carry a real location via the decoders.
    """
    if config is None:
        return

    from agentworks.config import CONFIG_PATH
    from agentworks.resources import Origin

    op_origin = Origin.operator_declared(file=CONFIG_PATH, line=0)
    for sys_name, sys_cmd in _load_system_commands(
        config.system_install_commands
    ).items():
        registry.add("system-install-command", sys_name, sys_cmd, op_origin)
    for user_name, user_cmd in _load_user_commands(
        config.user_install_commands
    ).items():
        registry.add("user-install-command", user_name, user_cmd, op_origin)


# -- Framework kind strategies -------------------------------------------------
#
# Both kinds use the **error miss policy**: a typo in a template's
# ``system_install_commands`` / ``user_install_commands`` list surfaces as
# a framework miss-policy error at ``build_registry`` time, citing the
# reference's source. There is no auto-declare path: entries are built-in
# (bundled manifests) or operator-declared, and references must resolve to
# a known name.


@dataclass(frozen=True)
class _SystemInstallCommandKind:
    """Implementation of ``ResourceKind`` for ``"system-install-command"``."""

    kind: str = "system-install-command"
    description: str = "System-level (root) install commands for VM init"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


@dataclass(frozen=True)
class _UserInstallCommandKind:
    """Implementation of ``ResourceKind`` for ``"user-install-command"``."""

    kind: str = "user-install-command"
    description: str = "Per-user install commands for admin/agent init"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        return synthesize_no_default(self.kind, references)


KIND_REGISTRY["system-install-command"] = _SystemInstallCommandKind()
KIND_REGISTRY["user-install-command"] = _UserInstallCommandKind()
