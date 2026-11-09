"""Shared builders for the ``test_initializer_*`` shards.

``test_initializer.py`` grew past 1200 lines covering several unrelated
areas of ``agentworks.vms.initializer`` (apt/install-command wiring, SSH
host-key preservation, the SVE mask reconcile, claude plugin install, and
VM hardening). It was split into sibling files grouped by theme; this
module holds the config and legacy ``Transport`` builders those shards
share; the steps on ``VMAccess`` use ``tests/phase_b_support.py``.
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


def _make_vm_template(*, apt_packages: list[str] | None = None, apt: list[str] | None = None) -> MagicMock:
    vm_template = MagicMock()
    vm_template.apt = apt or []
    vm_template.apt_packages = apt_packages or []
    return vm_template


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
    from tests.ssh_fixtures import write_test_ssh_keypair

    private = tmp_path / "id_ed25519"
    write_test_ssh_keypair(private)
    primary = tmp_path / "id_ed25519.pub"
    config = MagicMock()
    config.operator.ssh_public_key = primary
    config.operator.ssh_private_key = private
    config.operator.extra_ssh_public_keys = []
    return config
