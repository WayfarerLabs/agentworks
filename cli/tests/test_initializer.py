"""Tests for the VM initializer's apt / install-command integration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agentworks.apt import AptPackageEntry, AptSourceEntry
from agentworks.install_commands import (
    SystemInstallCommandEntry,
    UserInstallCommandEntry,
)
from agentworks.vms.initializer import (
    _apply_sve_mask,
    _configure_apt_sources,
    _install_apt_packages,
    _preserve_ssh_host_keys,
    _run_install_commands,
)


def _make_entries() -> SimpleNamespace:
    """A stand-in for the four name -> entry dicts the initializer
    helpers read (resolved from the Registry in production via
    ``kind_dict``), so tests pass e.g. ``entries.apt_packages``.
    """
    return SimpleNamespace(
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


def _make_vm_template(*, apt_packages: list[str] | None = None, apt: list[str] | None = None) -> MagicMock:
    vm_template = MagicMock()
    vm_template.apt = apt or []
    vm_template.apt_packages = apt_packages or []
    return vm_template


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


# -- SSH host key preservation ---------------------------------------------


def test_preserve_ssh_host_keys_writes_dropin() -> None:
    """Writes the cloud-init drop-in as root with the canonical content."""
    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SSH_PRESERVE_KEYS_LINES,
        SSH_PRESERVE_KEYS_PATH,
    )

    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)
    logger = MagicMock()
    logger.has_warnings = False

    _preserve_ssh_host_keys(target, logger)

    assert target.run.call_count == 1
    cmd = target.run.call_args[0][0]
    assert target.run.call_args.kwargs.get("sudo") is True
    assert SSH_PRESERVE_KEYS_PATH in cmd
    assert "/etc/cloud/cloud.cfg.d" in cmd  # parent created
    for line in SSH_PRESERVE_KEYS_LINES:
        assert line in cmd
    logger.warning.assert_not_called()


def test_preserve_ssh_host_keys_warns_on_failure() -> None:
    """A failure is non-fatal: logged as a warning, no exception raised."""
    from agentworks.ssh import SSHError

    target = MagicMock()
    target.run.side_effect = SSHError("permission denied")
    logger = MagicMock()
    logger.has_warnings = False

    _preserve_ssh_host_keys(target, logger)

    logger.warning.assert_called_once()


def test_preserve_ssh_host_keys_emits_same_bytes_as_phase_a() -> None:
    """Drift guard between Phase A (heredoc) and Phase B (printf).

    Phase A writes SSH_PRESERVE_KEYS_CONTENT verbatim via a cloud-init
    heredoc. Phase B writes via `printf '%s\\n' <line> <line> ...`. Both
    produce the same on-disk bytes today; a future tweak to either
    rendering (e.g. `printf '%s\\r\\n'`, switching to `tee`, swapping the
    heredoc terminator) would silently diverge what cloud-init reads on
    Phase-A VMs vs what Phase B reconciles in. Pin the helper's printf
    format + arg list together with the byte-equivalence between
    "printf '%s\\n' SSH_PRESERVE_KEYS_LINES" and SSH_PRESERVE_KEYS_CONTENT.
    """
    import shlex

    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SSH_PRESERVE_KEYS_CONTENT,
        SSH_PRESERVE_KEYS_LINES,
    )

    target = MagicMock()
    target.run.return_value = MagicMock(stdout="", stderr="", returncode=0, ok=True)
    logger = MagicMock()

    _preserve_ssh_host_keys(target, logger)
    cmd = target.run.call_args[0][0]

    # Pin the format string and each arg so a refactor to a different
    # builder (echo -e, tee, multi-line heredoc, %s\r\n, etc.) is loud.
    assert "printf '%s\\n' " in cmd
    for line in SSH_PRESERVE_KEYS_LINES:
        assert shlex.quote(line) in cmd

    # And the byte-level result of `printf '%s\n' <lines>` must equal
    # the canonical content that Phase A writes verbatim. The constant's
    # definition already satisfies this, but locking it as a test keeps
    # the equivalence load-bearing rather than incidental.
    assert (
        "".join(f"{line}\n" for line in SSH_PRESERVE_KEYS_LINES)
        == SSH_PRESERVE_KEYS_CONTENT
    )


# -- Apple-vz SVE mask reconcile (Phase B repair of pre-mask VMs) -----------


def _make_sve_target(
    *, gated: bool, cmdline_active: bool = False, update_grub_ok: bool = True
) -> MagicMock:
    """``Transport`` mock for ``_apply_sve_mask``.

    - ``gated``: the Apple-vz + SVE gate grep succeeds (an unmasked Apple guest).
    - ``cmdline_active``: ``arm64.nosve`` is already on ``/proc/cmdline``.
    - ``update_grub_ok``: ``update-grub`` exits zero.
    Every other command (the drop-in write) succeeds.
    """
    target = MagicMock()

    def run_side_effect(cmd, **kwargs):  # noqa: ANN001 -- mock side_effect signature
        result = MagicMock(stderr="", stdout="")
        if "apple virtualization" in cmd:  # the gate
            ok = gated
        elif cmd == "update-grub":
            ok = update_grub_ok
        elif "/proc/cmdline" in cmd:
            ok = cmdline_active
        else:  # the drop-in write, etc.
            ok = True
        result.ok = ok
        result.returncode = 0 if ok else 1
        return result

    target.run.side_effect = run_side_effect
    return target


def test_apply_sve_mask_noop_when_gate_closed() -> None:
    """Non-Apple host or already-masked VM: gate closed, nothing else runs."""
    target = _make_sve_target(gated=False)
    logger = MagicMock()

    _apply_sve_mask(target, logger)

    # Only the gate probe ran: no write, no update-grub, no cmdline check.
    assert target.run.call_count == 1
    assert "apple virtualization" in target.run.call_args_list[0][0][0]
    logger.warning.assert_not_called()


def test_apply_sve_mask_installs_dropin_as_root_and_warns_restart(
    warnings: list[str],
) -> None:
    """Unmasked Apple guest: install the grub drop-in as root, run update-grub,
    and warn that a restart is needed (arm64.nosve not yet on the cmdline)."""
    import shlex

    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SVE_NOSVE_GRUB_LINES,
        SVE_NOSVE_GRUB_PATH,
    )

    target = _make_sve_target(gated=True, cmdline_active=False)
    logger = MagicMock()

    _apply_sve_mask(target, logger)

    calls = [c for c in target.run.call_args_list]
    write = next(c for c in calls if "printf" in c[0][0])
    assert write.kwargs.get("sudo") is True
    assert SVE_NOSVE_GRUB_PATH in write[0][0]
    for line in SVE_NOSVE_GRUB_LINES:
        assert shlex.quote(line) in write[0][0]
    # update-grub runs as root.
    update = next(c for c in calls if c[0][0] == "update-grub")
    assert update.kwargs.get("sudo") is True
    # The operator is told to restart; not silently left broken.
    warned = "\n".join(warnings)
    assert "arm64.nosve" in warned
    assert "Restart the VM and reinit" in warned


def test_apply_sve_mask_warns_and_stops_on_update_grub_failure(
    warnings: list[str],
) -> None:
    """update-grub failure: warn and return without claiming a restart fixes it."""
    target = _make_sve_target(gated=True, update_grub_ok=False)
    logger = MagicMock()

    _apply_sve_mask(target, logger)

    logger.warning.assert_called_once()
    assert "update-grub failed" in "\n".join(warnings)
    # No /proc/cmdline check after a failed update-grub.
    assert not any(
        "/proc/cmdline" in c[0][0] for c in target.run.call_args_list
    )


def test_apply_sve_mask_non_fatal_on_ssh_error() -> None:
    """A write failure is non-fatal: warns, does not raise (Phase B contract)."""
    from agentworks.ssh import SSHError

    target = MagicMock()

    def run_side_effect(cmd, **kwargs):  # noqa: ANN001
        if "apple virtualization" in cmd:
            return MagicMock(ok=True, returncode=0, stdout="", stderr="")
        raise SSHError("permission denied")

    target.run.side_effect = run_side_effect
    logger = MagicMock()

    _apply_sve_mask(target, logger)  # must not raise

    logger.warning.assert_called_once()


def test_apply_sve_mask_emits_same_grub_bytes_as_phase_a() -> None:
    """Drift guard between Phase A (heredoc) and Phase B (printf).

    Phase A writes SVE_NOSVE_GRUB_CONTENT verbatim via a quoted heredoc;
    Phase B writes via ``printf '%s\\n' <line> ...``. Both must produce the
    same on-disk bytes, or a masked VM and a reconciled VM would carry
    different grub drop-ins. Pins the printf format, the arg list, and the
    byte-equivalence with the canonical content.
    """
    import shlex

    from agentworks.capabilities.vm_platform.bootstrap_script import (
        SVE_NOSVE_GRUB_CONTENT,
        SVE_NOSVE_GRUB_LINES,
    )

    target = _make_sve_target(gated=True)
    logger = MagicMock()

    _apply_sve_mask(target, logger)

    write = next(c for c in target.run.call_args_list if "printf" in c[0][0])
    cmd = write[0][0]
    assert "printf '%s\\n' " in cmd
    for line in SVE_NOSVE_GRUB_LINES:
        assert shlex.quote(line) in cmd
    assert (
        "".join(f"{line}\n" for line in SVE_NOSVE_GRUB_LINES) == SVE_NOSVE_GRUB_CONTENT
    )


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
    """``Transport`` mock parameterized by what `cat /etc/sysctl.d/...` and `cat /etc/fstab` return.

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


# -- Section role/level shape (regression guard) -------------------------------


def test_vm_initialization_step_and_subresult_roles(captured_output) -> None:  # noqa: ANN001
    """Pin the fixed indentation shape for a VM Initialization section.

    Regression guard for the over-indent bug: inside a ``section("VM
    Initialization")`` (body at level 1), a primary step must carry the
    BODY role so the handler renders it at the section level (2 spaces),
    while a genuine sub-result subordinate to that step stays DETAIL so it
    nests one notch deeper (4 spaces). ``apply_vm_hardening`` exercises
    both in one section: "Applying sysctl baseline..." and "Ensuring
    hidepid=1 on /proc..." are steps, and "Added /proc entry to
    /etc/fstab" is the fstab step's sub-result.

    The captured level is the ambient section level (1) for both roles;
    the step-vs-subresult distinction rides the ROLE (the handler maps
    DETAIL to level + 1 at render time), which is exactly what the fix
    restored.
    """
    from agentworks import output
    from agentworks.output import Role
    from agentworks.vms.hardening import apply_vm_hardening

    # sysctl file missing -> "Applying sysctl baseline..."; fstab has no
    # /proc line -> "Ensuring hidepid=1 on /proc..." + "Added /proc entry
    # to /etc/fstab" sub-result.
    target = _make_hardening_target(
        sysctl_content=None,
        fstab_content="# fstab\nUUID=root  /  ext4  defaults  0  1\n",
    )
    logger = MagicMock()

    with output.section("VM Initialization"):
        apply_vm_hardening(target, logger)

    # Primary steps: BODY at the section level (renders at 2 spaces).
    assert (Role.BODY, 1, "Applying sysctl baseline...") in captured_output.lines
    assert (Role.BODY, 1, "Ensuring hidepid=1 on /proc...") in captured_output.lines
    # Genuine sub-result: DETAIL, so the handler nests it one notch deeper
    # (4 spaces) under the step above.
    assert (Role.DETAIL, 1, "Added /proc entry to /etc/fstab") in captured_output.lines
    # The step lines are NOT dimmed to DETAIL (the bug this guards against).
    assert not any(
        role is Role.DETAIL and msg == "Ensuring hidepid=1 on /proc..."
        for role, _level, msg in captured_output.lines
    )


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
    """``Transport`` mock that returns a known mktemp path; logs run calls."""
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
