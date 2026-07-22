"""Shared builders for the ``test_initializer_*`` shards.

``test_initializer.py`` grew past 1200 lines covering several unrelated
areas of ``agentworks.vms.initializer`` (apt/install-command wiring, SSH
host-key preservation, the SVE mask reconcile, claude plugin install, and
VM hardening). It was split into sibling files grouped by theme; this
module holds the ``Transport``/config builders those shards share so each
one gets the same fixture shapes without duplicating them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agentworks.apt import AptPackageEntry, AptSourceEntry
from agentworks.install_commands import (
    SystemInstallCommandEntry,
    UserInstallCommandEntry,
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


def _make_sve_target(*, gated: bool, cmdline_active: bool = False, update_grub_ok: bool = True) -> MagicMock:
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
