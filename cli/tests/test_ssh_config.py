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


def test_include_directive_uses_resolved_path(tmp_path: Path) -> None:
    ssh_config = tmp_path / ".ssh" / "config"
    directive = _include_directive(ssh_config)
    assert directive.startswith("Include ")
    assert "config.d/*" in directive
    # Should use forward slashes even on Windows
    path_part = directive[len("Include ") :]
    assert "\\" not in path_part


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
    ssh_config.write_text(f"Host *\n    Foo bar\n\n{_LEGACY_MARKER}\nHost awvm--old-vm\n    HostName 1.2.3.4\n")

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
    config.operator.ssh_config = ssh_config
    config.operator.ssh_config_dir = True
    config.operator.ssh_host_prefix = "awvm--"
    config.operator.ssh_agent_host_prefix = "awagent--"
    config.operator.ssh_private_key = Path("/home/user/.ssh/id_ed25519")
    return config, ssh_dir


def _mock_vm(name: str, host: str) -> MagicMock:
    vm = MagicMock()
    vm.name = name
    vm.tailscale_host = host
    vm.admin_username = "agentworks"
    return vm


def test_rebuild_config_dir(tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
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

    # Include directive added (uses ~/forward-slash form via _to_ssh_path)
    ssh_content = config.operator.ssh_config.read_text()
    assert _include_directive(config.operator.ssh_config) in ssh_content


def test_rebuild_config_dir_no_vms_removes_file(tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    conf_d = ssh_dir / "config.d"
    conf_d.mkdir()
    (conf_d / _MANAGED_CONF).write_text("old content")

    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = []

    _rebuild_config_dir(config, db)

    assert not (conf_d / _MANAGED_CONF).exists()


def test_rebuild_config_dir_cleans_legacy(tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    config.operator.ssh_config.write_text(
        f"Host *\n    Foo bar\n\n{_LEGACY_MARKER}\nHost awvm--old-vm\n    HostName 1.2.3.4\n"
    )

    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = []

    _rebuild_config_dir(config, db)

    content = config.operator.ssh_config.read_text()
    assert _LEGACY_MARKER not in content
    assert "Foo bar" in content
    directive = _include_directive(config.operator.ssh_config)
    assert directive in content


def test_format_entry_quotes_spaces_in_identity_file(tmp_path: Path) -> None:
    import re

    from agentworks.ssh_config import _format_entry

    # Use Path("/home/my user/...") for the space; on Windows resolve() will
    # prefix it with the current drive, so assert structurally rather than
    # against an exact string.
    entry = _format_entry(
        alias="test",
        hostname="1.2.3.4",
        user="me",
        identity_file=Path("/home/my user/keys/id_ed25519"),
    )
    m = re.search(r'IdentityFile "([^"]*my user[^"]*id_ed25519)"', entry)
    assert m is not None, f"expected quoted path containing space, got: {entry!r}"


def test_format_entry_no_quotes_without_spaces(tmp_path: Path) -> None:
    from agentworks.ssh_config import _format_entry

    entry = _format_entry(
        alias="test",
        hostname="1.2.3.4",
        user="me",
        identity_file=Path("/home/user/.ssh/id_ed25519"),
    )
    assert '"' not in entry


def _mock_agent(name: str, linux_user: str, vm_name: str) -> MagicMock:
    agent = MagicMock()
    agent.name = name
    agent.linux_user = linux_user
    agent.vm_name = vm_name
    return agent


def test_rebuild_config_dir_emits_per_agent_blocks(tmp_path: Path) -> None:
    """Each VM's agents get one top-level ``awagent--<name>`` Host block each."""
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("vm1", "100.64.0.1")]
    db.list_agents.return_value = [
        _mock_agent("claude", "agt-claude", "vm1"),
        _mock_agent("aider", "agt-aider", "vm1"),
    ]

    _rebuild_config_dir(config, db)

    content = (ssh_dir / "config.d" / _MANAGED_CONF).read_text()
    # Admin block still present
    assert "Host awvm--vm1\n" in content
    assert "User agentworks" in content
    # Per-agent blocks use the operator-facing agent name, not the Linux user.
    assert "Host awagent--claude\n" in content
    assert "Host awagent--aider\n" in content
    # User is still the on-VM Linux user (the implementation detail the
    # alias hides).
    assert "User agt-claude" in content
    assert "User agt-aider" in content
    # Old ``<vm>--<linux_user>`` shape is gone.
    assert "Host awvm--vm1--claude" not in content
    assert "Host awvm--vm1--agt-claude" not in content
    # All per-alias blocks share the VM's HostName (admin + 2 agents = 3
    # ``HostName`` occurrences).
    assert content.count("100.64.0.1") == 3


def test_rebuild_config_dir_no_agent_blocks_when_vm_has_none(tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("solo", "100.64.0.9")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    content = (ssh_dir / "config.d" / _MANAGED_CONF).read_text()
    assert "Host awvm--solo\n" in content
    # No --<agent> suffix anywhere on the VM block.
    assert "Host awvm--solo--" not in content
    # And no awagent-- block.
    assert "Host awagent--" not in content


def test_rebuild_config_dir_agent_blocks_global_namespace(tmp_path: Path) -> None:
    """Agent aliases are globally unique (agents.name is PRIMARY KEY); the
    alias does not embed the VM name, so a single ``awagent--<name>`` block
    is emitted per agent regardless of which VM owns them."""
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [
        _mock_vm("vm1", "100.64.0.1"),
        _mock_vm("vm2", "100.64.0.2"),
    ]

    def list_agents_side_effect(*, vm_name: str) -> list[MagicMock]:
        if vm_name == "vm1":
            return [_mock_agent("a", "agt-a", "vm1")]
        if vm_name == "vm2":
            return [_mock_agent("b", "agt-b", "vm2")]
        return []

    db.list_agents.side_effect = list_agents_side_effect

    _rebuild_config_dir(config, db)

    content = (ssh_dir / "config.d" / _MANAGED_CONF).read_text()
    assert "Host awagent--a\n" in content
    assert "Host awagent--b\n" in content
    # Each agent block carries its own VM's HostName.
    assert "100.64.0.1" in content
    assert "100.64.0.2" in content


# -- legacy rebuild: per-agent block parity --------------------------------


def test_legacy_rebuild_emits_per_agent_blocks(tmp_path: Path) -> None:
    """The legacy path (ssh_config_dir=False) emits the same per-agent
    blocks as the config.d path. Symmetry was added as part of FRD R7."""
    from agentworks.ssh_config import _legacy_rebuild

    config, ssh_dir = _mock_config(tmp_path)
    config.operator.ssh_config_dir = False  # legacy
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("vm1", "100.64.0.1")]
    db.list_agents.return_value = [
        _mock_agent("claude", "agt-claude", "vm1"),
        _mock_agent("aider", "agt-aider", "vm1"),
    ]

    _legacy_rebuild(config, db)

    content = config.operator.ssh_config.read_text()
    # Admin block still present (existing behavior)
    assert "Host awvm--vm1\n" in content
    assert "User agentworks" in content
    # Per-agent blocks use the operator-facing agent name.
    assert "Host awagent--claude\n" in content
    assert "Host awagent--aider\n" in content
    assert "User agt-claude" in content
    assert "User agt-aider" in content


def test_legacy_rebuild_no_agents_no_per_agent_blocks(tmp_path: Path) -> None:
    from agentworks.ssh_config import _legacy_rebuild

    config, ssh_dir = _mock_config(tmp_path)
    config.operator.ssh_config_dir = False
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("solo", "100.64.0.9")]
    db.list_agents.return_value = []

    _legacy_rebuild(config, db)

    content = config.operator.ssh_config.read_text()
    assert "Host awvm--solo\n" in content
    assert "Host awvm--solo--" not in content
    assert "Host awagent--" not in content


# -- R10: slug-named managed file --------------------------------------------


def test_rebuild_config_dir_uses_slug_named_file(tmp_path: Path) -> None:
    """Slug set: the managed file becomes agentworks-{slug}.conf."""
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = "team-a"
    db.list_vms.return_value = [_mock_vm("dev-vm", "100.64.0.1")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    slugged = ssh_dir / "config.d" / "agentworks-team-a.conf"
    assert slugged.exists()
    assert not (ssh_dir / "config.d" / _MANAGED_CONF).exists()
    assert "Host awvm--dev-vm" in slugged.read_text()


def test_first_sync_after_slug_removes_old_file(tmp_path: Path) -> None:
    """The pre-slug agentworks.conf must not survive to shadow fresh
    aliases (the slug arrives at first vm create, not at migration)."""
    config, ssh_dir = _mock_config(tmp_path)
    config_d = ssh_dir / "config.d"
    config_d.mkdir()
    stale = config_d / _MANAGED_CONF
    stale.write_text("Host awvm--dev-vm\n    HostName 100.64.9.9\n")

    db = MagicMock()
    db.get_setting.return_value = "team-a"
    db.list_vms.return_value = [_mock_vm("dev-vm", "100.64.0.1")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    assert not stale.exists()
    assert (config_d / "agentworks-team-a.conf").exists()


def test_declined_slug_keeps_default_file_name(tmp_path: Path) -> None:
    """The declined row (empty value) behaves like no slug."""
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = ""
    db.list_vms.return_value = [_mock_vm("dev-vm", "100.64.0.1")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    assert (ssh_dir / "config.d" / _MANAGED_CONF).exists()


