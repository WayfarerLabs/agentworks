"""Tests for initializer catalog integration."""

from __future__ import annotations

from unittest.mock import MagicMock

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

    def run_new_side_effect(cmd, **kwargs):
        if "dpkg --print-architecture" in cmd:
            return arch_result
        if cmd.startswith("test -f"):
            return key_result
        if cmd.startswith("cat ") and key_exists:
            # Simulate existing source list file with correct content.
            # Determine which source file is being read and return matching content.
            result = MagicMock()
            result.returncode = 0
            result.ok = True
            result.stderr = ""
            if "test.list" in cmd:
                result.stdout = (
                    "deb [arch=arm64 signed-by=/etc/apt/keyrings/test.gpg] https://example.com stable main\n"
                )
            elif "dearmor.list" in cmd:
                result.stdout = (
                    "deb [arch=arm64 signed-by=/etc/apt/keyrings/dearmor.gpg] https://example.com stable main\n"
                )
            else:
                result.stdout = ""
            return result
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 0
        result.ok = True
        return result

    target.run_new.side_effect = run_new_side_effect
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

    # Should have called curl to download the key (now via run_new with sudo=True)
    curl_calls = [c for c in target.run_new.call_args_list if "curl" in str(c)]
    assert len(curl_calls) >= 1
    # Should have run apt-get update
    update_calls = [c for c in target.run_new.call_args_list if "apt-get update" in str(c)]
    assert len(update_calls) == 1


def test_configure_apt_sources_skips_existing(tmp_path) -> None:
    target = _make_target(key_exists=True)
    config = _make_config(apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, config, catalog, logger)

    # Should not have run apt-get update (nothing new configured)
    update_calls = [c for c in target.run_new.call_args_list if "apt-get update" in str(c)]
    assert len(update_calls) == 0


def test_configure_apt_sources_no_packages() -> None:
    target = MagicMock()
    config = _make_config(apt_packages=[])
    catalog = _make_catalog()
    logger = MagicMock()

    _configure_apt_sources(target, config, catalog, logger)

    # No calls at all
    target.run_new.assert_not_called()


def test_configure_apt_sources_resolves_arch() -> None:
    target = _make_target(key_exists=False)
    config = _make_config(apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, config, catalog, logger)

    # The source line written should have arm64, not {arch}
    write_calls = [str(c) for c in target.run_new.call_args_list if "sources.list.d" in str(c)]
    assert any("arm64" in c for c in write_calls)
    assert not any("{arch}" in c for c in write_calls)


# -- Apt package tests --


def test_install_apt_packages_combines_sources() -> None:
    target = MagicMock()
    target.run_new.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)
    config = _make_config(apt=["vim", "curl"], apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _install_apt_packages(target, config, catalog, logger)

    # Should have a single apt-get install with all packages
    install_calls = [str(c) for c in target.run_new.call_args_list if "apt-get install" in str(c)]
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

    target.run_new.assert_not_called()


# -- Catalog command tests --


def test_run_catalog_commands_returns_path() -> None:
    target = MagicMock()
    target.run_new.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["user-tool"],
        catalog.user_install_commands,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.user-tool/bin"]


def test_run_catalog_commands_missing_entry() -> None:
    target = MagicMock()
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["nonexistent"],
        catalog.user_install_commands,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == []
    logger.warning.assert_called_once()


def test_run_catalog_commands_empty() -> None:
    target = MagicMock()
    catalog = _make_catalog()
    logger = MagicMock()

    result = _run_catalog_commands(
        target,
        [],
        catalog.user_install_commands,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == []
    target.run_new.assert_not_called()


def test_run_catalog_commands_skips_when_test_exec_found() -> None:
    """When test_exec command exists, install is skipped but PATH additions are kept."""
    target = MagicMock()
    # command -v returns 0 (command found)
    target.run_new.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)

    entries = {
        "my-tool": UserInstallCommandEntry(
            name="my-tool",
            description="My tool",
            command="curl install.sh | bash",
            path=["~/.my-tool/bin"],
            test_exec="my-tool",
        ),
    }
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["my-tool"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    # PATH additions should still be returned
    assert result == ["~/.my-tool/bin"]
    # The install command itself should NOT have been run (only command -v was run)
    run_calls = [str(c) for c in target.run_new.call_args_list]
    assert any("command -v" in c for c in run_calls)
    assert not any("curl" in c for c in run_calls)


def test_run_catalog_commands_runs_when_test_exec_missing() -> None:
    """When test_exec command is not found, install runs normally."""
    target = MagicMock()

    def run_new_side_effect(cmd, **kwargs):
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        # command -v fails (not found), everything else succeeds
        result.returncode = 1 if "command -v" in cmd else 0
        result.ok = result.returncode == 0
        return result

    target.run_new.side_effect = run_new_side_effect

    entries = {
        "my-tool": UserInstallCommandEntry(
            name="my-tool",
            description="My tool",
            command="curl install.sh | bash",
            path=["~/.my-tool/bin"],
            test_exec="my-tool",
        ),
    }
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["my-tool"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.my-tool/bin"]
    # The install command should have been run
    run_calls = [str(c) for c in target.run_new.call_args_list]
    assert any("curl" in c for c in run_calls)


def test_run_catalog_commands_no_test_always_runs() -> None:
    """When no test is set, command always runs."""
    target = MagicMock()
    target.run_new.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)

    entries = {
        "my-tool": UserInstallCommandEntry(
            name="my-tool",
            description="My tool",
            command="curl install.sh | bash",
            path=["~/.my-tool/bin"],
            # no test field
        ),
    }
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["my-tool"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.my-tool/bin"]
    # Should NOT have run any test check
    run_calls = [str(c) for c in target.run_new.call_args_list]
    assert not any("command -v" in c for c in run_calls)
    assert not any("test -f" in c for c in run_calls)
    assert not any("test -d" in c for c in run_calls)
    # Should have run the command
    assert any("curl" in c for c in run_calls)


def test_run_catalog_commands_skips_when_test_file_found() -> None:
    """When test_file path exists, install is skipped but PATH additions are kept."""
    target = MagicMock()
    target.run_new.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)

    entries = {
        "nvm": UserInstallCommandEntry(
            name="nvm",
            description="NVM",
            command="curl install.sh | bash",
            path=["~/.nvm/bin"],
            test_file="~/.nvm/nvm.sh",
        ),
    }
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["nvm"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.nvm/bin"]
    run_calls = [str(c) for c in target.run_new.call_args_list]
    assert any("test -f" in c for c in run_calls)
    assert any("/home/agentworks/.nvm/nvm.sh" in c for c in run_calls)
    assert not any("curl" in c for c in run_calls)


def test_run_catalog_commands_runs_when_test_file_missing() -> None:
    """When test_file path does not exist, install runs normally."""
    target = MagicMock()

    def run_new_side_effect(cmd, **kwargs):
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 1 if "test -f" in cmd else 0
        result.ok = result.returncode == 0
        return result

    target.run_new.side_effect = run_new_side_effect

    entries = {
        "nvm": UserInstallCommandEntry(
            name="nvm",
            description="NVM",
            command="curl install.sh | bash",
            path=["~/.nvm/bin"],
            test_file="~/.nvm/nvm.sh",
        ),
    }
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["nvm"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.nvm/bin"]
    run_calls = [str(c) for c in target.run_new.call_args_list]
    assert any("curl" in c for c in run_calls)


def test_run_catalog_commands_skips_when_test_dir_found() -> None:
    """When test_dir path exists, install is skipped but PATH additions are kept."""
    target = MagicMock()
    target.run_new.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)

    entries = {
        "oh-my-zsh": UserInstallCommandEntry(
            name="oh-my-zsh",
            description="Oh My Zsh",
            command="sh -c install.sh",
            path=[],
            test_dir="~/.oh-my-zsh",
        ),
    }
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["oh-my-zsh"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == []
    run_calls = [str(c) for c in target.run_new.call_args_list]
    assert any("test -d" in c for c in run_calls)
    assert any("/home/agentworks/.oh-my-zsh" in c for c in run_calls)
    assert not any("sh -c" in c for c in run_calls)


def test_run_catalog_commands_runs_when_test_dir_missing() -> None:
    """When test_dir path does not exist, install runs normally."""
    target = MagicMock()

    def run_new_side_effect(cmd, **kwargs):
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 1 if "test -d" in cmd else 0
        result.ok = result.returncode == 0
        return result

    target.run_new.side_effect = run_new_side_effect

    entries = {
        "oh-my-zsh": UserInstallCommandEntry(
            name="oh-my-zsh",
            description="Oh My Zsh",
            command="sh -c install.sh",
            path=[],
            test_dir="~/.oh-my-zsh",
        ),
    }
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_catalog_commands(
        target,
        ["oh-my-zsh"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == []
    run_calls = [str(c) for c in target.run_new.call_args_list]
    assert any("sh -c" in c for c in run_calls)
