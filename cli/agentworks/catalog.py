"""Built-in catalog loading, merging, and resolution.

The catalog provides named entries for apt sources, apt packages, system
install commands, and user install commands. A built-in catalog ships with
the package; user config entries override built-in entries on name collision.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import Config


class CatalogError(Exception):
    """Raised when the catalog is invalid."""


# -- Data classes --------------------------------------------------------------


@dataclass(frozen=True)
class AptSourceEntry:
    name: str
    description: str
    key_url: str
    key_path: str
    source: str
    source_file: str
    key_dearmor: bool = False


@dataclass(frozen=True)
class AptPackageEntry:
    name: str
    description: str
    apt: list[str]
    apt_sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SystemInstallCommandEntry:
    name: str
    description: str
    command: str
    path: list[str] = field(default_factory=list)
    test_exec: str | None = None
    test_file: str | None = None
    test_dir: str | None = None


@dataclass(frozen=True)
class UserInstallCommandEntry:
    name: str
    description: str
    command: str
    path: list[str] = field(default_factory=list)
    test_exec: str | None = None
    test_file: str | None = None
    test_dir: str | None = None


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


def _load_test_fields(data: dict[str, object], ctx: str) -> dict[str, str | None]:
    """Load and validate test_exec/test_file/test_dir fields. At most one may be set."""
    if "test" in data:
        raise CatalogError(f"{ctx}: 'test' is not a valid field. Use 'test_exec', 'test_file', or 'test_dir'.")
    fields: dict[str, str | None] = {}
    for key in ("test_exec", "test_file", "test_dir"):
        raw = str(data[key]).strip() if key in data else None
        fields[key] = raw if raw else None
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


def load_builtin_catalog() -> ResolvedCatalog:
    """Load the built-in catalog bundled with the package."""
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
    """Load and merge built-in + user-defined catalog entries.

    User entries override built-in entries with the same name.
    Cross-references (apt_sources in apt_packages) are validated.
    """
    builtin = load_builtin_catalog()

    # Parse user-defined entries (raw dicts from config) into typed entries
    user_apt_sources = _load_apt_sources(config.apt_sources)
    user_apt_packages = _load_apt_packages(config.apt_packages)
    user_system_cmds = _load_system_commands(config.system_install_commands)
    user_user_cmds = _load_user_commands(config.user_install_commands)

    # Merge: user wins on name collision
    apt_sources = {**builtin.apt_sources, **user_apt_sources}
    apt_packages = {**builtin.apt_packages, **user_apt_packages}
    system_cmds = {**builtin.system_install_commands, **user_system_cmds}
    user_cmds = {**builtin.user_install_commands, **user_user_cmds}

    catalog = ResolvedCatalog(
        apt_sources=apt_sources,
        apt_packages=apt_packages,
        system_install_commands=system_cmds,
        user_install_commands=user_cmds,
    )

    _validate_references(catalog)
    return catalog


def _validate_references(catalog: ResolvedCatalog) -> None:
    """Validate cross-references within the catalog."""
    for name, pkg in catalog.apt_packages.items():
        for src_name in pkg.apt_sources:
            if src_name not in catalog.apt_sources:
                raise CatalogError(f"apt_packages.{name} references unknown apt source: {src_name}")


def validate_selections(config: Config, catalog: ResolvedCatalog) -> None:
    """Validate that vm.config and agent.config selections resolve in the catalog."""
    for ref in config.vm.apt_packages:
        if ref not in catalog.apt_packages:
            raise CatalogError(f"vm.config.apt_packages references unknown entry: {ref}")
    for ref in config.vm.system_install_commands:
        if ref not in catalog.system_install_commands:
            raise CatalogError(f"vm.config.system_install_commands references unknown entry: {ref}")
    for ref in config.admin.user_install_commands:
        if ref not in catalog.user_install_commands:
            raise CatalogError(f"admin.config.user_install_commands references unknown entry: {ref}")
    for ref in config.agent.user_install_commands:
        if ref not in catalog.user_install_commands:
            raise CatalogError(f"agent.config.user_install_commands references unknown entry: {ref}")
