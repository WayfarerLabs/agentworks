"""Tests for the VM initializer's target-reconciliation helpers.

Split out of ``test_initializer.py`` (see ``_initializer_support.py`` for
the shared ``Transport``/config builders). These helpers all reconcile
state on an already-connected target rather than provisioning fresh state:
preserving SSH host keys across reboots, repairing the SVE mask on
pre-mask Apple-vz VMs, installing claude plugins, and staging
``authorized_keys`` under a non-admin owner.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from agentworks.vms.initializer import _apply_sve_mask, _preserve_ssh_host_keys

from ._initializer_support import _make_keys_config, _make_reconcile_target, _make_sve_target

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
    assert "".join(f"{line}\n" for line in SSH_PRESERVE_KEYS_LINES) == SSH_PRESERVE_KEYS_CONTENT


# -- Apple-vz SVE mask reconcile (Phase B repair of pre-mask VMs) -----------


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
    assert not any("/proc/cmdline" in c[0][0] for c in target.run.call_args_list)


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
    assert "".join(f"{line}\n" for line in SVE_NOSVE_GRUB_LINES) == SVE_NOSVE_GRUB_CONTENT


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


# -- _reconcile_authorized_keys: owner= stage-and-install path -----------------


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

    _reconcile_authorized_keys(target, config, home="/home/claude", logger=logger, owner="claude")

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
    install_calls = [c for c in target.run_log if c.startswith("install ") and "authorized_keys" in c and "0600" in c]
    assert len(install_calls) == 1
    assert "-o claude -g claude" in install_calls[0]
    assert "/home/claude/.ssh/authorized_keys" in install_calls[0]

    # cleanup
    rm_calls = [c for c in target.run_log if c.startswith("rm -f") and "/tmp/agw-ak" in c]
    assert len(rm_calls) == 1
