"""Tests for SSH config management (config.d and legacy approaches)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.ssh_config import (
    _LEGACY_MARKER,
    _MANAGED_CONF,
    _ensure_include,
    _include_directive,
    _rebuild_config_dir,
    _remove_legacy_section,
    ssh_host_alias,
    sync_ssh_config,
)
from tests.ssh_fixtures import fake_host_key


def test_ssh_host_alias_default_prefix() -> None:
    assert ssh_host_alias("dev-vm") == "awvm--dev-vm"


def test_ssh_host_alias_custom_prefix() -> None:
    assert ssh_host_alias("dev-vm", "myprefix-") == "myprefix-dev-vm"


@pytest.mark.windows
def test_include_directive_uses_resolved_path(tmp_path: Path) -> None:
    ssh_config = tmp_path / ".ssh" / "config"
    directive = _include_directive(ssh_config)
    assert directive.startswith("Include ")
    assert "config.d/*" in directive
    # Should use forward slashes even on Windows
    path_part = directive[len("Include ") :]
    assert "\\" not in path_part


@pytest.mark.windows
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


def test_sync_ssh_config_announce_controls_the_synced_line(tmp_path: Path, captured_output) -> None:  # noqa: ANN001
    """``sync_ssh_config`` emits 'SSH config synced' by default and stays
    silent under ``announce=False``. Either way the managed file is written,
    so the silent form (``vm create``'s post-init re-sync) is a real sync,
    just without the duplicate line."""
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("dev-vm", "100.64.0.1")]
    db.list_agents.return_value = []
    conf = ssh_dir / "config.d" / _MANAGED_CONF

    sync_ssh_config(config, db)
    assert "SSH config synced" in captured_output.info
    assert conf.exists()  # the announcing sync wrote the file

    # A silent re-sync still writes the file but adds no output line.
    conf.unlink()
    before = list(captured_output.info)
    sync_ssh_config(config, db, announce=False)
    assert captured_output.info == before  # no new line
    assert conf.exists()  # still written for correctness


@pytest.mark.windows
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


@pytest.mark.windows
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


# On Windows an apostrophe with no whitespace is refused instead (tested below).
_KEY_DIRS = [("keys", False), ("my keys", True)] + ([] if sys.platform == "win32" else [("o'brien", True)])


@pytest.mark.windows
@pytest.mark.parametrize(("key_dir", "quoted"), _KEY_DIRS)
def test_the_identity_file_is_quoted_only_when_ssh_would_split_or_unquote_it(
    key_dir: str, quoted: bool, tmp_path: Path
) -> None:
    from agentworks.ssh_config import _identity_value

    key = tmp_path / key_dir / "id_ed25519"
    key.parent.mkdir()
    key.write_text("")

    value = _identity_value(key)

    assert value is not None
    assert value.startswith('"') == quoted
    assert value.strip('"').endswith(f"/{key_dir}/id_ed25519")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names")
@pytest.mark.parametrize("key_dir", ["100%", "a$b", 'a"b', "a\\b"])
def test_an_identity_file_ssh_would_misread_is_left_out_with_a_warning(
    key_dir: str,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    key = tmp_path / key_dir / "id_ed25519"
    key.parent.mkdir()
    key.write_text("")
    config.operator.ssh_private_key = key
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = [_mock_agent("claude", "agt-claude", "web")]

    _rebuild_config_dir(config, db)

    blocks = _managed_blocks((ssh_dir / "config.d" / _MANAGED_CONF).read_text())
    assert set(blocks) == {"awvm--web", "awagent--claude"}
    assert not any("IdentityFile" in block for block in blocks.values())
    assert len(captured_output.warnings) == 1


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


# -- Slug-named managed file --------------------------------------------------


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


@pytest.mark.windows
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


# -- Host-key store options ---------------------------------------------------


def _managed_blocks(content: str) -> dict[str, str]:
    """The managed file's Host blocks, keyed by alias."""
    blocks: dict[str, str] = {}
    for block in content.split("Host ")[1:]:
        alias, _, body = block.partition("\n")
        blocks[alias] = body
    return blocks


def _comment_lines(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.lstrip().startswith("#")]


def _entry_path(path: Path) -> str:
    """How an entry names ``path``: relative to ``~`` when it is under the home directory, wherever tmp lives."""
    from agentworks import ssh_config as ssh_config_module
    from agentworks.execution import trust

    return trust.option_value(ssh_config_module._to_ssh_path(path))


@pytest.mark.parametrize("rebuild", ["config_dir", "legacy"])
def test_every_entry_reads_its_store_as_the_global_file_and_writes_nowhere(tmp_path: Path, rebuild: str) -> None:
    from agentworks import ssh_config as ssh_config_module
    from agentworks.execution import trust

    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("known", "100.64.0.1"), _mock_vm("fresh", "100.64.0.2")]
    db.list_agents.side_effect = lambda vm_name: [_mock_agent(f"agent-{vm_name}", "agt-claude", vm_name)]
    trust.write_records("known", [fake_host_key()])

    if rebuild == "config_dir":
        _rebuild_config_dir(config, db)
        content = (ssh_dir / "config.d" / _MANAGED_CONF).read_text()
    else:
        ssh_config_module._legacy_rebuild(config, db)
        content = (ssh_dir / "config").read_text()

    blocks = _managed_blocks(content)
    assert set(blocks) == {"awvm--known", "awagent--agent-known", "awvm--fresh", "awagent--agent-fresh"}
    for vm_name in ("known", "fresh"):
        # ssh writes only to the user file, so the store as the global file is read-only to it, record or not.
        for alias in (f"awvm--{vm_name}", f"awagent--agent-{vm_name}"):
            assert f"    HostKeyAlias agentworks-vm-{vm_name}\n" in blocks[alias]
            assert f"    GlobalKnownHostsFile {_entry_path(trust.store_file(vm_name))}\n" in blocks[alias]
            assert f"    UserKnownHostsFile {trust.NULL_DEVICE}\n" in blocks[alias]
            assert "    StrictHostKeyChecking yes\n" in blocks[alias]
            assert "    UpdateHostKeys no\n" in blocks[alias]
            assert "    HashKnownHosts no\n" in blocks[alias]
            assert "    CheckHostIP no\n" in blocks[alias]
    # Only the entries of the VM with no record point the operator at the commands that record one.
    assert not _comment_lines(blocks["awvm--known"])
    assert not _comment_lines(blocks["awagent--agent-known"])
    assert len(_comment_lines(blocks["awvm--fresh"])) == 1
    assert len(_comment_lines(blocks["awagent--agent-fresh"])) == 1


def test_recording_or_removing_a_key_changes_only_the_missing_record_comment(tmp_path: Path) -> None:
    from agentworks.execution import trust

    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []
    conf = ssh_dir / "config.d" / _MANAGED_CONF

    def entry() -> str:
        return _managed_blocks(conf.read_text())["awvm--web"]

    def options() -> list[str]:
        return [line for line in entry().splitlines() if not line.lstrip().startswith("#")]

    _rebuild_config_dir(config, db)
    without_record = options()
    assert _comment_lines(entry())

    trust.write_records("web", [fake_host_key()])
    _rebuild_config_dir(config, db)
    assert options() == without_record
    assert not _comment_lines(entry())

    trust.remove("web")
    _rebuild_config_dir(config, db)
    assert options() == without_record
    assert _comment_lines(entry())


# Git for Windows' ssh cannot read an apostrophe path with no whitespace, so the Windows run skips that store.
_RESOLVED_STORE_NAMES = ["store", "store with space"] + ([] if sys.platform == "win32" else ["o'brien"])


@pytest.mark.windows
@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an installed OpenSSH client")
@pytest.mark.parametrize("store_name", _RESOLVED_STORE_NAMES)
def test_the_installed_client_resolves_the_managed_entry_against_the_store(
    store_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution import trust
    from tests.conftest import isolate_home

    # A home beside the store keeps the store path absolute, so ssh's own expansion of ~ is not under test.
    isolate_home(monkeypatch, tmp_path / "home")
    store = tmp_path / store_name
    monkeypatch.setattr(trust, "STORE_DIR", store)
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("known", "100.64.0.1"), _mock_vm("fresh", "100.64.0.2")]
    db.list_agents.return_value = []
    trust.write_records("known", [fake_host_key()])
    _rebuild_config_dir(config, db)

    for vm_name in ("known", "fresh"):
        # -G resolves the configuration and exits; it never connects.
        out = subprocess.run(
            ["ssh", "-G", "-F", str(ssh_dir / "config.d" / _MANAGED_CONF), f"awvm--{vm_name}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        settings = dict(line.split(" ", 1) for line in out.splitlines())

        assert settings["hostkeyalias"] == f"agentworks-vm-{vm_name}"
        assert settings["globalknownhostsfile"] == trust.store_file(vm_name).as_posix()
        assert settings["userknownhostsfile"] == trust.NULL_DEVICE
        assert settings["stricthostkeychecking"] == "true"
        assert settings["updatehostkeys"] == "false"
        assert settings["hashknownhosts"] == "no"
        assert settings["checkhostip"] == "no"
    assert settings["hostname"] == "100.64.0.2"


@pytest.mark.windows
@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an installed OpenSSH client")
def test_a_weaker_setting_read_first_still_finds_the_store_only_as_the_file_ssh_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh takes each option's first value, so an earlier ``Host *`` wins; what it accepts or learns goes to nothing.

    ssh writes known hosts only to the first user file, so the store, as the global file, stays read-only to it.
    """
    from agentworks.execution import trust
    from tests.conftest import isolate_home

    isolate_home(monkeypatch, tmp_path / "home")
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("known", "100.64.0.1"), _mock_vm("fresh", "100.64.0.2")]
    db.list_agents.return_value = []
    trust.write_records("known", [fake_host_key()])
    _rebuild_config_dir(config, db)
    combined = ssh_dir / "combined"
    managed = (ssh_dir / "config.d" / _MANAGED_CONF).read_text()
    combined.write_text(f"Host *\n    StrictHostKeyChecking accept-new\n    UpdateHostKeys yes\n\n{managed}")

    for vm_name in ("known", "fresh"):
        out = subprocess.run(
            ["ssh", "-G", "-F", str(combined), f"awvm--{vm_name}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        settings = dict(line.split(" ", 1) for line in out.splitlines())

        assert (settings["stricthostkeychecking"], settings["updatehostkeys"]) == ("accept-new", "true")
        assert settings["userknownhostsfile"] == trust.NULL_DEVICE
        assert settings["globalknownhostsfile"] == trust.store_file(vm_name).as_posix()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_an_unreadable_record_keeps_the_vms_entries_verifying_against_the_store(
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.execution import trust

    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("broken", "100.64.0.1"), _mock_vm("good", "100.64.0.2")]
    db.list_agents.return_value = []
    trust.write_records("good", [fake_host_key()])
    trust.begin_enrollment("broken")
    trust.remove("broken")
    trust.store_file("broken").mkdir()  # reading it raises

    _rebuild_config_dir(config, db)

    blocks = _managed_blocks((ssh_dir / "config.d" / _MANAGED_CONF).read_text())
    assert set(blocks) == {"awvm--broken", "awvm--good"}
    assert "HostKeyAlias agentworks-vm-broken" in blocks["awvm--broken"]
    assert "StrictHostKeyChecking yes" in blocks["awvm--broken"]
    assert "HostKeyAlias agentworks-vm-good" in blocks["awvm--good"]
    assert captured_output.warnings == []


def test_a_store_path_ssh_would_expand_leaves_each_vms_entries_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Entries that cannot name the store would trust whatever answers, so none are written for those VMs."""
    from agentworks.execution import trust

    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("one", "100.64.0.1"), _mock_vm("two", "100.64.0.2")]
    db.list_agents.return_value = [_mock_agent("claude", "agt-claude", "one")]
    monkeypatch.setattr(trust, "STORE_DIR", tmp_path / "100%")
    for name in ("one", "two"):
        trust.write_records(name, [fake_host_key()])

    _rebuild_config_dir(config, db)

    assert not (ssh_dir / "config.d" / _MANAGED_CONF).exists()
    assert len(captured_output.warnings) == 2


def test_on_windows_an_apostrophe_path_without_whitespace_is_refused_before_it_reaches_an_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Git for Windows' ssh reads neither form of such a value; the warning names the character and the path."""
    from agentworks import ssh_config as ssh_config_module
    from agentworks.execution import trust
    from agentworks.path_rendering import format_host_path

    monkeypatch.setattr(ssh_config_module, "_GIT_FOR_WINDOWS_MAY_READ", True)
    config, ssh_dir = _mock_config(tmp_path)
    key = tmp_path / "o'brien" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("")
    config.operator.ssh_private_key = key
    store = tmp_path / "o'brien-store"
    monkeypatch.setattr(trust, "STORE_DIR", store)
    trust.write_records("web", [fake_host_key()])  # only a recorded VM's entries name its store
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    assert not (ssh_dir / "config.d" / _MANAGED_CONF).exists()
    identity_warning, store_warning = captured_output.warnings
    assert format_host_path(key.resolve()) in identity_warning
    assert format_host_path(trust.store_file("web").resolve()) in store_warning

    # With whitespace the quoted form is one that client reads, so the path is written.
    spaced = tmp_path / "o'brien x" / "id_ed25519"
    spaced.parent.mkdir()
    spaced.write_text("")
    assert ssh_config_module._identity_value(spaced) == _entry_path(spaced)


@pytest.mark.windows
def test_a_home_directory_named_with_an_apostrophe_never_reaches_an_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A Windows profile such as O'Brien is a default path: the entries name files under it with ``~``."""
    from agentworks import ssh_config as ssh_config_module
    from agentworks.execution import trust
    from tests.conftest import isolate_home

    monkeypatch.setattr(ssh_config_module, "_GIT_FOR_WINDOWS_MAY_READ", True)
    home = isolate_home(monkeypatch, tmp_path / "O'Brien")
    monkeypatch.setattr(trust, "STORE_DIR", home / ".config" / "agentworks" / "known_hosts")
    config, ssh_dir = _mock_config(tmp_path)
    key = home / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("")
    config.operator.ssh_private_key = key
    trust.write_records("web", [fake_host_key()])
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    block = _managed_blocks((ssh_dir / "config.d" / _MANAGED_CONF).read_text())["awvm--web"]
    assert "    IdentityFile ~/.ssh/id_ed25519\n" in block
    assert "    GlobalKnownHostsFile ~/.config/agentworks/known_hosts/vm-web\n" in block
    assert captured_output.warnings == []


@pytest.mark.parametrize("field", ["tailscale_host", "admin_username", "linux_user", "name"])
def test_a_persisted_value_that_would_add_a_line_leaves_the_vms_entries_out(
    tmp_path: Path,
    field: str,
    captured_output,  # noqa: ANN001
) -> None:
    """Boundary: a row written by another version or by hand cannot add an option to an entry."""
    config, ssh_dir = _mock_config(tmp_path)
    vm, other = _mock_vm("web", "100.64.0.1"), _mock_vm("db", "100.64.0.2")
    agent = _mock_agent("claude", "agt-one", "web")
    target = agent if field in ("linux_user", "name") else vm
    setattr(target, field, f"{getattr(target, field)}\n    ProxyCommand sh -c 'evil'")
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [vm, other]
    db.list_agents.side_effect = lambda vm_name: [agent] if vm_name == "web" else []

    _rebuild_config_dir(config, db)

    content = (ssh_dir / "config.d" / _MANAGED_CONF).read_text()
    assert set(_managed_blocks(content)) == {"awvm--db"}
    assert "ProxyCommand" not in content
    (warning,) = captured_output.warnings
    if field == "linux_user":
        assert "claude" in warning


@pytest.mark.parametrize(
    ("host", "user"),
    [
        ("100.64.0.1", "svc$"),
        ("100.64.0.1", "1admin"),
        (".example", "agentworks"),
        ("-example", "agentworks"),
        ("fd7a:115c:a1e0::1", "agentworks"),
        ("fe80::1%eth0", "agentworks"),
        ("vm.tailnet.ts.net", "agentworks"),
    ],
)
def test_an_entry_is_written_exactly_when_the_execution_core_would_connect_there(
    tmp_path: Path,
    host: str,
    user: str,
    captured_output,  # noqa: ANN001
) -> None:
    """One rule for a row's host and user: the entries agree with ``Destination``, which every connection builds."""
    from agentworks.errors import ValidationError
    from agentworks.execution.ssh_process import Destination

    try:
        Destination(host, user)
        accepted = True
    except ValidationError:
        accepted = False
    config, ssh_dir = _mock_config(tmp_path)
    vm = _mock_vm("web", host)
    vm.admin_username = user
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [vm]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    assert (ssh_dir / "config.d" / _MANAGED_CONF).exists() is accepted
    assert len(captured_output.warnings) == (0 if accepted else 1)


@pytest.mark.windows
@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an installed OpenSSH client")
def test_an_include_of_a_config_directory_with_a_space_resolves_the_managed_entries(tmp_path: Path) -> None:
    ssh_dir = tmp_path / "my ssh"
    ssh_dir.mkdir()
    ssh_config = ssh_dir / "config"
    (ssh_dir / "config.d").mkdir()
    (ssh_dir / "config.d" / _MANAGED_CONF).write_text("Host awvm--web\n    HostName 100.64.0.9\n")

    _ensure_include(ssh_config)

    out = subprocess.run(
        ["ssh", "-G", "-F", str(ssh_config), "awvm--web"], capture_output=True, text=True, timeout=10, check=True
    ).stdout
    assert "hostname 100.64.0.9" in out.splitlines()


def test_an_unquoted_include_split_at_its_space_is_replaced_where_it_stands(tmp_path: Path) -> None:
    from agentworks import ssh_config as ssh_config_module

    ssh_dir = tmp_path / "my ssh"
    ssh_dir.mkdir()
    ssh_config = ssh_dir / "config"
    unquoted = f"Include {ssh_config_module._include_pattern(ssh_config)}"
    ssh_config.write_text(f"Host *\n    ServerAliveInterval 60\n\n{unquoted}\n")

    _ensure_include(ssh_config)

    content = ssh_config.read_text()
    assert unquoted not in content.splitlines()
    assert content == f"Host *\n    ServerAliveInterval 60\n\n{_include_directive(ssh_config)}\n"


def test_an_operators_unrelated_include_is_left_alone(tmp_path: Path) -> None:
    ssh_dir = tmp_path / "my ssh"
    ssh_dir.mkdir()
    ssh_config = ssh_dir / "config"
    original = "Include ~/.ssh/work/*\n\nHost *\n    ServerAliveInterval 60\n"
    ssh_config.write_text(original)

    _ensure_include(ssh_config)

    content = ssh_config.read_text()
    assert content.endswith(original)
    assert _include_directive(ssh_config) in content.splitlines()
