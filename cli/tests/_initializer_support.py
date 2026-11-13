"""Shared config builders for the ``test_initializer_*`` shards.

The shards cover ``agentworks.vms.initializer`` by theme; the steps they
drive run over ``tests/phase_b_support.py``'s recording access.
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
