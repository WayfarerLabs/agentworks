"""Built-in catalog loading, merging, and resolution.

The catalog provides named entries for apt sources, apt packages, system
install commands, and user install commands. A built-in catalog ships with
the package; custom config entries override built-in entries on name collision.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from agentworks.errors import ExternalError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources import Origin, Registry
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


class CatalogError(ExternalError):
    """Raised when the catalog is invalid."""


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True)
class AptSourceEntry:
    """One apt repository source. Referenced by ``AptPackageEntry.apt_sources``
    when a package requires a source's key + list stanza before it can be
    installed. First-class Registry Resource -- ``origin`` set by the
    catalog publisher, ``references`` attached by the framework's finalize
    pass from the apt_packages that name it.
    """

    name: str
    description: str
    key_url: str
    key_path: str
    source: str
    source_file: str
    key_dearmor: bool = False
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class AptPackageEntry:
    name: str
    description: str
    apt: list[str]
    apt_sources: list[str] = field(default_factory=list)
    # Phase 2b: catalog entries become first-class Registry citizens.
    # ``origin`` is set by the publisher (``code-declared by
    # agentworks.catalog`` for built-in entries); ``references`` is attached
    # by the framework's finalize pass from incoming references.
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        """Emit one ``ResourceReference`` per name in ``apt_sources``. The
        framework's ``apt_source`` kind uses an ``error`` miss policy, so
        an unknown source name surfaces as a clean ``ConfigError`` at
        ``build_registry`` time with the referencing package's identity
        attached (rather than the pre-Phase-2b silent ordering assumption
        that packages must appear after their sources in TOML).

        The registry attaches the corresponding ``ReferenceEntry`` to
        each ``AptSourceEntry`` during finalize, so
        ``agw resource describe apt_source github`` shows every apt_package
        that depends on it -- the dependency graph that was previously
        implicit is now visible.
        """
        from agentworks.resources.reference import ResourceReference

        return [
            ResourceReference(
                name=source_name,
                kind="apt_source",
                usage=f"the {source_name} apt source",
                source=("apt_package", self.name),
            )
            for source_name in self.apt_sources
        ]


@dataclass(frozen=True)
class SystemInstallCommandEntry:
    name: str
    description: str
    command: str
    path: list[str] = field(default_factory=list)
    test_exec: str | None = None
    test_file: str | None = None
    test_dir: str | None = None
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class UserInstallCommandEntry:
    name: str
    description: str
    command: str
    path: list[str] = field(default_factory=list)
    test_exec: str | None = None
    test_file: str | None = None
    test_dir: str | None = None
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class ResolvedCatalog:
    apt_sources: dict[str, AptSourceEntry]
    apt_packages: dict[str, AptPackageEntry]
    system_install_commands: dict[str, SystemInstallCommandEntry]
    user_install_commands: dict[str, UserInstallCommandEntry]


# -- Loading -------------------------------------------------------------------

_BUILTIN_CATALOG_PATH = Path(__file__).parent / "catalog.toml"

# source_file must be a simple filename (no slashes, no shell metacharacters)
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _require_field(data: dict[str, object], key: str, context: str) -> object:
    if key not in data:
        raise CatalogError(f"{context}.{key} is required")
    return data[key]


def _require_list(data: dict[str, object], key: str, context: str) -> list[str]:
    val = data.get(key, [])
    if not isinstance(val, list):
        raise CatalogError(f"{context}.{key} must be a list")
    return [str(item) for item in val]


def _load_apt_sources(raw: dict[str, object]) -> dict[str, AptSourceEntry]:
    entries: dict[str, AptSourceEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise CatalogError(f"apt_sources.{name} must be a table")
        ctx = f"apt_sources.{name}"
        source_file = str(_require_field(data, "source_file", ctx))
        if not _SAFE_FILENAME_RE.match(source_file):
            raise CatalogError(f"{ctx}.source_file must be a simple filename, got: {source_file}")
        entries[name] = AptSourceEntry(
            name=name,
            description=str(data.get("description", "")),
            key_url=str(_require_field(data, "key_url", ctx)),
            key_path=str(_require_field(data, "key_path", ctx)),
            source=str(_require_field(data, "source", ctx)),
            source_file=source_file,
            key_dearmor=bool(data.get("key_dearmor", False)),
        )
    return entries


def _load_apt_packages(raw: dict[str, object]) -> dict[str, AptPackageEntry]:
    entries: dict[str, AptPackageEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise CatalogError(f"apt_packages.{name} must be a table")
        ctx = f"apt_packages.{name}"
        entries[name] = AptPackageEntry(
            name=name,
            description=str(data.get("description", "")),
            apt=_require_list(data, "apt", ctx),
            apt_sources=_require_list(data, "apt_sources", ctx) if "apt_sources" in data else [],
        )
    return entries


class _TestFields(TypedDict):
    test_exec: str | None
    test_file: str | None
    test_dir: str | None


def _load_test_fields(data: dict[str, object], ctx: str) -> _TestFields:
    """Load and validate test_exec/test_file/test_dir fields. At most one may be set."""
    if "test" in data:
        raise CatalogError(f"{ctx}: 'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'.")
    fields: _TestFields = {"test_exec": None, "test_file": None, "test_dir": None}
    for key in ("test_exec", "test_file", "test_dir"):
        raw = str(data[key]).strip() if key in data else None
        fields[key] = raw if raw else None  # type: ignore[literal-required,unused-ignore]
    set_count = sum(1 for v in fields.values() if v is not None)
    if set_count > 1:
        raise CatalogError(f"{ctx}: at most one of test_exec, test_file, test_dir may be set")
    return fields


def _load_system_commands(raw: dict[str, object]) -> dict[str, SystemInstallCommandEntry]:
    entries: dict[str, SystemInstallCommandEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise CatalogError(f"system_install_commands.{name} must be a table")
        ctx = f"system_install_commands.{name}"
        tests = _load_test_fields(data, ctx)
        entries[name] = SystemInstallCommandEntry(
            name=name,
            description=str(data.get("description", "")),
            command=str(_require_field(data, "command", ctx)),
            path=_require_list(data, "path", ctx) if "path" in data else [],
            **tests,
        )
    return entries


def _load_user_commands(raw: dict[str, object]) -> dict[str, UserInstallCommandEntry]:
    entries: dict[str, UserInstallCommandEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise CatalogError(f"user_install_commands.{name} must be a table")
        ctx = f"user_install_commands.{name}"
        tests = _load_test_fields(data, ctx)
        entries[name] = UserInstallCommandEntry(
            name=name,
            description=str(data.get("description", "")),
            command=str(_require_field(data, "command", ctx)),
            path=_require_list(data, "path", ctx) if "path" in data else [],
            **tests,
        )
    return entries


def _load_toml(path: Path) -> dict[str, object]:
    with open(path, "rb") as f:
        return tomllib.load(f)


@cache
def load_builtin_catalog() -> ResolvedCatalog:
    """Load the built-in catalog bundled with the package.

    Memoized via ``@cache`` because every command path now parses it
    twice: once via ``catalog.publish_to(registry)`` at
    ``build_registry`` time and again via ``load_catalog(config)`` at
    initializer time (which still needs the resolved entries' payloads
    to drive installs). The built-in catalog is a ship-time artifact
    so the cache holds for the process lifetime; the cache is
    file-bound through ``_BUILTIN_CATALOG_PATH`` which is itself a
    module-level constant.
    """
    if not _BUILTIN_CATALOG_PATH.exists():
        raise CatalogError(f"Built-in catalog not found: {_BUILTIN_CATALOG_PATH}")

    data = _load_toml(_BUILTIN_CATALOG_PATH)
    return _parse_catalog(data)


def _get_section(data: dict[str, object], key: str) -> dict[str, object]:
    """Extract a TOML table section, returning an empty dict if missing or wrong type."""
    val = data.get(key, {})
    if not isinstance(val, dict):
        raise CatalogError(f"Catalog section '{key}' must be a table, got {type(val).__name__}")
    return val


def _parse_catalog(data: dict[str, object]) -> ResolvedCatalog:
    return ResolvedCatalog(
        apt_sources=_load_apt_sources(_get_section(data, "apt_sources")),
        apt_packages=_load_apt_packages(_get_section(data, "apt_packages")),
        system_install_commands=_load_system_commands(_get_section(data, "system_install_commands")),
        user_install_commands=_load_user_commands(_get_section(data, "user_install_commands")),
    )


def load_catalog(config: Config) -> ResolvedCatalog:
    """Load and merge built-in + custom catalog entries.

    Custom entries override built-in entries with the same name.
    Cross-references (apt_sources in apt_packages) are validated.
    """
    builtin = load_builtin_catalog()

    # Parse custom entries (raw dicts from config) into typed entries
    custom_apt_sources = _load_apt_sources(config.apt_sources)
    custom_apt_packages = _load_apt_packages(config.apt_packages)
    custom_system_cmds = _load_system_commands(config.system_install_commands)
    custom_user_install_cmds = _load_user_commands(config.user_install_commands)

    # Merge: custom wins on name collision
    apt_sources = {**builtin.apt_sources, **custom_apt_sources}
    apt_packages = {**builtin.apt_packages, **custom_apt_packages}
    system_cmds = {**builtin.system_install_commands, **custom_system_cmds}
    user_install_cmds = {**builtin.user_install_commands, **custom_user_install_cmds}

    return ResolvedCatalog(
        apt_sources=apt_sources,
        apt_packages=apt_packages,
        system_install_commands=system_cmds,
        user_install_commands=user_install_cmds,
    )


# ``_validate_references`` retired: the framework validates
# apt_package -> apt_source edges via ``AptSourceKind``'s ``error`` miss
# policy at ``Registry.finalize`` time (the reference is emitted by
# ``AptPackageEntry.referenced_resources()``). Same pattern as Phase 2b.0
# dropping ``validate_selections``: single source of truth for
# reference validation lives in the framework.


def publish_to(registry: Registry, config: Config | None = None) -> None:
    """Publish catalog entries into the registry as first-class Resources.

    Phase 2b: built-in catalog entries become Registry citizens with
    ``Origin.code_declared(source="agentworks.catalog")``. The four
    catalog kinds (``apt_source``, ``apt_package``,
    ``system_install_command``, ``user_install_command``) use the
    framework's error miss policy, so a typo'd reference from
    ``[vm_templates.*].apt_packages = ["..."]`` etc. surfaces as a
    framework error citing the reference's source.

    ``apt_source`` is published even though operators don't reference
    sources directly from templates: apt_packages reference them via
    their ``apt_sources`` field, so the framework needs the sources in
    the registry to resolve the ``AptPackageEntry.referenced_resources()``
    edges cleanly. The dependency graph
    (apt_package -> apt_source) becomes visible in
    ``agw resource describe`` via the ``Referenced by:`` section on
    each apt_source.

    When ``config`` is provided, operator-declared catalog entries
    (``[apt_sources.<name>]``, ``[apt_packages.<name>]``,
    ``[system_install_commands.<name>]``,
    ``[user_install_commands.<name>]`` in the operator's TOML) are
    published on top of the built-in entries with
    ``Origin.operator_declared(...)``. Same last-writer-wins pattern
    the other publishers use: catalog runs first, then Config, then
    other publishers -- an operator's override lands on top of the
    code-declared base. Config-side publishing lives here (rather than
    in ``Config.publish_to``) because parsing operator catalog entries
    is catalog's expertise; Config just stashes the raw TOML dicts.
    """
    from agentworks.resources import Origin

    builtin = load_builtin_catalog()
    code_origin = Origin.code_declared(source="agentworks.catalog")

    for src_name, src in builtin.apt_sources.items():
        registry.add("apt_source", src_name, src, code_origin)
    for pkg_name, pkg in builtin.apt_packages.items():
        registry.add("apt_package", pkg_name, pkg, code_origin)
    for sys_name, sys_cmd in builtin.system_install_commands.items():
        registry.add("system_install_command", sys_name, sys_cmd, code_origin)
    for user_name, user_cmd in builtin.user_install_commands.items():
        registry.add("user_install_command", user_name, user_cmd, code_origin)

    if config is None:
        return

    # Operator-declared catalog entries. Parse the raw TOML dicts Config
    # stashed at load-time, publish each with operator-declared origin.
    #
    # Line-level ``declared_at`` isn't attached per-entry yet: catalog
    # loaders don't consume Phase 0's ``_SectionLineMap`` and Config
    # stores raw dicts (not typed entries) for these sections. Publishing
    # here uses ``Origin.operator_declared(file=CONFIG_PATH, line=0)`` --
    # matches the same sentinel Phase 0 uses for singleton-omitted-
    # section defaults (``named_console_template:default`` when there's
    # no ``[named_console]``). The renderer drops the parenthetical for
    # ``line=0``, so operators see "operator-declared" without file:line
    # for now.
    #
    # Plumbing declared_at through catalog entries properly is a small
    # follow-up (add ``declared_at: SourceLocation`` to the four entry
    # dataclasses, thread ``_SectionLineMap`` into the ``_load_*``
    # helpers, either at load_config time or via a public
    # ``config.declared_at_for(...)`` helper); tracked alongside the
    # ``named_console_template`` singleton-omitted case in the SDD
    # follow-ups.
    from agentworks.config import CONFIG_PATH

    op_origin = Origin.operator_declared(file=CONFIG_PATH, line=0)
    for src_name, src in _load_apt_sources(config.apt_sources).items():
        registry.add("apt_source", src_name, src, op_origin)
    for pkg_name, pkg in _load_apt_packages(config.apt_packages).items():
        registry.add("apt_package", pkg_name, pkg, op_origin)
    for sys_name, sys_cmd in _load_system_commands(
        config.system_install_commands
    ).items():
        registry.add("system_install_command", sys_name, sys_cmd, op_origin)
    for user_name, user_cmd in _load_user_commands(
        config.user_install_commands
    ).items():
        registry.add("user_install_command", user_name, user_cmd, op_origin)


# validate_selections removed in Phase 2b.0: the framework's catalog
# kinds + miss policy validate these references at build_registry time,
# which every manager-entry function calls before any business logic
# (per Phase 2a.0's hoist sweep). The function had no remaining
# production callers; tests covering the old contract were also dropped.
