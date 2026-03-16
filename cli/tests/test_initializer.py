"""Tests for initializer catalog integration."""

from __future__ import annotations

from unittest.mock import MagicMock, call

from agentworks.catalog import (
    AptPackageEntry,
    AptSourceEntry,
    ResolvedCatalog,
    SystemInstallCommandEntry,
    UserInstallCommandEntry,
)
from agentworks.vms.initializer import (
    _configure_apt_sources,
    _install_apt_packages,
    _run_catalog_commands,
)


def _make_catalog() -> ResolvedCatalog:
    return ResolvedCatalog(
        apt_sources={
            "test-source": AptSourceEntry(
                name="test-source",
                description="Test apt source",
                key_url="https://example.com/key.gpg",
                key_path="/etc/apt/keyrings/test.gpg",
                source="deb [arch={arch} signed-by=/etc/apt/keyrings/test.gpg] https://example.com stable main",
                source_file="test.list",
            ),
            "dearmor-source": AptSourceEntry(
                name="dearmor-source",
                description="Source needing dearmor",
                key_url="https://example.com/key2.gpg",
                key_path="/etc/apt/keyrings/dearmor.gpg",
                source="deb [arch={arch} signed-by=/etc/apt/keyrings/dearmor.gpg] https://example.com stable main",
                source_file="dearmor.list",
                key_dearmor=True,
            ),
        },
        apt_packages={
            "test-pkg": AptPackageEntry(
                name="test-pkg",
                description="Test package",
                apt=["test-tool"],
                apt_sources=["test-source"],
            ),
            "no-source-pkg": AptPackageEntry(
                name="no-source-pkg",
                description="Package without custom source",
                apt=["vim"],
            ),
        },
        system_install_commands={
            "sys-tool": SystemInstallCommandEntry(
                name="sys-tool",
                description="System tool",
                command="curl -sL https://example.com/install.sh | sudo bash",
                path=["/usr/local/bin"],
            ),
        },
        user_install_commands={
            "user-tool": UserInstallCommandEntry(
                name="user-tool",
                description="User tool",
                command="curl -fsSL https://example.com/install.sh | bash",
                path=["~/.user-tool/bin"],
            ),
        },
    )


def _make_target(*, key_exists: bool = False) -> MagicMock:
    target = MagicMock()
    # dpkg --print-architecture
    arch_result = MagicMock()
    arch_result.stdout = "arm64\n"
    arch_result.returncode = 0
    # test -f (key existence check)
    key_result = MagicMock()
    key_result.returncode = 0 if key_exists else 1

    def run_side_effect(cmd, **kwargs):
        if "dpkg --print-architecture" in cmd:
            return arch_result
        if cmd.startswith("test -f"):
            return key_result
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 0
        return result

    target.run.side_effect = run_side_effect
    target.run_as_root.return_value = MagicMock(stdout="", stderr="", returncode=0)
    return target


def _make_config(*, apt_packages: list[str] | None = None, apt: list[str] | None = None) -> MagicMock:
    config = MagicMock()
    config.vm.apt = apt or []
    config.vm.apt_packages = apt_packages or []
    return config


# -- Apt source tests --


def test_configure_apt_sources_installs_key(tmp_path) -> None:
    target = _make_target(key_exists=False)
    config = _make_config(apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, config, catalog, logger)

    # Should have called curl to download the key
    curl_calls = [c for c in target.run_as_root.call_args_list if "curl" in str(c)]
    assert len(curl_calls) >= 1
    # Should have run apt-get update
    update_calls = [c for c in target.run_as_root.call_args_list if "apt-get update" in str(c)]
    assert len(update_calls) == 1


def test_configure_apt_sources_skips_existing(tmp_path) -> None:
    target = _make_target(key_exists=True)
    config = _make_config(apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, config, catalog, logger)

    # Should not have run apt-get update (nothing new configured)
    update_calls = [c for c in target.run_as_root.call_args_list if "apt-get update" in str(c)]
    assert len(update_calls) == 0


def test_configure_apt_sources_no_packages() -> None:
    target = MagicMock()
    config = _make_config(apt_packages=[])
    catalog = _make_catalog()
    logger = MagicMock()

    _configure_apt_sources(target, config, catalog, logger)

    # No calls at all
    target.run.assert_not_called()
    target.run_as_root.assert_not_called()


def test_configure_apt_sources_resolves_arch() -> None:
    target = _make_target(key_exists=False)
    config = _make_config(apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, config, catalog, logger)

    # The source line written should have arm64, not {arch}
    tee_calls = [str(c) for c in target.run_as_root.call_args_list if "tee" in str(c)]
    assert any("arm64" in c for c in tee_calls)
    assert not any("{arch}" in c for c in tee_calls)


# -- Apt package tests --


def test_install_apt_packages_combines_sources() -> None:
    target = MagicMock()
    target.run_as_root.return_value = MagicMock(stdout="", stderr="", returncode=0)
    config = _make_config(apt=["vim", "curl"], apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _install_apt_packages(target, config, catalog, logger)

    # Should have a single apt-get install with all packages
    install_calls = [str(c) for c in target.run_as_root.call_args_list if "apt-get install" in str(c)]
    assert len(install_calls) == 1
    assert "vim" in install_calls[0]
    assert "curl" in install_calls[0]
    assert "test-tool" in install_calls[0]


def test_install_apt_packages_empty() -> None:
    target = MagicMock()
    config = _make_config()
    catalog = _make_catalog()
    logger = MagicMock()

    _install_apt_packages(target, config, catalog, logger)

    target.run_as_root.assert_not_called()


# -- Catalog command tests --


def test_run_catalog_commands_returns_path() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0)
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target, ["user-tool"], catalog.user_install_commands, "zsh", logger,
    )

    assert result == ["~/.user-tool/bin"]


def test_run_catalog_commands_missing_entry() -> None:
    target = MagicMock()
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target, ["nonexistent"], catalog.user_install_commands, "zsh", logger,
    )

    assert result == []
    logger.warning.assert_called_once()


def test_run_catalog_commands_empty() -> None:
    target = MagicMock()
    catalog = _make_catalog()
    logger = MagicMock()

    result = _run_catalog_commands(
        target, [], catalog.user_install_commands, "zsh", logger,
    )

    assert result == []
    target.run.assert_not_called()
