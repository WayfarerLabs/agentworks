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

    def run_side_effect(cmd, **kwargs):
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

    target.run.side_effect = run_side_effect
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

    # Should have called curl to download the key (now via run with sudo=True)
    curl_calls = [c for c in target.run.call_args_list if "curl" in str(c)]
    assert len(curl_calls) >= 1
    # Should have run apt-get update
    update_calls = [c for c in target.run.call_args_list if "apt-get update" in str(c)]
    assert len(update_calls) == 1


def test_configure_apt_sources_skips_existing(tmp_path) -> None:
    target = _make_target(key_exists=True)
    config = _make_config(apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, config, catalog, logger)

    # Should not have run apt-get update (nothing new configured)
    update_calls = [c for c in target.run.call_args_list if "apt-get update" in str(c)]
    assert len(update_calls) == 0


def test_configure_apt_sources_no_packages() -> None:
    target = MagicMock()
    config = _make_config(apt_packages=[])
    catalog = _make_catalog()
    logger = MagicMock()

    _configure_apt_sources(target, config, catalog, logger)

    # No calls at all
    target.run.assert_not_called()


def test_configure_apt_sources_resolves_arch() -> None:
    target = _make_target(key_exists=False)
    config = _make_config(apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _configure_apt_sources(target, config, catalog, logger)

    # The source line written should have arm64, not {arch}
    write_calls = [str(c) for c in target.run.call_args_list if "sources.list.d" in str(c)]
    assert any("arm64" in c for c in write_calls)
    assert not any("{arch}" in c for c in write_calls)


# -- Apt package tests --


def test_install_apt_packages_combines_sources() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)
    config = _make_config(apt=["vim", "curl"], apt_packages=["test-pkg"])
    catalog = _make_catalog()
    logger = MagicMock()
    logger.has_warnings = False

    _install_apt_packages(target, config, catalog, logger)

    # Should have a single apt-get install with all packages
    install_calls = [str(c) for c in target.run.call_args_list if "apt-get install" in str(c)]
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

    target.run.assert_not_called()


# -- Catalog command tests --


def test_run_catalog_commands_returns_path() -> None:
    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)
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
    target.run.assert_not_called()


def test_run_catalog_commands_skips_when_test_exec_found() -> None:
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
    run_calls = [str(c) for c in target.run.call_args_list]
    assert any("command -v" in c for c in run_calls)
    assert not any("curl" in c for c in run_calls)


def test_run_catalog_commands_runs_when_test_exec_missing() -> None:
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
    run_calls = [str(c) for c in target.run.call_args_list]
    assert any("curl" in c for c in run_calls)


def test_run_catalog_commands_no_test_always_runs() -> None:
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
    run_calls = [str(c) for c in target.run.call_args_list]
    assert not any("command -v" in c for c in run_calls)
    assert not any("test -f" in c for c in run_calls)
    assert not any("test -d" in c for c in run_calls)
    # Should have run the command
    assert any("curl" in c for c in run_calls)


def test_run_catalog_commands_skips_when_test_file_found() -> None:
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

    result = _run_catalog_commands(
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


def test_run_catalog_commands_runs_when_test_file_missing() -> None:
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

    result = _run_catalog_commands(
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


def test_run_catalog_commands_skips_when_test_dir_found() -> None:
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

    result = _run_catalog_commands(
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


def test_run_catalog_commands_runs_when_test_dir_missing() -> None:
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

    result = _run_catalog_commands(
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


# -- install_claude_plugins ------------------------------------------------


def test_install_claude_plugins_skips_when_claude_missing() -> None:
    """When claude is not on PATH, skip marketplace/plugin setup with a warning."""
    from agentworks.ssh import SSHError
    from agentworks.vms.initializer import install_claude_plugins

    commands: list[str] = []

    def fake_run(cmd: str, timeout: int) -> None:
        if "command -v claude" in cmd:
            raise SSHError("command failed")
        commands.append(cmd)

    install_claude_plugins(
        fake_run,
        marketplaces=["https://github.com/example/tools#v1"],
        plugins=["my-plugin@my-marketplace"],
    )

    # No marketplace or plugin commands should have been run
    assert commands == []


def test_install_claude_plugins_runs_when_claude_present() -> None:
    """When claude is on PATH, register marketplaces and install plugins."""
    from agentworks.vms.initializer import install_claude_plugins

    commands: list[str] = []

    def fake_run(cmd: str, timeout: int) -> None:
        commands.append(cmd)

    install_claude_plugins(
        fake_run,
        marketplaces=["https://github.com/example/tools#v1"],
        plugins=["my-plugin@my-marketplace"],
    )

    assert any("marketplace add" in c for c in commands)
    assert any("plugin install" in c and "my-plugin@my-marketplace" in c for c in commands)


def test_install_claude_plugins_noop_when_empty() -> None:
    """No-op when both lists are empty."""
    from agentworks.vms.initializer import install_claude_plugins

    called = False

    def fake_run(cmd: str, timeout: int) -> None:
        nonlocal called
        called = True

    install_claude_plugins(fake_run, marketplaces=[], plugins=[])
    assert not called


# -- VM hardening tests --------------------------------------------------------


def _make_hardening_target(
    *,
    sysctl_content: str | None = None,
    fstab_content: str | None = None,
) -> MagicMock:
    """ExecTarget mock parameterized by what `cat /etc/sysctl.d/...` and `cat /etc/fstab` return.

    `sysctl_content=None` simulates a missing file (cat exits non-zero).
    """
    target = MagicMock()
    write_log: list[tuple[str, str]] = []
    run_log: list[str] = []
    target.write_log = write_log
    target.run_log = run_log

    from agentworks.vms.hardening import HARDENING_FSTAB_PATH, HARDENING_SYSCTL_PATH

    def run_side_effect(cmd, **kwargs):  # noqa: ANN001 -- mock side_effect signature
        run_log.append(cmd)
        result = MagicMock()
        if cmd == f"cat {HARDENING_SYSCTL_PATH}":
            if sysctl_content is None:
                result.returncode = 1
                result.ok = False
                result.stdout = ""
            else:
                result.returncode = 0
                result.ok = True
                result.stdout = sysctl_content
        elif cmd == f"cat {HARDENING_FSTAB_PATH}":
            if fstab_content is None:
                result.returncode = 1
                result.ok = False
                result.stdout = ""
            else:
                result.returncode = 0
                result.ok = True
                result.stdout = fstab_content
        elif cmd.startswith("mktemp"):
            result.returncode = 0
            result.ok = True
            # Return a deterministic staging path so tests can assert against it.
            result.stdout = "/tmp/agw-fstab.AAAAAA" if "fstab" in cmd else "/tmp/agw-sysctl.AAAAAA"
        else:
            result.returncode = 0
            result.ok = True
            result.stdout = ""
        result.stderr = ""
        return result

    target.run.side_effect = run_side_effect
    target.write_file.side_effect = lambda path, content, **kw: write_log.append((path, content))
    return target


def test_sysctl_baseline_writes_when_missing() -> None:
    from agentworks.vms.hardening import (
        HARDENING_SYSCTL_CONTENT,
        HARDENING_SYSCTL_PATH,
        _apply_hardening_sysctl,
    )

    target = _make_hardening_target(sysctl_content=None)
    logger = MagicMock()

    _apply_hardening_sysctl(target, logger)

    # write_file was called with the canonical content
    assert any(content == HARDENING_SYSCTL_CONTENT for _, content in target.write_log)
    # install -m 0644 and sysctl --system were run
    assert any("install -m 0644" in cmd and HARDENING_SYSCTL_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "sysctl --system" for cmd in target.run_log)


def test_sysctl_baseline_noop_when_content_matches() -> None:
    from agentworks.vms.hardening import (
        HARDENING_SYSCTL_CONTENT,
        HARDENING_SYSCTL_PATH,
        _apply_hardening_sysctl,
    )

    target = _make_hardening_target(sysctl_content=HARDENING_SYSCTL_CONTENT)
    logger = MagicMock()

    _apply_hardening_sysctl(target, logger)

    # No write, no install, no sysctl reload
    assert target.write_log == []
    assert not any("install -m 0644" in cmd and HARDENING_SYSCTL_PATH in cmd for cmd in target.run_log)
    assert not any(cmd == "sysctl --system" for cmd in target.run_log)


def test_sysctl_baseline_rewrites_when_content_differs() -> None:
    from agentworks.vms.hardening import (
        HARDENING_SYSCTL_CONTENT,
        HARDENING_SYSCTL_PATH,
        _apply_hardening_sysctl,
    )

    target = _make_hardening_target(sysctl_content="# old content\n")
    logger = MagicMock()

    _apply_hardening_sysctl(target, logger)

    # Writes the canonical content and reloads
    assert any(content == HARDENING_SYSCTL_CONTENT for _, content in target.write_log)
    assert any("install -m 0644" in cmd and HARDENING_SYSCTL_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "sysctl --system" for cmd in target.run_log)


def test_fstab_hidepid_appends_when_no_proc_line() -> None:
    """No /proc line in fstab: append one with defaults,hidepid=1."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "# fstab\nUUID=root  /  ext4  defaults  0  1\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert len(target.write_log) == 1
    _, new_content = target.write_log[0]
    # New /proc line ends up in the file with hidepid=1.
    assert "proc" in new_content and "hidepid=1" in new_content
    # Original lines preserved.
    assert "UUID=root  /  ext4  defaults  0  1" in new_content
    assert any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "mount -o remount,hidepid=1 /proc" for cmd in target.run_log)


def test_fstab_hidepid_adds_option_when_proc_line_has_no_hidepid() -> None:
    """Existing /proc line with no hidepid= : append hidepid=1 to options."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "# fstab\nproc  /proc  proc  defaults  0  0\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert len(target.write_log) == 1
    _, new_content = target.write_log[0]
    # The single proc line now has both defaults AND hidepid=1; not parallel lines.
    proc_lines = [ln for ln in new_content.splitlines() if ln.split()[:1] == ["proc"]]
    assert len(proc_lines) == 1
    assert "defaults" in proc_lines[0]
    assert "hidepid=1" in proc_lines[0]
    assert any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "mount -o remount,hidepid=1 /proc" for cmd in target.run_log)


def test_fstab_hidepid_upgrades_0_to_1() -> None:
    """Existing /proc line with hidepid=0: upgrade to hidepid=1."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "proc  /proc  proc  defaults,hidepid=0  0  0\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert len(target.write_log) == 1
    _, new_content = target.write_log[0]
    assert "hidepid=1" in new_content
    assert "hidepid=0" not in new_content
    assert any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)


def test_fstab_hidepid_noop_when_already_1() -> None:
    """Existing /proc line with hidepid=1: no fstab rewrite, but still remount."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "proc  /proc  proc  defaults,hidepid=1  0  0\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert target.write_log == []
    assert not any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "mount -o remount,hidepid=1 /proc" for cmd in target.run_log)


def test_fstab_hidepid_preserves_admin_set_hidepid_2() -> None:
    """Existing /proc line with hidepid=2 (stricter): no fstab edit, remount uses 2."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "proc  /proc  proc  defaults,hidepid=2  0  0\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert target.write_log == []
    assert not any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)
    # Critical: live remount uses hidepid=2 (admin's stricter choice), not 1.
    assert any(cmd == "mount -o remount,hidepid=2 /proc" for cmd in target.run_log)


# -- Pure function tests for the fstab editor ----------------------------------


def test_ensure_proc_hidepid_appends_when_no_proc_line() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "UUID=root  /  ext4  defaults  0  1\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "appended"
    assert eff == 1
    assert "proc" in new and "hidepid=1" in new


def test_ensure_proc_hidepid_added_option() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "added-option"
    assert eff == 1
    assert "hidepid=1" in new


def test_ensure_proc_hidepid_upgraded() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=0  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "upgraded"
    assert eff == 1
    assert "hidepid=1" in new
    assert "hidepid=0" not in new


def test_ensure_proc_hidepid_no_op() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=1  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "no-op"
    assert eff == 1
    assert new == content


def test_ensure_proc_hidepid_preserves_stricter() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=2  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "preserved-stricter"
    assert eff == 2
    assert new == content


def test_ensure_proc_hidepid_preserves_trailing_comment() -> None:
    """An existing trailing comment on the /proc line is preserved on edit."""
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults  0  0  # custom proc note\n"
    new, action, _ = _ensure_proc_hidepid_in_fstab(content)
    assert action == "added-option"
    assert "# custom proc note" in new


def test_ensure_proc_hidepid_malformed() -> None:
    """A /proc line without 6 fields is left untouched (caller warns)."""
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "malformed"
    assert new == content
    assert eff == 1


# -- _reconcile_authorized_keys: owner= stage-and-install path -----------------


def _make_reconcile_target(*, mktemp_path: str = "/tmp/agw-ak.AAAAAA") -> MagicMock:
    """ExecTarget mock that returns a known mktemp path; logs run calls."""
    target = MagicMock()
    run_log: list[str] = []
    write_log: list[tuple[str, str]] = []
    target.run_log = run_log
    target.write_log = write_log

    def run_side_effect(cmd: str, **kwargs):  # noqa: ANN001 -- mock side_effect
        run_log.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.ok = True
        result.stderr = ""
        result.stdout = mktemp_path if cmd.startswith("mktemp") else ""
        return result

    target.run.side_effect = run_side_effect
    target.write_file.side_effect = lambda path, content, **kw: write_log.append((path, content))
    return target


def _make_keys_config(tmp_path) -> MagicMock:
    primary = tmp_path / "id_ed25519.pub"
    primary.write_text("ssh-ed25519 AAAA primary-key\n")
    config = MagicMock()
    config.operator.ssh_public_key = primary
    config.operator.extra_ssh_public_keys = []
    return config


def test_reconcile_authorized_keys_direct_write_when_owner_none(tmp_path) -> None:
    from agentworks.vms.initializer import _reconcile_authorized_keys

    target = _make_reconcile_target()
    config = _make_keys_config(tmp_path)
    logger = MagicMock()

    _reconcile_authorized_keys(target, config, home="/home/admin", logger=logger)

    # Direct write_file path -- single write, no install commands.
    assert len(target.write_log) == 1
    path, content = target.write_log[0]
    assert path == "/home/admin/.ssh/authorized_keys"
    assert "primary-key" in content
    # No install, no mktemp, no sudo dance.
    assert not any("install" in cmd for cmd in target.run_log)
    assert not any(cmd.startswith("mktemp") for cmd in target.run_log)


def test_reconcile_authorized_keys_stage_and_install_when_owner_set(tmp_path) -> None:
    from agentworks.vms.initializer import _reconcile_authorized_keys

    target = _make_reconcile_target(mktemp_path="/tmp/agw-ak.XXXXYY")
    config = _make_keys_config(tmp_path)
    logger = MagicMock()

    _reconcile_authorized_keys(
        target, config, home="/home/claude", logger=logger, owner="claude"
    )

    # Expected sequence:
    # 1. install -d to ensure /home/claude/.ssh exists with owner=claude
    # 2. mktemp to get a staging path
    # 3. write_file(staging, content) via scp as admin
    # 4. install (atomic rename) into /home/claude/.ssh/authorized_keys
    # 5. rm -f staging
    install_d_calls = [c for c in target.run_log if "install -d" in c]
    assert len(install_d_calls) == 1
    assert "-o claude -g claude" in install_d_calls[0]
    assert "/home/claude/.ssh" in install_d_calls[0]
    assert "0700" in install_d_calls[0]

    mktemp_calls = [c for c in target.run_log if c.startswith("mktemp")]
    assert len(mktemp_calls) == 1

    # write_file landed at the mktemp path with the keys content
    assert len(target.write_log) == 1
    staging_path, content = target.write_log[0]
    assert staging_path == "/tmp/agw-ak.XXXXYY"
    assert "primary-key" in content

    # install -o claude -g claude -m 0600 ... authorized_keys
    install_calls = [
        c for c in target.run_log
        if c.startswith("install ") and "authorized_keys" in c and "0600" in c
    ]
    assert len(install_calls) == 1
    assert "-o claude -g claude" in install_calls[0]
    assert "/home/claude/.ssh/authorized_keys" in install_calls[0]

    # cleanup
    rm_calls = [c for c in target.run_log if c.startswith("rm -f") and "/tmp/agw-ak" in c]
    assert len(rm_calls) == 1
