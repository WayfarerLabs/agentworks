"""Tests for SSH config management (config.d and legacy approaches)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agentworks.ssh_config import (
    _LEGACY_MARKER,
    _MANAGED_CONF,
    _ensure_include,
    _include_directive,
    _rebuild_config_dir,
    _remove_legacy_section,
    ssh_host_alias,
)


def test_ssh_host_alias_default_prefix() -> None:
    assert ssh_host_alias("dev-vm") == "awvm--dev-vm"


def test_ssh_host_alias_custom_prefix() -> None:
    assert ssh_host_alias("dev-vm", "myprefix-") == "myprefix-dev-vm"


def test_include_directive_uses_absolute_path(tmp_path: Path) -> None:
    ssh_config = tmp_path / ".ssh" / "config"
    directive = _include_directive(ssh_config)
    assert str(tmp_path / ".ssh" / "config.d") in directive
    assert directive.startswith("Include /")


def test_ensure_include_creates_file(tmp_path: Path) -> None:
    ssh_config = tmp_path / ".ssh" / "config"
    _ensure_include(ssh_config)

    assert ssh_config.exists()
    directive = _include_directive(ssh_config)
    assert directive in ssh_config.read_text()


def test_ensure_include_adds_to_top(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    ssh_config.write_text("Host *\n    ServerAliveInterval 60\n")

    _ensure_include(ssh_config)

    content = ssh_config.read_text()
    lines = content.splitlines()
    directive = _include_directive(ssh_config)
    assert lines[0] == "# Added by agentworks"
    assert lines[1] == directive
    assert "ServerAliveInterval" in content


def test_ensure_include_idempotent(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    directive = _include_directive(ssh_config)
    ssh_config.write_text(f"{directive}\n\nHost *\n    Foo bar\n")

    _ensure_include(ssh_config)

    content = ssh_config.read_text()
    assert content.count(directive) == 1


def test_ensure_include_noop_if_present_elsewhere(tmp_path: Path) -> None:
    """If the directive already exists anywhere, don't add it again."""
    ssh_config = tmp_path / "config"
    directive = _include_directive(ssh_config)
    original = f"Host *\n    Foo bar\n\n{directive}\n"
    ssh_config.write_text(original)

    _ensure_include(ssh_config)

    assert ssh_config.read_text() == original


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


# -- config.d rebuild tests ------------------------------------------------


def _mock_config(tmp_path: Path) -> tuple[MagicMock, Path]:
    ssh_dir = tmp_path / ".ssh"
    ssh_config = ssh_dir / "config"
    ssh_dir.mkdir(parents=True)
    ssh_config.write_text("")

    config = MagicMock()
    config.user.ssh_config = ssh_config
    config.user.ssh_config_dir = True
    config.user.ssh_host_prefix = "awvm--"
    config.user.ssh_private_key = Path("/home/user/.ssh/id_ed25519")
    return config, ssh_dir


def _mock_vm(name: str, host: str) -> MagicMock:
    vm = MagicMock()
    vm.name = name
    vm.tailscale_host = host
    vm.vm_user = "agentworks"
    return vm


def test_rebuild_config_dir(tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.list_vms.return_value = [
        _mock_vm("dev-vm", "100.64.0.1"),
        _mock_vm("test-vm", "100.64.0.2"),
    ]

    _rebuild_config_dir(config, db)

    conf = ssh_dir / "config.d" / _MANAGED_CONF
    assert conf.exists()
    content = conf.read_text()
    assert "Host awvm--dev-vm" in content
    assert "100.64.0.1" in content
    assert "Host awvm--test-vm" in content
    assert "100.64.0.2" in content
    assert "Managed by agentworks" in content

    # Include directive added with absolute path
    ssh_content = config.user.ssh_config.read_text()
    assert str(ssh_dir / "config.d") in ssh_content


def test_rebuild_config_dir_no_vms_removes_file(tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    conf_d = ssh_dir / "config.d"
    conf_d.mkdir()
    (conf_d / _MANAGED_CONF).write_text("old content")

    db = MagicMock()
    db.list_vms.return_value = []

    _rebuild_config_dir(config, db)

    assert not (conf_d / _MANAGED_CONF).exists()


def test_rebuild_config_dir_cleans_legacy(tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    config.user.ssh_config.write_text(
        f"Host *\n    Foo bar\n\n{_LEGACY_MARKER}\n"
        "Host awvm--old-vm\n    HostName 1.2.3.4\n"
    )

    db = MagicMock()
    db.list_vms.return_value = []

    _rebuild_config_dir(config, db)

    content = config.user.ssh_config.read_text()
    assert _LEGACY_MARKER not in content
    assert "Foo bar" in content
    directive = _include_directive(config.user.ssh_config)
    assert directive in content


def test_format_entry_quotes_spaces_in_identity_file(tmp_path: Path) -> None:
    from agentworks.ssh_config import _format_entry

    entry = _format_entry(
        alias="test",
        hostname="1.2.3.4",
        user="me",
        identity_file=Path("/home/my user/keys/id_ed25519"),
    )
    assert '"' in entry
    assert '"/home/my user/keys/id_ed25519"' in entry


def test_format_entry_no_quotes_without_spaces(tmp_path: Path) -> None:
    from agentworks.ssh_config import _format_entry

    entry = _format_entry(
        alias="test",
        hostname="1.2.3.4",
        user="me",
        identity_file=Path("/home/user/.ssh/id_ed25519"),
    )
    assert '"' not in entry
