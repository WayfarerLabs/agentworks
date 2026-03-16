"""Built-in catalog loading, merging, and resolution.

The catalog provides named entries for apt sources, apt packages, system
install commands, and user install commands. A built-in catalog ships with
the package; user config entries override built-in entries on name collision.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class UserInstallCommandEntry:
    name: str
    description: str
    command: str
    path: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedCatalog:
    apt_sources: dict[str, AptSourceEntry]
    apt_packages: dict[str, AptPackageEntry]
    system_install_commands: dict[str, SystemInstallCommandEntry]
    user_install_commands: dict[str, UserInstallCommandEntry]


# -- Loading -------------------------------------------------------------------

_BUILTIN_CATALOG_PATH = Path(__file__).parent / "catalog.toml"


def _load_apt_sources(raw: dict[str, object]) -> dict[str, AptSourceEntry]:
    entries: dict[str, AptSourceEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise CatalogError(f"apt_sources.{name} must be a table")
        entries[name] = AptSourceEntry(
            name=name,
            description=str(data.get("description", "")),
            key_url=str(data["key_url"]),
            key_path=str(data["key_path"]),
            source=str(data["source"]),
            source_file=str(data["source_file"]),
            key_dearmor=bool(data.get("key_dearmor", False)),
        )
    return entries


def _load_apt_packages(raw: dict[str, object]) -> dict[str, AptPackageEntry]:
    entries: dict[str, AptPackageEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise CatalogError(f"apt_packages.{name} must be a table")
        entries[name] = AptPackageEntry(
            name=name,
            description=str(data.get("description", "")),
            apt=list(data.get("apt", [])),
            apt_sources=list(data.get("apt_sources", [])),
        )
    return entries


def _load_system_commands(raw: dict[str, object]) -> dict[str, SystemInstallCommandEntry]:
    entries: dict[str, SystemInstallCommandEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise CatalogError(f"system_install_commands.{name} must be a table")
        entries[name] = SystemInstallCommandEntry(
            name=name,
            description=str(data.get("description", "")),
            command=str(data["command"]),
            path=list(data.get("path", [])),
        )
    return entries


def _load_user_commands(raw: dict[str, object]) -> dict[str, UserInstallCommandEntry]:
    entries: dict[str, UserInstallCommandEntry] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise CatalogError(f"user_install_commands.{name} must be a table")
        entries[name] = UserInstallCommandEntry(
            name=name,
            description=str(data.get("description", "")),
            command=str(data["command"]),
            path=list(data.get("path", [])),
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


def _parse_catalog(data: dict[str, object]) -> ResolvedCatalog:
    return ResolvedCatalog(
        apt_sources=_load_apt_sources(data.get("apt_sources", {})),
        apt_packages=_load_apt_packages(data.get("apt_packages", {})),
        system_install_commands=_load_system_commands(data.get("system_install_commands", {})),
        user_install_commands=_load_user_commands(data.get("user_install_commands", {})),
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
                raise CatalogError(
                    f"apt_packages.{name} references unknown apt source: {src_name}"
                )


def validate_selections(config: Config, catalog: ResolvedCatalog) -> None:
    """Validate that vm.config and agent.config selections resolve in the catalog."""
    for ref in config.vm.apt_packages:
        if ref not in catalog.apt_packages:
            raise CatalogError(
                f"vm.config.apt_packages references unknown entry: {ref}"
            )
    for ref in config.vm.system_install_commands:
        if ref not in catalog.system_install_commands:
            raise CatalogError(
                f"vm.config.system_install_commands references unknown entry: {ref}"
            )
    for ref in config.vm.admin_user_install_commands:
        if ref not in catalog.user_install_commands:
            raise CatalogError(
                f"vm.config.admin_user_install_commands references unknown entry: {ref}"
            )
    for ref in config.agent.user_install_commands:
        if ref not in catalog.user_install_commands:
            raise CatalogError(
                f"agent.config.user_install_commands references unknown entry: {ref}"
            )
