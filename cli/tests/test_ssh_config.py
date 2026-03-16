"""Tests for SSH config management (config.d and legacy approaches)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agentworks.ssh_config import (
    _INCLUDE_DIRECTIVE,
    _LEGACY_MARKER,
    _ensure_include,
    _rebuild_config_dir,
    _remove_legacy_section,
    ssh_host_alias,
)


def test_ssh_host_alias_default_prefix() -> None:
    assert ssh_host_alias("dev-vm") == "awvm--dev-vm"


def test_ssh_host_alias_custom_prefix() -> None:
    assert ssh_host_alias("dev-vm", "myprefix-") == "myprefix-dev-vm"


def test_ensure_include_creates_file(tmp_path: Path) -> None:
    ssh_config = tmp_path / ".ssh" / "config"
    _ensure_include(ssh_config)

    assert ssh_config.exists()
    assert _INCLUDE_DIRECTIVE in ssh_config.read_text()


def test_ensure_include_adds_to_top(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    ssh_config.write_text("Host *\n    ServerAliveInterval 60\n")

    _ensure_include(ssh_config)

    content = ssh_config.read_text()
    lines = content.splitlines()
    assert lines[0] == _INCLUDE_DIRECTIVE
    assert "ServerAliveInterval" in content


def test_ensure_include_idempotent(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    ssh_config.write_text(f"{_INCLUDE_DIRECTIVE}\n\nHost *\n    Foo bar\n")

    _ensure_include(ssh_config)

    content = ssh_config.read_text()
    assert content.count(_INCLUDE_DIRECTIVE) == 1


def test_remove_legacy_section(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    ssh_config.write_text(
        f"Host *\n    Foo bar\n\n{_LEGACY_MARKER}\n"
        "Host awvm--old-vm\n    HostName 1.2.3.4\n"
    )

    _remove_legacy_section(ssh_config)

    content = ssh_config.read_text()
    assert _LEGACY_MARKER not in content
    assert "awvm--old-vm" not in content
    assert "Foo bar" in content


def test_remove_legacy_section_noop_if_absent(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    original = "Host *\n    Foo bar\n"
    ssh_config.write_text(original)

    _remove_legacy_section(ssh_config)

    assert ssh_config.read_text() == original


def test_rebuild_config_dir(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_config = ssh_dir / "config"
    ssh_config.parent.mkdir(parents=True)
    ssh_config.write_text("")

    config = MagicMock()
    config.user.ssh_config = ssh_config
    config.user.ssh_config_dir = True
    config.user.ssh_host_prefix = "awvm--"
    config.user.ssh_private_key = Path("/home/user/.ssh/id_ed25519")

    vm1 = MagicMock()
    vm1.name = "dev-vm"
    vm1.tailscale_host = "100.64.0.1"
    vm1.vm_user = "agentworks"

    vm2 = MagicMock()
    vm2.name = "test-vm"
    vm2.tailscale_host = "100.64.0.2"
    vm2.vm_user = "agentworks"

    db = MagicMock()
    db.list_vms.return_value = [vm1, vm2]

    _rebuild_config_dir(config, db)

    config_d = ssh_dir / "config.d"
    assert config_d.exists()
    assert (config_d / "awvm--dev-vm.conf").exists()
    assert (config_d / "awvm--test-vm.conf").exists()

    content = (config_d / "awvm--dev-vm.conf").read_text()
    assert "Host awvm--dev-vm" in content
    assert "100.64.0.1" in content

    # Include directive added
    assert _INCLUDE_DIRECTIVE in ssh_config.read_text()


def test_rebuild_config_dir_removes_stale(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_config = ssh_dir / "config"
    config_d = ssh_dir / "config.d"
    config_d.mkdir(parents=True)
    ssh_config.write_text("")
    # Create a stale file
    (config_d / "awvm--deleted-vm.conf").write_text("Host awvm--deleted-vm\n")

    config = MagicMock()
    config.user.ssh_config = ssh_config
    config.user.ssh_config_dir = True
    config.user.ssh_host_prefix = "awvm--"
    config.user.ssh_private_key = Path("/home/user/.ssh/id_ed25519")

    db = MagicMock()
    db.list_vms.return_value = []  # no VMs

    _rebuild_config_dir(config, db)

    assert not (config_d / "awvm--deleted-vm.conf").exists()


def test_rebuild_config_dir_cleans_legacy(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_config = ssh_dir / "config"
    ssh_dir.mkdir(parents=True)
    ssh_config.write_text(
        f"Host *\n    Foo bar\n\n{_LEGACY_MARKER}\n"
        "Host awvm--old-vm\n    HostName 1.2.3.4\n"
    )

    config = MagicMock()
    config.user.ssh_config = ssh_config
    config.user.ssh_config_dir = True
    config.user.ssh_host_prefix = "awvm--"
    config.user.ssh_private_key = Path("/home/user/.ssh/id_ed25519")

    db = MagicMock()
    db.list_vms.return_value = []

    _rebuild_config_dir(config, db)

    content = ssh_config.read_text()
    assert _LEGACY_MARKER not in content
    assert "Foo bar" in content
    assert _INCLUDE_DIRECTIVE in content


def test_rebuild_preserves_non_prefixed_files(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_config = ssh_dir / "config"
    config_d = ssh_dir / "config.d"
    config_d.mkdir(parents=True)
    ssh_config.write_text("")
    # A file not matching our prefix should be left alone
    (config_d / "other-tool.conf").write_text("Host something\n")

    config = MagicMock()
    config.user.ssh_config = ssh_config
    config.user.ssh_config_dir = True
    config.user.ssh_host_prefix = "awvm--"
    config.user.ssh_private_key = Path("/home/user/.ssh/id_ed25519")

    db = MagicMock()
    db.list_vms.return_value = []

    _rebuild_config_dir(config, db)

    assert (config_d / "other-tool.conf").exists()
