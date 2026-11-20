"""Tests for SSH config management (config.d and legacy approaches)."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks import ssh_config as ssh_config_module
from agentworks.errors import AuthorizationError, ConfigError
from agentworks.execution.ssh_process import VMPolicy
from agentworks.execution.trust import TrustBinding, TrustMode
from agentworks.ssh_config import (
    _LEGACY_MARKER,
    _MANAGED_CONF,
    _ensure_include,
    _include_directive,
    _rebuild_config_dir,
    _remove_legacy_section,
    ssh_host_alias,
    sync_ssh_config,
    sync_ssh_config_or_warn,
)
from tests.conftest import seed_vm_trust
from tests.ssh_fixtures import fake_host_key

needs_ssh = pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an installed OpenSSH client")


def _resolved_hostname(ssh_config: Path, alias: str) -> str:
    """The HostName the installed client resolves for ``alias``; ``-G`` never connects.

    A failure shows which client ran, its verbose account of reading the
    configuration and the files it read, since clients and their rules differ
    by platform and version.
    """
    argv = ["ssh", "-G", "-F", str(ssh_config), alias]
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if completed.returncode != 0:
        verbose = subprocess.run([*argv[:2], "-vvv", *argv[2:]], capture_output=True, text=True, timeout=10)
        files = {path: path.read_text() for path in (ssh_config, *sorted((ssh_config.parent / "config.d").glob("*")))}
        pytest.fail(
            f"{shutil.which('ssh')} -G exited {completed.returncode} under Python {sys.version}\n"
            f"verbose stderr: {verbose.stderr}\nfiles: {files}{_acls(ssh_config.parent)}"
        )
    return dict(line.split(" ", 1) for line in completed.stdout.splitlines())["hostname"]


def _acls(ssh_dir: Path) -> str:
    """On Windows, what icacls shows for ``ssh_dir``, the two directories above it and everything inside it.

    Win32-OpenSSH refuses an included file whose ACL grants anyone beyond the
    user, SYSTEM and Administrators, and a file's ACL comes from these
    directories.
    """
    if sys.platform != "win32":
        return ""
    paths = [ssh_dir.parent.parent, ssh_dir.parent, ssh_dir, *sorted(ssh_dir.rglob("*"))]
    shown = (subprocess.run(["icacls", str(path)], capture_output=True, text=True) for path in paths)
    lines = (line for run in shown for line in (run.stdout + run.stderr).splitlines())
    return "\nacls:\n" + "\n".join(line for line in lines if line and not line.startswith("Successfully processed"))


def _line_written_by_0_19(ssh_config: Path) -> str:
    """The Include line v0.19.0 wrote for ``ssh_config``: the resolved pattern unquoted, ``~`` for the home directory.

    A fixed copy of that release's writer, so no fixture follows the current one.
    """
    resolved = (ssh_config.parent / "config.d").resolve().as_posix()
    home = Path.home().resolve().as_posix()
    if resolved == home or resolved.startswith(home + "/"):
        resolved = "~" + resolved[len(home) :]
    return f"Include {resolved}/*"


def _written_by_0_19(ssh_config: Path, content: str, newline: str = "\n") -> bytes:
    """The bytes v0.19.0's ``_ensure_include`` wrote above ``content``: its marker comment, its line and a blank line.

    That release read and wrote the file in text mode, so every line ends in
    ``newline``: CRLF on Windows, LF elsewhere.
    """
    text = f"# Added by agentworks\n{_line_written_by_0_19(ssh_config)}\n\n{content}"
    return text.replace("\n", newline).encode()


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
    directive = _include_directive(ssh_config)
    _ensure_include(ssh_config, directive)

    assert ssh_config.exists()
    assert directive in ssh_config.read_text()


def test_ensure_include_adds_to_top(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    ssh_config.write_text("Host *\n    ServerAliveInterval 60\n")
    directive = _include_directive(ssh_config)

    _ensure_include(ssh_config, directive)

    content = ssh_config.read_text()
    lines = content.splitlines()
    assert lines[0] == "# Added by agentworks"
    assert lines[1] == directive
    assert "ServerAliveInterval" in content


def test_ensure_include_idempotent(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    directive = _include_directive(ssh_config)
    ssh_config.write_text(f"{directive}\n\nHost *\n    Foo bar\n")

    _ensure_include(ssh_config, directive)

    content = ssh_config.read_text()
    assert content.count(directive) == 1


def test_ensure_include_noop_if_present_elsewhere(tmp_path: Path) -> None:
    """If the directive already exists anywhere, don't add it again."""
    ssh_config = tmp_path / "config"
    directive = _include_directive(ssh_config)
    original = f"Host *\n    Foo bar\n\n{directive}\n"
    ssh_config.write_text(original)

    _ensure_include(ssh_config, directive)

    assert ssh_config.read_text() == original


@pytest.mark.parametrize("parent", ["my keys", "o'brien"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_the_include_an_earlier_release_wrote_is_rewritten_where_it_stands_and_only_there(
    parent: str, newline: str, tmp_path: Path
) -> None:
    """Copies of that line the operator wrote, at the top level or in a block, keep their meaning, so they stay."""
    ssh_config = tmp_path / parent / "config"
    ssh_config.parent.mkdir()
    directive = _include_directive(ssh_config)
    unquoted = _line_written_by_0_19(ssh_config)
    own = f"Host *\n    Foo bar\n\n{unquoted}\nHost work\n    {unquoted}\nMatch all\n    {unquoted}\n"
    ssh_config.write_bytes(_written_by_0_19(ssh_config, own, newline))

    _ensure_include(ssh_config, directive)

    assert ssh_config.read_bytes() == f"# Added by agentworks\n{directive}\n\n{own}".replace("\n", newline).encode()


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_a_file_holding_only_the_include_an_earlier_release_wrote_is_rewritten(ending: str, tmp_path: Path) -> None:
    """Releases before 0.20 left that line alone in the file when removing the legacy section left nothing else."""
    ssh_config = tmp_path / "my keys" / "config"
    ssh_config.parent.mkdir()
    directive = _include_directive(ssh_config)
    ssh_config.write_bytes(f"{_line_written_by_0_19(ssh_config)}{ending}".encode())

    _ensure_include(ssh_config, directive)

    assert ssh_config.read_bytes() == f"{directive}{ending}".encode()


@pytest.mark.windows
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_directory_whose_name_holds_a_unicode_line_separator_gets_the_include_once(
    newline: str, tmp_path: Path
) -> None:
    """ssh breaks lines only at LF, so U+2028 in the directory name is part of the directive's line."""
    ssh_config = tmp_path / "a\u2028b" / "config"
    ssh_config.parent.mkdir()
    ssh_config.write_bytes(f"Host *{newline}    Foo bar{newline}".encode())
    directive = _include_directive(ssh_config)
    _ensure_include(ssh_config, directive)
    once = ssh_config.read_bytes()

    _ensure_include(ssh_config, directive)

    assert ssh_config.read_bytes() == once


@pytest.mark.parametrize("parent", ["keys", "my keys"])
@pytest.mark.parametrize("comment", ["# {directive}", "#{unquoted}", "    #  {unquoted}"])
def test_the_managed_include_commented_out_is_an_opt_out(parent: str, comment: str, tmp_path: Path) -> None:
    ssh_config = tmp_path / parent / "config"
    ssh_config.parent.mkdir()
    directive = _include_directive(ssh_config)
    original = comment.format(directive=directive, unquoted=_line_written_by_0_19(ssh_config)) + "\nHost *\n"
    ssh_config.write_text(original)

    _ensure_include(ssh_config, directive)

    assert ssh_config.read_text() == original


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_any_other_comment_naming_the_include_does_not_count(newline: str, tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    directive = _include_directive(ssh_config)
    original = f"# see {directive}{newline}Host *{newline}"
    ssh_config.write_bytes(original.encode())

    _ensure_include(ssh_config, directive)

    added = f"# Added by agentworks{newline}{directive}{newline}{newline}"
    assert ssh_config.read_bytes() == (added + original).encode()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_a_rewritten_config_keeps_its_mode_and_group(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    ssh_config.write_text("Host *\n    Foo bar\n")
    ssh_config.chmod(0o640)
    other_groups = [gid for gid in os.getgroups() if gid != ssh_config.stat().st_gid]
    if other_groups:
        os.chown(ssh_config, -1, other_groups[0])
    before = ssh_config.stat()

    _ensure_include(ssh_config, _include_directive(ssh_config))

    after = ssh_config.stat()
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_a_new_config_gets_the_mode_of_any_new_file_beside_it(tmp_path: Path) -> None:
    """The umask and any default ACL on the directory decide, as for a file the operator creates there.

    An uncommon umask, so neither 0644 nor 0600 written explicitly would match.
    """
    ssh_config = tmp_path / "config"
    ordinary = tmp_path / "ordinary"
    previous = os.umask(0o027)
    try:
        _ensure_include(ssh_config, _include_directive(ssh_config))
        ordinary.write_text("")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(ssh_config.stat().st_mode) == stat.S_IMODE(ordinary.stat().st_mode)


def test_bytes_that_are_not_utf8_survive_a_rewrite(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    original = "# caf\u00e9 in Latin-1\nHost *\n    Foo bar\n".encode("latin-1")
    ssh_config.write_bytes(original)
    directive = _include_directive(ssh_config)

    _ensure_include(ssh_config, directive)

    assert ssh_config.read_bytes() == f"# Added by agentworks\n{directive}\n\n".encode() + original


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_symlinked_config_is_updated_through_its_link(tmp_path: Path) -> None:
    """A dotfiles checkout often owns ~/.ssh/config; the rewrite must not replace the link with a copy."""
    target = tmp_path / "dotfiles" / "ssh-config"
    target.parent.mkdir()
    target.write_text("Host *\n    Foo bar\n")
    ssh_config = tmp_path / ".ssh" / "config"
    ssh_config.parent.mkdir()
    ssh_config.symlink_to(target)
    directive = _include_directive(ssh_config)

    _ensure_include(ssh_config, directive)

    assert ssh_config.is_symlink()
    assert directive in target.read_text().splitlines()


@needs_ssh
def test_the_installed_client_reads_the_file_once_a_stale_include_with_an_apostrophe_is_rewritten(
    tmp_path: Path,
) -> None:
    """An unquoted apostrophe makes ssh reject the whole file, so adding a correct line would fix nothing."""
    config, _ = _mock_config(tmp_path / "o'brien")
    ssh_config = config.operator.ssh_config
    ssh_config.write_bytes(_written_by_0_19(ssh_config, ""))
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    assert _resolved_hostname(ssh_config, "awvm--web") == "100.64.0.1"


def test_remove_legacy_section(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    ssh_config.write_text(f"Host *\n    Foo bar\n\n{_LEGACY_MARKER}\nHost awvm--old-vm\n    HostName 1.2.3.4\n")

    _remove_legacy_section(ssh_config)

    content = ssh_config.read_text()
    assert _LEGACY_MARKER not in content
    assert "awvm--old-vm" not in content
    assert "Foo bar" in content


def test_removing_the_legacy_section_keeps_crlf_line_endings(tmp_path: Path) -> None:
    ssh_config = tmp_path / "config"
    ssh_config.write_bytes(f"Host *\r\n    Foo bar\r\n\r\n{_LEGACY_MARKER}\r\nHost awvm--old-vm\r\n".encode())

    _remove_legacy_section(ssh_config)

    assert ssh_config.read_bytes() == b"Host *\r\n    Foo bar\r\n"


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
    # Absolute on every platform, so the entries carry an IdentityFile line.
    config.operator.ssh_private_key = tmp_path / "id_ed25519"
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


def _db_with_one_vm() -> MagicMock:
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []
    return db


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root may write a read-only file")
@pytest.mark.parametrize(
    "shape", ["file", pytest.param("symlink", marks=pytest.mark.skipif(sys.platform == "win32", reason="symlinks"))]
)
def test_a_config_the_user_may_not_write_is_refused_and_left_as_it_was(
    shape: str,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    """On Windows the read-only attribute is the refusal; the warning carries the error's hint."""
    config, _ = _mock_config(tmp_path)
    ssh_config = config.operator.ssh_config
    original = b"Host *\n    Foo bar\n"
    target = ssh_config if shape == "file" else tmp_path / "dotfiles-config"
    target.write_bytes(original)
    if shape == "symlink":
        ssh_config.unlink()
        ssh_config.symlink_to(target)
    target.chmod(0o444)

    with pytest.raises(AuthorizationError) as caught:
        sync_ssh_config(config, _db_with_one_vm())
    sync_ssh_config_or_warn(config, _db_with_one_vm())

    assert target.read_bytes() == original
    assert ssh_config.is_symlink() == (shape == "symlink")
    assert caught.value.hint is not None
    (warning,) = captured_output.warnings
    assert caught.value.hint in warning


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root may read a mode 000 file")
def test_a_config_the_user_may_not_read_is_refused_and_left_as_it_was(
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    config, _ = _mock_config(tmp_path)
    ssh_config = config.operator.ssh_config
    original = b"Host *\n    Foo bar\n"
    ssh_config.write_bytes(original)
    ssh_config.chmod(0o000)
    before = ssh_config.stat()
    try:
        with pytest.raises(AuthorizationError) as caught:
            sync_ssh_config(config, _db_with_one_vm())
        sync_ssh_config_or_warn(config, _db_with_one_vm())
        after = ssh_config.stat()
    finally:
        ssh_config.chmod(0o600)

    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
    assert ssh_config.read_bytes() == original
    assert caught.value.hint is not None
    (warning,) = captured_output.warnings
    assert caught.value.hint in warning


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes and symlinks")
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root may search any directory")
def test_a_config_linked_through_a_directory_the_user_may_not_search_is_refused_and_left_as_it_was(
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    config, _ = _mock_config(tmp_path)
    ssh_config = config.operator.ssh_config
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "config"
    original = b"Host *\n    Foo bar\n"
    target.write_bytes(original)
    ssh_config.unlink()
    ssh_config.symlink_to(target)
    locked.chmod(0o600)
    try:
        with pytest.raises(AuthorizationError) as caught:
            sync_ssh_config(config, _db_with_one_vm())
        sync_ssh_config_or_warn(config, _db_with_one_vm())
    finally:
        locked.chmod(0o700)

    assert ssh_config.is_symlink()
    assert target.read_bytes() == original
    assert caught.value.hint is not None
    (warning,) = captured_output.warnings
    assert caught.value.hint in warning


@pytest.mark.parametrize(("error", "refused"), [(errno.EROFS, True), (errno.EIO, False)])
def test_only_a_refused_open_is_reported_as_the_operators_to_fix(
    error: int,
    refused: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config linked into a read-only mount, as home-manager links into ``/nix/store``, is the operator's to fix."""
    ssh_config = tmp_path / "config"
    original = b"Host *\n    Foo bar\n"
    ssh_config.write_bytes(original)
    real_open = open

    def open_on_a_failing_mount(file: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if "w" in mode:
            raise OSError(error, os.strerror(error), str(file))
        return real_open(file, mode, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(ssh_config_module, "open", open_on_a_failing_mount, raising=False)

    with pytest.raises(AuthorizationError if refused else OSError):
        _ensure_include(ssh_config, _include_directive(ssh_config))
    assert ssh_config.read_bytes() == original


@pytest.mark.parametrize("message", ["cannot update the file.", "cannot update the file"])
def test_a_warning_joins_the_errors_hint_after_exactly_one_period(
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    def failing_sync(*args: object, **kwargs: object) -> None:
        raise AuthorizationError(message, hint="Then retry.")

    monkeypatch.setattr(ssh_config_module, "sync_ssh_config", failing_sync)

    sync_ssh_config_or_warn(MagicMock(), MagicMock())

    (warning,) = captured_output.warnings
    assert warning.endswith("cannot update the file. Then retry.")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory modes")
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root may write into a read-only directory")
def test_a_writable_config_in_a_read_only_directory_is_updated(tmp_path: Path) -> None:
    """Only the file's own permissions decide, as for any in-place edit."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    ssh_config = ssh_dir / "config"
    ssh_config.write_text("Host *\n    Foo bar\n")
    directive = _include_directive(ssh_config)
    ssh_dir.chmod(0o555)
    try:
        _ensure_include(ssh_config, directive)
    finally:
        ssh_dir.chmod(0o755)

    assert ssh_config.read_text() == f"# Added by agentworks\n{directive}\n\nHost *\n    Foo bar\n"


@pytest.mark.windows
def test_a_config_is_edited_in_place_keeping_its_inode_and_hard_links(tmp_path: Path) -> None:
    """A replacement would leave a hard link holding the old content, and on Windows drop the file's own ACL."""
    ssh_config = tmp_path / "config"
    ssh_config.write_text("Host *\n    Foo bar\n")
    linked = tmp_path / "linked-config"
    os.link(ssh_config, linked)
    before = ssh_config.stat().st_ino
    directive = _include_directive(ssh_config)

    _ensure_include(ssh_config, directive)

    assert ssh_config.stat().st_ino == before
    assert directive in linked.read_text().splitlines()


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
    assert _include_directive(config.operator.ssh_config) in content


def test_a_stale_include_beside_a_legacy_section_is_rewritten_and_the_section_removed(tmp_path: Path) -> None:
    config, _ = _mock_config(tmp_path / "my keys")
    ssh_config = config.operator.ssh_config
    directive = _include_directive(ssh_config)
    legacy = f"{_LEGACY_MARKER}\nHost awvm--old\n    HostName 1.2.3.4\n"
    ssh_config.write_bytes(_written_by_0_19(ssh_config, f"Host *\n    Foo bar\n\n{legacy}"))
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = []

    _rebuild_config_dir(config, db)

    assert ssh_config.read_text() == f"# Added by agentworks\n{directive}\n\nHost *\n    Foo bar\n"


def test_an_include_inside_the_legacy_section_is_restored_when_the_section_goes(tmp_path: Path) -> None:
    config, _ = _mock_config(tmp_path)
    ssh_config = config.operator.ssh_config
    directive = _include_directive(ssh_config)
    ssh_config.write_text(
        f"Host *\n    Foo bar\n\n{_LEGACY_MARKER}\n{directive}\nHost awvm--old\n    HostName 1.2.3.4\n"
    )
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = []

    _rebuild_config_dir(config, db)

    content = ssh_config.read_text()
    assert _LEGACY_MARKER not in content
    assert content.splitlines().count(directive) == 1


@pytest.mark.windows
@pytest.mark.parametrize(("key_dir", "quoted"), [("keys", False), ("my keys", True), ("o'brien", True)])
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names and symlinks")
@pytest.mark.parametrize(("link_dir", "target_dir", "expected_ok"), [("keys", "100%", True), ("100%", "keys", False)])
def test_the_managed_entry_and_the_execution_core_judge_the_same_identity_path(
    link_dir: str,
    target_dir: str,
    expected_ok: bool,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    config, ssh_dir = _mock_config(tmp_path)
    target = tmp_path / target_dir / "id_ed25519"
    target.parent.mkdir()
    target.write_text("")
    key = tmp_path / link_dir / "id_ed25519"
    key.parent.mkdir()
    key.symlink_to(target)
    config.operator.ssh_private_key = key
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []
    seed_vm_trust("web")

    _rebuild_config_dir(config, db)

    block = _managed_blocks((ssh_dir / "config.d" / _MANAGED_CONF).read_text())["awvm--web"]
    if expected_ok:
        policy = VMPolicy(key, TrustBinding(TrustMode.VM, "web")).options()
        assert f"IdentityFile={key.as_posix()}" in policy
        assert f"    IdentityFile {key.as_posix()}\n" in block  # the link, as the core passes it
        assert not captured_output.warnings
    else:
        with pytest.raises(ConfigError):
            VMPolicy(key, TrustBinding(TrustMode.VM, "web")).options()
        assert "IdentityFile" not in block
        assert len(captured_output.warnings) == 1


@pytest.mark.windows
def test_the_identity_file_is_written_as_the_configured_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssh reads ``~`` as the passwd home, which need not be ``$HOME``, so the entry never abbreviates."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config, ssh_dir = _mock_config(tmp_path)
    key = tmp_path / "keys" / "id_ed25519"
    config.operator.ssh_private_key = key
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    block = _managed_blocks((ssh_dir / "config.d" / _MANAGED_CONF).read_text())["awvm--web"]
    assert f"    IdentityFile {key.as_posix()}\n" in block


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_legacy_rebuild_keeps_the_operators_bytes_line_endings_and_mode(tmp_path: Path) -> None:
    from agentworks.ssh_config import _legacy_rebuild

    config, _ = _mock_config(tmp_path)
    config.operator.ssh_config_dir = False
    ssh_config = config.operator.ssh_config
    operator_section = "# caf\u00e9 in Latin-1\r\nHost *\r\n    Foo bar\r\n".encode("latin-1")
    ssh_config.write_bytes(operator_section)
    ssh_config.chmod(0o640)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("solo", "100.64.0.9")]
    db.list_agents.return_value = []

    _legacy_rebuild(config, db)

    written = ssh_config.read_bytes()
    assert written.startswith(operator_section)
    assert b"Host awvm--solo\r\n    HostName 100.64.0.9\r\n" in written
    assert b"\n" not in written.replace(b"\r\n", b"")
    assert stat.S_IMODE(ssh_config.stat().st_mode) == 0o640


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


@pytest.mark.parametrize("rebuild", ["config_dir", "legacy"])
def test_store_options_appear_exactly_for_vms_with_a_record(tmp_path: Path, rebuild: str) -> None:
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
    for alias in ("awvm--known", "awagent--agent-known"):
        assert "    HostKeyAlias agentworks-vm-known\n" in blocks[alias]
        assert f"    UserKnownHostsFile {trust.store_file('known').as_posix()}\n" in blocks[alias]
        assert "    StrictHostKeyChecking yes\n" in blocks[alias]
        assert "    UpdateHostKeys no\n" in blocks[alias]
        assert "    HashKnownHosts no\n" in blocks[alias]
        assert "    CheckHostIP no\n" in blocks[alias]
        assert f"    GlobalKnownHostsFile {trust._NULL_DEVICE}\n" in blocks[alias]
    for alias in ("awvm--fresh", "awagent--agent-fresh"):
        assert "IdentityFile" in blocks[alias]
        assert not any(
            option in blocks[alias]
            for option in ("HostKeyAlias", "UserKnownHostsFile", "StrictHostKeyChecking", "UpdateHostKeys")
        )


def test_an_entry_gains_and_loses_the_store_options_with_the_record(tmp_path: Path) -> None:
    from agentworks.execution import trust

    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []
    conf = ssh_dir / "config.d" / _MANAGED_CONF

    _rebuild_config_dir(config, db)
    assert "HostKeyAlias" not in conf.read_text()

    trust.write_records("web", [fake_host_key()])
    _rebuild_config_dir(config, db)
    assert "HostKeyAlias agentworks-vm-web" in conf.read_text()

    trust.remove("web")
    _rebuild_config_dir(config, db)
    assert "HostKeyAlias" not in conf.read_text()


@pytest.mark.windows
@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an installed OpenSSH client")
@pytest.mark.parametrize("store_name", ["store", "store with space", "o'brien"])
def test_the_installed_client_resolves_the_managed_entry_against_the_store(
    store_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution import trust

    store = tmp_path / store_name
    monkeypatch.setattr(trust, "STORE_DIR", store)
    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("known", "100.64.0.1")]
    db.list_agents.return_value = []
    trust.write_records("known", [fake_host_key()])
    _rebuild_config_dir(config, db)

    # -G resolves the configuration and exits; it never connects.
    out = subprocess.run(
        ["ssh", "-G", "-F", str(ssh_dir / "config.d" / _MANAGED_CONF), "awvm--known"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout
    settings = dict(line.split(" ", 1) for line in out.splitlines())

    assert settings["hostname"] == "100.64.0.1"
    assert settings["hostkeyalias"] == "agentworks-vm-known"
    assert settings["userknownhostsfile"] == trust.store_file("known").as_posix()
    assert settings["stricthostkeychecking"] == "true"
    assert settings["updatehostkeys"] == "false"
    assert settings["hashknownhosts"] == "no"
    assert settings["checkhostip"] == "no"
    assert settings["globalknownhostsfile"] == trust._NULL_DEVICE


def _give_the_acl_of_a_profiles_ssh_directory(directory: Path) -> None:
    """On Windows, give ``directory`` the DACL a profile's ``~/.ssh`` has: the user, SYSTEM and Administrators.

    pytest creates its temporary directories with mode 0o700, which Python on
    Windows turns into the protected DACL ``D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)
    (A;OICI;FA;;;OW)``. Directories created below it get those entries, OWNER
    RIGHTS (S-1-3-4) among them, as explicit entries, not inherited ones
    (icacls shows no ``(I)`` on them), so removing inheritance keeps them and
    OWNER RIGHTS has to be removed by name. Win32-OpenSSH checks the
    permissions of every file an Include pulls in and refuses one granting
    OWNER RIGHTS, so a test tree must look like a real profile's instead.
    """
    if sys.platform != "win32":
        return
    grants = [f"{os.environ['USERNAME']}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]
    argv = ["icacls", str(directory), "/inheritance:r", "/grant:r", *grants, "/remove:g", "*S-1-3-4"]
    done = subprocess.run(argv, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.windows
@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an installed OpenSSH client")
@pytest.mark.parametrize("parent", ["home", "home with space", "o'brien"])
def test_the_installed_client_reaches_the_managed_entries_through_the_include(parent: str, tmp_path: Path) -> None:
    config, ssh_dir = _mock_config(tmp_path / parent)
    _give_the_acl_of_a_profiles_ssh_directory(ssh_dir)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []
    _rebuild_config_dir(config, db)

    # An Include that names nothing leaves the alias as the host name.
    assert _resolved_hostname(config.operator.ssh_config, "awvm--web") == "100.64.0.1"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names")
@pytest.mark.parametrize("parent", ["100%d", "a$b", "a${HOME}b", "b[1]x", "g*x", "q?x", 'a"b', "a\\b", "a\tb"])
def test_a_config_directory_an_include_cannot_name_gets_no_include_with_a_warning(
    parent: str,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    """OpenSSH 9.9 and later expand ``%`` tokens and ``${VAR}`` in Include; earlier ones do not."""
    config, ssh_dir = _mock_config(tmp_path / parent)
    config.operator.ssh_private_key = tmp_path / "id_ed25519"
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("web", "100.64.0.1")]
    db.list_agents.return_value = []

    _rebuild_config_dir(config, db)

    assert "Include" not in config.operator.ssh_config.read_text()
    assert "Host awvm--web" in (ssh_dir / "config.d" / _MANAGED_CONF).read_text()
    assert len(captured_output.warnings) == 1


def _config_under_an_unnameable_directory(tmp_path: Path, shape: str, name: str = 'a"b') -> MagicMock:
    """A config whose config.d ssh cannot name: in such a directory, or reached through a ``.ssh`` linked to one."""
    if shape == "directory":
        config, _ = _mock_config(tmp_path / name)
    else:
        config, ssh_dir = _mock_config(tmp_path / "home")
        target = tmp_path / name / "ssh"
        target.parent.mkdir()
        ssh_dir.rename(target)
        ssh_dir.symlink_to(target, target_is_directory=True)
    # A key path ssh can read, so the only warning is about the Include.
    config.operator.ssh_private_key = tmp_path / "id_ed25519"
    return config


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names and symlinks")
@pytest.mark.parametrize(
    ("shape", "name"),
    [
        ("directory", 'a"b'),
        ("directory", "100%d"),
        ("directory", "g*x"),
        ("directory", "line\nbreak"),
        ("symlinked .ssh", 'a"b'),
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_the_include_an_earlier_release_wrote_for_an_unnameable_directory_is_removed_with_a_warning(
    shape: str,
    name: str,
    newline: str,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    """Only that release's marked line goes, with its marker, matched whole where the directory name holds a newline.

    Copies of it the operator wrote, at the top level or in a block, stay, and
    the file keeps its other bytes, its inode and its mode.
    """
    config = _config_under_an_unnameable_directory(tmp_path, shape, name)
    ssh_config = config.operator.ssh_config
    earlier = _line_written_by_0_19(ssh_config)
    kept = (
        f"# Include {earlier}\nInclude ~/.ssh/extra/*\n\n{earlier}\nHost other\n    HostName 10.0.0.9\n"
        f"    {earlier}\nMatch all\n    {earlier}\n"
    )
    ssh_config.write_bytes(_written_by_0_19(ssh_config, f"{kept}    ServerAliveInterval 60\n", newline))
    ssh_config.chmod(0o640)
    before = ssh_config.stat()

    _rebuild_config_dir(config, _db_with_one_vm())

    after = ssh_config.stat()
    assert ssh_config.read_bytes() == f"\n{kept}    ServerAliveInterval 60\n".replace("\n", newline).encode()
    assert (after.st_ino, stat.S_IMODE(after.st_mode)) == (before.st_ino, 0o640)
    (warning,) = captured_output.warnings
    assert earlier in warning
    assert "Host awvm--web" in (ssh_config.parent / "config.d" / _MANAGED_CONF).read_text()


@pytest.mark.windows
@pytest.mark.parametrize(
    ("marked", "newline"), [(True, "\n"), (True, "\r\n"), (False, ""), (False, "\n"), (False, "\r\n")]
)
def test_a_0_19_include_for_a_directory_with_a_percent_is_removed_with_or_without_its_marker(
    marked: bool,
    newline: str,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    """0.19 wrote the line alone when removing its legacy section left nothing else, and wrote CRLF on Windows."""
    config = _config_under_an_unnameable_directory(tmp_path, "directory", "100%d")
    ssh_config = config.operator.ssh_config
    earlier = _line_written_by_0_19(ssh_config)
    rest = "Host other\n    HostName 10.0.0.9\n"
    if marked:
        ssh_config.write_bytes(_written_by_0_19(ssh_config, rest, newline))
        expected = f"\n{rest}".replace("\n", newline).encode()
    else:
        ssh_config.write_bytes(f"{earlier}{newline}".encode())
        expected = b""
    before = ssh_config.stat().st_ino

    _rebuild_config_dir(config, _db_with_one_vm())

    assert ssh_config.read_bytes() == expected
    assert ssh_config.stat().st_ino == before
    (warning,) = captured_output.warnings
    assert earlier in warning


@pytest.mark.parametrize(
    ("written", "left"),
    [
        ("# Added by agentworks\n{line}\n{line}", "{line}"),
        ("# Added by agentworks\r\n{line}\r\n{line}\n", "{line}\n"),
        ("# Added by agentworks\n{line}\n{line}\r\n", "{line}\r\n"),
    ],
)
def test_a_copy_left_once_the_marked_line_goes_is_the_operators_and_stays(
    written: str,
    left: str,
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    """Only a file that held the bare line when found is that release's; one the removal leaves so is not."""
    config = _config_under_an_unnameable_directory(tmp_path, "directory", "100%d")
    ssh_config = config.operator.ssh_config
    line = _line_written_by_0_19(ssh_config)
    ssh_config.write_bytes(written.format(line=line).encode())

    _rebuild_config_dir(config, _db_with_one_vm())

    assert ssh_config.read_bytes() == left.format(line=line).encode()


@needs_ssh
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names and symlinks")
@pytest.mark.parametrize(
    ("shape", "name"), [("directory", 'a"b'), ("symlinked .ssh", 'a"b'), ("directory", "line\nbreak")]
)
def test_the_installed_client_reads_the_file_again_once_the_include_it_rejected_is_removed(
    shape: str,
    name: str,
    tmp_path: Path,
) -> None:
    """A quote left unbalanced, or a line broken in two, makes ssh reject the whole file, every host with it."""
    config = _config_under_an_unnameable_directory(tmp_path, shape, name)
    ssh_config = config.operator.ssh_config
    ssh_config.write_bytes(_written_by_0_19(ssh_config, "Host other\n    HostName 10.0.0.9\n"))
    argv = ["ssh", "-G", "-F", str(ssh_config), "other"]
    assert subprocess.run(argv, capture_output=True, timeout=10).returncode != 0

    _rebuild_config_dir(config, _db_with_one_vm())

    assert _resolved_hostname(ssh_config, "other") == "10.0.0.9"


def test_one_vms_store_problem_leaves_its_trust_lines_out_without_aborting_the_rebuild(
    tmp_path: Path,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.execution import trust

    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("broken", "100.64.0.1"), _mock_vm("good", "100.64.0.2")]
    db.list_agents.return_value = []
    key = fake_host_key()
    trust.write_records("good", [key])
    trust.begin_enrollment("broken")
    trust.remove("broken")
    trust.store_file("broken").mkdir()  # reading it raises

    _rebuild_config_dir(config, db)

    blocks = _managed_blocks((ssh_dir / "config.d" / _MANAGED_CONF).read_text())
    assert set(blocks) == {"awvm--broken", "awvm--good"}
    assert "HostKeyAlias" not in blocks["awvm--broken"]
    assert "HostKeyAlias agentworks-vm-good" in blocks["awvm--good"]
    assert len(captured_output.warnings) == 1


def test_a_store_path_ssh_would_expand_leaves_the_trust_lines_out_for_every_vm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.execution import trust

    config, ssh_dir = _mock_config(tmp_path)
    db = MagicMock()
    db.get_setting.return_value = None
    db.list_vms.return_value = [_mock_vm("one", "100.64.0.1"), _mock_vm("two", "100.64.0.2")]
    db.list_agents.return_value = []
    monkeypatch.setattr(trust, "STORE_DIR", tmp_path / "100%")
    for name in ("one", "two"):
        trust.write_records(name, [fake_host_key()])

    _rebuild_config_dir(config, db)

    blocks = _managed_blocks((ssh_dir / "config.d" / _MANAGED_CONF).read_text())
    assert set(blocks) == {"awvm--one", "awvm--two"}
    assert all("HostKeyAlias" not in body for body in blocks.values())
    assert len(captured_output.warnings) == 2
