"""Tests for the VM initializer's apt / install-command integration.

Split out of ``test_initializer.py`` (see ``_initializer_support.py`` for
the shared ``Transport``/entry builders). This file covers apt source
configuration, apt package installation, and system/user install-command
execution, the three pieces the original module's docstring already
grouped together.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agentworks.install_commands import UserInstallCommandEntry
from agentworks.vms.initializer import (
    _configure_apt_sources,
    _install_apt_packages,
    _run_install_commands,
)

from ._initializer_support import _make_entries, _make_target, _make_vm_template

# -- Apt source tests --


def test_configure_apt_sources_installs_key(tmp_path) -> None:
    target = _make_target(key_exists=False)
    vm_template = _make_vm_template(apt_packages=["test-pkg"])
    entries = _make_entries()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, vm_template, entries.apt_packages, entries.apt_sources, logger)

    # Should have called curl to download the key (now via run with sudo=True)
    curl_calls = [c for c in target.run.call_args_list if "curl" in str(c)]
    assert len(curl_calls) >= 1
    # Should have run apt-get update
    update_calls = [c for c in target.run.call_args_list if "apt-get update" in str(c)]
    assert len(update_calls) == 1


def test_configure_apt_sources_skips_existing(tmp_path) -> None:
    target = _make_target(key_exists=True)
    vm_template = _make_vm_template(apt_packages=["test-pkg"])
    entries = _make_entries()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, vm_template, entries.apt_packages, entries.apt_sources, logger)

    # Should not have run apt-get update (nothing new configured)
    update_calls = [c for c in target.run.call_args_list if "apt-get update" in str(c)]
    assert len(update_calls) == 0


def test_configure_apt_sources_no_packages() -> None:
    target = MagicMock()
    vm_template = _make_vm_template(apt_packages=[])
    entries = _make_entries()
    logger = MagicMock()

    _configure_apt_sources(target, vm_template, entries.apt_packages, entries.apt_sources, logger)

    # No calls at all
    target.run.assert_not_called()


def test_configure_apt_sources_resolves_arch() -> None:
    target = _make_target(key_exists=False)
    vm_template = _make_vm_template(apt_packages=["test-pkg"])
    entries = _make_entries()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, vm_template, entries.apt_packages, entries.apt_sources, logger)

    # The source line written should have arm64, not {arch}
    write_calls = [str(c) for c in target.run.call_args_list if "sources.list.d" in str(c)]
    assert any("arm64" in c for c in write_calls)
    assert not any("{arch}" in c for c in write_calls)


# -- Apt package tests --


def test_install_apt_packages_combines_sources() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)
    vm_template = _make_vm_template(apt=["vim", "curl"], apt_packages=["test-pkg"])
    entries = _make_entries()
    logger = MagicMock()
    logger.has_warnings = False

    _install_apt_packages(target, vm_template, entries.apt_packages, logger)

    # Should have a single apt-get install with all packages
    install_calls = [str(c) for c in target.run.call_args_list if "apt-get install" in str(c)]
    assert len(install_calls) == 1
    assert "vim" in install_calls[0]
    assert "curl" in install_calls[0]
    assert "test-tool" in install_calls[0]


def test_install_apt_packages_empty() -> None:
    target = MagicMock()
    vm_template = _make_vm_template()
    entries = _make_entries()
    logger = MagicMock()

    _install_apt_packages(target, vm_template, entries.apt_packages, logger)

    target.run.assert_not_called()


# -- Install command tests --


def test_run_install_commands_returns_path() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)
    entries = _make_entries()
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_install_commands(
        target,
        ["user-tool"],
        entries.user_install_commands,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.user-tool/bin"]


def test_run_install_commands_missing_entry() -> None:
    target = MagicMock()
    entries = _make_entries()
    logger = MagicMock()
    logger.has_warnings = False

    result = _run_install_commands(
        target,
        ["nonexistent"],
        entries.user_install_commands,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == []
    logger.warning.assert_called_once()


def test_run_install_commands_empty() -> None:
    target = MagicMock()
    entries = _make_entries()
    logger = MagicMock()

    result = _run_install_commands(
        target,
        [],
        entries.user_install_commands,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == []
    target.run.assert_not_called()


def test_run_install_commands_skips_when_test_exec_found() -> None:
    """When test_exec command exists, install is skipped but PATH additions are kept."""
    target = MagicMock()
    # command -v returns 0 (command found)
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)

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

    result = _run_install_commands(
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
    run_calls = [str(c) for c in target.run.call_args_list]
    assert any("command -v" in c for c in run_calls)
    assert not any("curl" in c for c in run_calls)


def test_run_install_commands_runs_when_test_exec_missing() -> None:
    """When test_exec command is not found, install runs normally."""
    target = MagicMock()

    def run_side_effect(cmd, **kwargs):
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        # command -v fails (not found), everything else succeeds
        result.returncode = 1 if "command -v" in cmd else 0
        result.ok = result.returncode == 0
        return result

    target.run.side_effect = run_side_effect

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

    result = _run_install_commands(
        target,
        ["my-tool"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.my-tool/bin"]
    # The install command should have been run
    run_calls = [str(c) for c in target.run.call_args_list]
    assert any("curl" in c for c in run_calls)


def test_run_install_commands_no_test_always_runs() -> None:
    """When no test is set, command always runs."""
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)

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

    result = _run_install_commands(
        target,
        ["my-tool"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.my-tool/bin"]
    # Should NOT have run any test check
    run_calls = [str(c) for c in target.run.call_args_list]
    assert not any("command -v" in c for c in run_calls)
    assert not any("test -f" in c for c in run_calls)
    assert not any("test -d" in c for c in run_calls)
    # Should have run the command
    assert any("curl" in c for c in run_calls)


def test_run_install_commands_skips_when_test_file_found() -> None:
    """When test_file path exists, install is skipped but PATH additions are kept."""
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)

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

    result = _run_install_commands(
        target,
        ["nvm"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.nvm/bin"]
    run_calls = [str(c) for c in target.run.call_args_list]
    assert any("test -f" in c for c in run_calls)
    assert any("/home/agentworks/.nvm/nvm.sh" in c for c in run_calls)
    assert not any("curl" in c for c in run_calls)


def test_run_install_commands_runs_when_test_file_missing() -> None:
    """When test_file path does not exist, install runs normally."""
    target = MagicMock()

    def run_side_effect(cmd, **kwargs):
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 1 if "test -f" in cmd else 0
        result.ok = result.returncode == 0
        return result

    target.run.side_effect = run_side_effect

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

    result = _run_install_commands(
        target,
        ["nvm"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == ["~/.nvm/bin"]
    run_calls = [str(c) for c in target.run.call_args_list]
    assert any("curl" in c for c in run_calls)


def test_run_install_commands_skips_when_test_dir_found() -> None:
    """When test_dir path exists, install is skipped but PATH additions are kept."""
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)

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

    result = _run_install_commands(
        target,
        ["oh-my-zsh"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == []
    run_calls = [str(c) for c in target.run.call_args_list]
    assert any("test -d" in c for c in run_calls)
    assert any("/home/agentworks/.oh-my-zsh" in c for c in run_calls)
    assert not any("sh -c" in c for c in run_calls)


def test_run_install_commands_runs_when_test_dir_missing() -> None:
    """When test_dir path does not exist, install runs normally."""
    target = MagicMock()

    def run_side_effect(cmd, **kwargs):
        result = MagicMock()
        result.stdout = ""
        result.stderr = ""
        result.returncode = 1 if "test -d" in cmd else 0
        result.ok = result.returncode == 0
        return result

    target.run.side_effect = run_side_effect

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

    result = _run_install_commands(
        target,
        ["oh-my-zsh"],
        entries,
        "zsh",
        "/home/agentworks",
        logger,
    )

    assert result == []
    run_calls = [str(c) for c in target.run.call_args_list]
    assert any("sh -c" in c for c in run_calls)
