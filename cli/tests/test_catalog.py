"""Tests for catalog loading, merging, and validation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentworks.catalog import (
    CatalogError,
    ResolvedCatalog,
    load_builtin_catalog,
    load_catalog,
    validate_selections,
)


def test_load_builtin_catalog() -> None:
    catalog = load_builtin_catalog()
    assert isinstance(catalog, ResolvedCatalog)
    # Spot-check expected entries
    assert "github-cli" in catalog.apt_sources
    assert "gh" in catalog.apt_packages
    assert "az-cli" in catalog.system_install_commands
    assert "bun" in catalog.user_install_commands
    assert "claude" in catalog.user_install_commands
    assert "nvm" in catalog.user_install_commands


def test_builtin_apt_source_fields() -> None:
    catalog = load_builtin_catalog()
    gh_source = catalog.apt_sources["github-cli"]
    assert gh_source.key_url.startswith("https://")
    assert gh_source.key_path.startswith("/etc/apt/keyrings/")
    assert "{arch}" in gh_source.source
    assert gh_source.source_file == "github-cli.list"


def test_builtin_apt_package_fields() -> None:
    catalog = load_builtin_catalog()
    gh_pkg = catalog.apt_packages["gh"]
    assert gh_pkg.apt == ["gh"]
    assert "github-cli" in gh_pkg.apt_sources


def test_builtin_system_command_test_fields() -> None:
    catalog = load_builtin_catalog()
    az = catalog.system_install_commands["az-cli"]
    assert az.test_exec == "az"


def test_builtin_user_command_test_fields() -> None:
    catalog = load_builtin_catalog()
    assert catalog.user_install_commands["bun"].test_exec == "bun"
    assert catalog.user_install_commands["fnm"].test_exec == "fnm"
    assert catalog.user_install_commands["claude"].test_exec == "claude"
    assert catalog.user_install_commands["oh-my-zsh"].test_dir == "~/.oh-my-zsh"
    assert catalog.user_install_commands["nvm"].test_file == "~/.nvm/nvm.sh"


def test_user_override_preserves_test_exec() -> None:
    config = _make_config_with_overrides(
        user_install_commands={
            "my-tool": {
                "command": "echo install",
                "description": "My tool",
                "test_exec": "my-tool",
            },
        },
    )
    catalog = load_catalog(config)
    assert catalog.user_install_commands["my-tool"].test_exec == "my-tool"


def test_user_override_test_defaults_none() -> None:
    config = _make_config_with_overrides(
        user_install_commands={
            "my-tool": {"command": "echo install", "description": "My tool"},
        },
    )
    catalog = load_catalog(config)
    assert catalog.user_install_commands["my-tool"].test_exec is None
    assert catalog.user_install_commands["my-tool"].test_file is None
    assert catalog.user_install_commands["my-tool"].test_dir is None


def test_legacy_test_field_rejected() -> None:
    config = _make_config_with_overrides(
        user_install_commands={
            "old-tool": {
                "command": "echo install",
                "description": "Old tool",
                "test": "old-tool",
            },
        },
    )
    with pytest.raises(CatalogError, match="'test' is not a valid field"):
        load_catalog(config)


def test_multiple_test_fields_rejected() -> None:
    config = _make_config_with_overrides(
        user_install_commands={
            "bad": {
                "command": "echo install",
                "description": "Bad",
                "test_exec": "bad",
                "test_file": "~/.bad",
            },
        },
    )
    with pytest.raises(CatalogError, match="at most one"):
        load_catalog(config)


def test_builtin_cross_references_valid() -> None:
    """All apt_sources referenced by apt_packages exist."""
    catalog = load_builtin_catalog()
    for name, pkg in catalog.apt_packages.items():
        for src in pkg.apt_sources:
            assert src in catalog.apt_sources, (
                f"apt_packages.{name} references unknown apt source: {src}"
            )


def test_user_entries_override_builtin() -> None:
    config = _make_config_with_overrides(
        user_install_commands={
            "bun": {"command": "echo custom-bun", "description": "Custom bun"},
        },
    )
    catalog = load_catalog(config)
    assert catalog.user_install_commands["bun"].command == "echo custom-bun"


def test_user_entries_extend_builtin() -> None:
    config = _make_config_with_overrides(
        user_install_commands={
            "my-tool": {"command": "echo install", "description": "My tool"},
        },
    )
    catalog = load_catalog(config)
    assert "my-tool" in catalog.user_install_commands
    # Built-in entries still present
    assert "bun" in catalog.user_install_commands


def test_bad_apt_source_reference() -> None:
    config = _make_config_with_overrides(
        apt_packages={
            "bad-pkg": {
                "description": "Bad",
                "apt": ["bad"],
                "apt_sources": ["nonexistent"],
            },
        },
    )
    with pytest.raises(CatalogError, match="unknown apt source.*nonexistent"):
        load_catalog(config)


def test_validate_selections_bad_apt_package() -> None:
    catalog = load_builtin_catalog()
    config = _make_config_with_vm(apt_packages=["nonexistent"])
    with pytest.raises(CatalogError, match="vm.config.apt_packages.*nonexistent"):
        validate_selections(config, catalog)


def test_validate_selections_bad_system_command() -> None:
    catalog = load_builtin_catalog()
    config = _make_config_with_vm(system_install_commands=["nonexistent"])
    with pytest.raises(CatalogError, match="vm.config.system_install_commands.*nonexistent"):
        validate_selections(config, catalog)


def test_validate_selections_bad_admin_user_command() -> None:
    catalog = load_builtin_catalog()
    config = _make_config_with_vm(admin_install_commands=["nonexistent"])
    with pytest.raises(CatalogError, match="vm.config.admin_install_commands.*nonexistent"):
        validate_selections(config, catalog)


def test_validate_selections_bad_agent_command() -> None:
    catalog = load_builtin_catalog()
    config = _make_config_with_agent(user_install_commands=["nonexistent"])
    with pytest.raises(CatalogError, match="agent.config.user_install_commands.*nonexistent"):
        validate_selections(config, catalog)


def test_validate_selections_valid() -> None:
    catalog = load_builtin_catalog()
    config = _make_config_with_vm(
        apt_packages=["gh"],
        system_install_commands=["az-cli"],
        admin_install_commands=["bun"],
    )
    validate_selections(config, catalog)  # should not raise


# -- Helpers -------------------------------------------------------------------


def _make_config_with_overrides(
    *,
    apt_sources: dict | None = None,
    apt_packages: dict | None = None,
    system_install_commands: dict | None = None,
    user_install_commands: dict | None = None,
) -> MagicMock:
    config = MagicMock()
    config.apt_sources = apt_sources or {}
    config.apt_packages = apt_packages or {}
    config.system_install_commands = system_install_commands or {}
    config.user_install_commands = user_install_commands or {}
    return config


def _make_config_with_vm(
    *,
    apt_packages: list[str] | None = None,
    system_install_commands: list[str] | None = None,
    admin_install_commands: list[str] | None = None,
) -> MagicMock:
    config = MagicMock()
    config.vm.apt_packages = apt_packages or []
    config.vm.system_install_commands = system_install_commands or []
    config.vm.admin_install_commands = admin_install_commands or []
    config.agent.user_install_commands = []
    return config


def _make_config_with_agent(
    *,
    user_install_commands: list[str] | None = None,
) -> MagicMock:
    config = MagicMock()
    config.vm.apt_packages = []
    config.vm.system_install_commands = []
    config.vm.admin_install_commands = []
    config.agent.user_install_commands = user_install_commands or []
    return config
