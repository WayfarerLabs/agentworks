"""Adversarial database snapshot and installed doctor entrypoint tests."""

from __future__ import annotations

import errno
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent
from time import monotonic
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from agentworks.db import Database


def _database_files(db_path: Path) -> tuple[Path, Path, Path]:
    return db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")


def _file_bytes(paths: tuple[Path, ...]) -> tuple[bytes | None, ...]:
    return tuple(path.read_bytes() if path.exists() else None for path in paths)


def test_inspection_snapshot_retries_concurrent_checkpoint_after_main_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint transition cannot produce a mixed or silently stale copy."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database

    db_path = tmp_path / "checkpoint.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "committed-before-checkpoint")
    paths = _database_files(db_path)
    original_fingerprint = inspection_module._fingerprint_stream
    checkpointed: tuple[bytes | None, ...] | None = None

    def checkpoint_after_main_copy(
        name: str,
        directory_fd: int,
        *,
        expected: tuple[int, int, int, int],
        destination: Path | None = None,
    ):  # noqa: ANN202
        nonlocal checkpointed
        fingerprint = original_fingerprint(
            name,
            directory_fd,
            expected=expected,
            destination=destination,
        )
        if name == db_path.name and destination is not None and checkpointed is None:
            writer.close()
            checkpointed = _file_bytes(paths)
        return fingerprint

    monkeypatch.setattr(inspection_module, "_fingerprint_stream", checkpoint_after_main_copy)

    with Database.inspection_snapshot(db_path) as (exists, current, latest, snapshot):
        assert exists and current == latest and snapshot is not None
        assert snapshot.get_setting("system_slug") == "committed-before-checkpoint"

    assert checkpointed is not None
    assert checkpointed[1:] == (None, None)
    assert _file_bytes(paths) == checkpointed


def test_inspection_snapshot_retries_clean_to_active_transition_after_main_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A main-only observation cannot omit a WAL commit created during copy."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database

    db_path = tmp_path / "clean-to-active.db"
    clean = Database(db_path)
    clean.set_setting("system_slug", "clean-before-transition")
    clean.close()
    paths = _database_files(db_path)
    assert _file_bytes(paths)[1:] == (None, None)

    original_copy = inspection_module._copy_verified_file_set
    original_fingerprint = inspection_module._fingerprint_stream
    attempts = 0
    writer: Database | None = None
    committed_files: tuple[bytes | None, ...] | None = None

    def counted_copy(source_db: Path, snapshot_db: Path) -> None:
        nonlocal attempts
        attempts += 1
        original_copy(source_db, snapshot_db)

    def activate_after_main_copy(
        name: str,
        directory_fd: int,
        *,
        expected: tuple[int, int, int, int],
        destination: Path | None = None,
    ):  # noqa: ANN202
        nonlocal committed_files, writer
        fingerprint = original_fingerprint(
            name,
            directory_fd,
            expected=expected,
            destination=destination,
        )
        if name == db_path.name and destination is not None and writer is None:
            writer = Database(db_path)
            writer.set_setting("system_slug", "committed-after-clean-observation")
            committed_files = _file_bytes(paths)
        return fingerprint

    monkeypatch.setattr(inspection_module, "_copy_verified_file_set", counted_copy)
    monkeypatch.setattr(inspection_module, "_fingerprint_stream", activate_after_main_copy)
    try:
        with Database.inspection_snapshot(db_path) as (exists, current, latest, snapshot):
            assert exists and current == latest and snapshot is not None
            assert snapshot.get_setting("system_slug") == "committed-after-clean-observation"
        assert committed_files is not None
        assert committed_files[1] is not None and committed_files[2] is not None
        assert _file_bytes(paths) == committed_files
    finally:
        if writer is not None:
            writer.close()

    assert attempts == 2


def test_inspection_snapshot_resolves_symlink_before_active_sidecars(
    tmp_path: Path,
) -> None:
    """A database alias discovers WAL state beside the real database identity."""
    from agentworks.db import Database

    real_directory = tmp_path / "real"
    alias_directory = tmp_path / "alias"
    real_directory.mkdir()
    alias_directory.mkdir()
    db_path = real_directory / "state.db"
    alias_path = alias_directory / "agentworks.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "resolved-active-wal")
    alias_path.symlink_to(db_path)
    paths = _database_files(db_path)
    before = _file_bytes(paths)
    try:
        with Database.inspection_snapshot(alias_path) as (exists, current, latest, snapshot):
            assert exists and current == latest and snapshot is not None
            assert snapshot.get_setting("system_slug") == "resolved-active-wal"
        after = _file_bytes(paths)
    finally:
        writer.close()

    assert after == before
    assert not Path(f"{alias_path}-wal").exists()
    assert not Path(f"{alias_path}-shm").exists()


def test_inspection_snapshot_resolves_stable_component_symlink(
    tmp_path: Path,
) -> None:
    """A stable symlinked ancestor resolves before the no-follow descriptor walk."""
    from agentworks.db import Database

    real_parent = tmp_path / "real-parent"
    real_directory = real_parent / "nested"
    real_directory.mkdir(parents=True)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    db_path = real_directory / "state.db"
    alias_path = alias_parent / "nested" / "state.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "stable-component-target")
    paths = _database_files(db_path)
    before = _file_bytes(paths)
    assert all(content is not None for content in before)
    try:
        with Database.inspection_snapshot(alias_path) as (exists, current, latest, snapshot):
            assert exists and current == latest and snapshot is not None
            assert snapshot.get_setting("system_slug") == "stable-component-target"
        after = _file_bytes(paths)
    finally:
        writer.close()

    assert after == before


def test_inspection_snapshot_rejects_intermediate_ancestor_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancestor replaced after resolve cannot redirect snapshot acquisition."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database
    from agentworks.errors import StateError

    container = tmp_path / "container"
    original_ancestor = container / "mutable-ancestor"
    original_directory = original_ancestor / "nested"
    original_directory.mkdir(parents=True)
    original_db = original_directory / "operator-private-original.db"
    original_writer = Database(original_db)
    original_writer.set_setting("system_slug", "original-target")

    replacement_ancestor = container / "replacement-ancestor"
    replacement_directory = replacement_ancestor / "nested"
    replacement_directory.mkdir(parents=True)
    replacement_db = replacement_directory / original_db.name
    replacement_writer = Database(replacement_db)
    replacement_writer.set_setting("system_slug", "replacement-must-not-be-read")
    assert all(path.exists() for path in _database_files(original_db))
    assert all(path.exists() for path in _database_files(replacement_db))

    moved_ancestor = container / "pinned-original"
    original_copy = inspection_module._copy_verified_file_set
    swapped = False

    def swap_before_directory_walk(source_db: Path, snapshot_db: Path) -> None:
        nonlocal swapped
        if not swapped:
            original_ancestor.rename(moved_ancestor)
            original_ancestor.symlink_to(replacement_ancestor, target_is_directory=True)
            swapped = True
        original_copy(source_db, snapshot_db)

    monkeypatch.setattr(inspection_module, "_copy_verified_file_set", swap_before_directory_walk)
    try:
        with pytest.raises(StateError) as raised, Database.inspection_snapshot(original_db):
            pytest.fail("an ancestor replacement must not be yielded")
    finally:
        original_writer.close()
        replacement_writer.close()

    assert swapped
    assert str(raised.value) == "state database inspection snapshot could not be created"
    assert "operator-private" not in str(raised.value)
    assert "replacement-must-not-be-read" not in str(raised.value)


def test_inspection_snapshot_retry_exhaustion_is_clean_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuously changing source fails closed without residue or paths."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database
    from agentworks.errors import StateError

    db_path = tmp_path / "operator-private-name.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "busy")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_temporary_directory = tempfile.TemporaryDirectory
    created: list[Path] = []
    attempts = 0

    def tracked_temporary_directory(*, prefix: str):  # noqa: ANN202
        temporary = real_temporary_directory(prefix=prefix, dir=scratch)
        created.append(Path(temporary.name))
        return temporary

    def always_changes(source_db: Path, snapshot_db: Path) -> None:
        nonlocal attempts
        del source_db, snapshot_db
        attempts += 1
        raise inspection_module._SnapshotChanged

    monkeypatch.setattr("agentworks.db.inspection.tempfile.TemporaryDirectory", tracked_temporary_directory)
    monkeypatch.setattr(inspection_module, "_copy_verified_file_set", always_changes)
    try:
        with pytest.raises(StateError) as raised, Database.inspection_snapshot(db_path):
            pytest.fail("an unstable snapshot must not be yielded")
    finally:
        writer.close()

    message = str(raised.value)
    assert message == "state database inspection snapshot could not be created"
    assert str(db_path) not in message and str(scratch) not in message
    assert attempts == inspection_module._INSPECTION_SNAPSHOT_ATTEMPTS
    assert created and not any(path.exists() for path in created)


def _assert_snapshot_rejected(path: Path) -> None:
    from agentworks.db import Database
    from agentworks.errors import StateError

    with pytest.raises(StateError) as raised, Database.inspection_snapshot(path):
        pytest.fail("an unsupported database entry must not be yielded")
    assert str(raised.value) == "state database inspection snapshot could not be created"
    assert str(path) not in str(raised.value)


def test_inspection_snapshot_distinguishes_missing_from_broken_symlinks(tmp_path: Path) -> None:
    from agentworks.db import LATEST_VERSION, Database

    missing = tmp_path / "missing.db"
    with Database.inspection_snapshot(missing) as snapshot:
        assert snapshot[:3] == (False, 0, LATEST_VERSION)
        assert snapshot[3] is None

    dangling = tmp_path / "operator-private-dangling.db"
    dangling.symlink_to("absent-target.db")
    _assert_snapshot_rejected(dangling)

    loop = tmp_path / "operator-private-loop.db"
    loop.symlink_to(loop.name)
    _assert_snapshot_rejected(loop)


def test_inspection_snapshot_rejects_directory_device_and_socket(tmp_path: Path) -> None:
    directory = tmp_path / "operator-private-directory.db"
    directory.mkdir()
    _assert_snapshot_rejected(directory)

    device = Path("/dev/null")
    if device.exists():
        _assert_snapshot_rejected(device)

    if not hasattr(socket, "AF_UNIX"):
        return
    socket_path = tmp_path / "operator-private-socket.db"
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(socket_path))
        _assert_snapshot_rejected(socket_path)
    finally:
        listener.close()


@pytest.mark.parametrize("database_state", ["absent", "active"])
@pytest.mark.parametrize(
    ("primitive", "value"),
    [
        ("O_NOFOLLOW", None),
        ("O_NONBLOCK", 0),
        ("O_DIRECTORY", 0),
        ("openat", None),
    ],
)
def test_inspection_snapshot_fails_before_source_access_without_required_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_state: str,
    primitive: str,
    value: int | None,
) -> None:
    """An incomplete host protocol cannot touch the main, WAL, or SHM entries."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database
    from agentworks.errors import DatabaseInspectionUnavailable

    db_path = tmp_path / "operator-private-capability.db"
    writer: Database | None = None
    if database_state == "active":
        writer = Database(db_path)
        writer.set_setting("system_slug", "active-sidecars")
        assert all(path.exists() for path in _database_files(db_path))
    else:
        assert not db_path.exists()

    requested_entries: list[Path] = []

    def record_requested_entry(path: Path) -> bool:
        requested_entries.append(path)
        return True

    monkeypatch.setattr(inspection_module, "_requested_entry_exists", record_requested_entry)
    if primitive == "openat":
        monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    elif value is None:
        monkeypatch.delattr(os, primitive, raising=False)
    else:
        monkeypatch.setattr(os, primitive, value)

    started = monotonic()
    try:
        with pytest.raises(DatabaseInspectionUnavailable) as raised, Database.inspection_snapshot(db_path):
            pytest.fail("an incomplete snapshot protocol must not be yielded")
    finally:
        if writer is not None:
            writer.close()

    assert monotonic() - started < 1
    assert str(raised.value) == "secure database inspection is unavailable on this host"
    assert str(db_path) not in str(raised.value)
    assert requested_entries == []


@pytest.mark.parametrize("error_kind", ["not_implemented", "not_supported", "not_implemented_syscall"])
def test_inspection_snapshot_maps_runtime_primitive_unavailability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
) -> None:
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database
    from agentworks.errors import DatabaseInspectionUnavailable

    db_path = tmp_path / "runtime-unavailable.db"
    assert not db_path.exists()
    original_open = os.open
    source_access: list[Path] = []
    relative_probes: list[str] = []

    def unavailable_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if path == "." and kwargs.get("dir_fd") is not None:
            relative_probes.append(path)
            if error_kind == "not_implemented":
                raise NotImplementedError
            error_number = errno.ENOSYS if error_kind == "not_implemented_syscall" else errno.EOPNOTSUPP
            raise OSError(error_number, "operator-private-runtime-detail")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        inspection_module,
        "_requested_entry_exists",
        lambda path: source_access.append(path) or False,
    )
    monkeypatch.setattr(os, "open", unavailable_open)
    monkeypatch.setattr(os, "supports_dir_fd", frozenset({unavailable_open}))
    with pytest.raises(DatabaseInspectionUnavailable) as raised, Database.inspection_snapshot(db_path):
        pytest.fail("runtime primitive unavailability must not yield a snapshot")

    assert str(raised.value) == "secure database inspection is unavailable on this host"
    assert "operator-private" not in str(raised.value)
    assert relative_probes == ["."]
    assert source_access == []


def test_inspection_snapshot_keeps_ordinary_open_errors_as_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import Database
    from agentworks.errors import StateError

    db_path = tmp_path / "ordinary-error.db"
    database = Database(db_path)
    database.close()
    original_open = os.open

    def denied_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if flags & os.O_DIRECTORY:
            raise OSError(errno.EACCES, "operator-private-permission-detail")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied_open)
    monkeypatch.setattr(os, "supports_dir_fd", frozenset({denied_open}))
    with pytest.raises(StateError) as raised, Database.inspection_snapshot(db_path):
        pytest.fail("an ordinary acquisition error must not yield a snapshot")

    assert str(raised.value) == "state database inspection snapshot could not be created"
    assert "operator-private" not in str(raised.value)


def test_inspection_snapshot_uses_complete_protocol_for_main_wal_and_shm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every source-set entry uses nonblocking no-follow relative acquisition."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database

    db_path = tmp_path / "complete-protocol.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "active-sidecars")
    source_paths = _database_files(db_path)
    assert all(path.exists() for path in source_paths)
    original_open_source = inspection_module._open_source
    original_open_directory_component = inspection_module._open_directory_component
    acquisitions: list[tuple[str, int, int]] = []
    directory_acquisitions: list[tuple[str, int, int]] = []

    def record_open_source(name: str, directory_fd: int) -> int:
        flags = inspection_module._source_flags()
        acquisitions.append((name, directory_fd, flags))
        return original_open_source(name, directory_fd)

    def record_open_directory_component(name: str, parent_fd: int) -> int:
        flags = inspection_module._directory_flags()
        directory_acquisitions.append((name, parent_fd, flags))
        return original_open_directory_component(name, parent_fd)

    monkeypatch.setattr(inspection_module, "_open_source", record_open_source)
    monkeypatch.setattr(inspection_module, "_open_directory_component", record_open_directory_component)
    try:
        with Database.inspection_snapshot(db_path) as (exists, current, latest, snapshot):
            assert exists and current == latest and snapshot is not None
            assert snapshot.get_setting("system_slug") == "active-sidecars"
    finally:
        writer.close()

    required_names = {path.name for path in source_paths}
    assert required_names <= {name for name, _directory_fd, _flags in acquisitions}
    assert all(directory_fd >= 0 for _name, directory_fd, _flags in acquisitions)
    assert all(flags & os.O_NOFOLLOW for _name, _directory_fd, flags in acquisitions)
    assert all(flags & os.O_NONBLOCK for _name, _directory_fd, flags in acquisitions)
    assert directory_acquisitions
    preflight_names = [*db_path.parent.resolve().parts[1:], "."]
    assert [name for name, _parent_fd, _flags in directory_acquisitions[: len(preflight_names)]] == (preflight_names)
    assert all(parent_fd >= 0 for _name, parent_fd, _flags in directory_acquisitions)
    assert all(flags & os.O_DIRECTORY for _name, _parent_fd, flags in directory_acquisitions)
    assert all(flags & os.O_NOFOLLOW for _name, _parent_fd, flags in directory_acquisitions)


def _installed_agw() -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    entrypoint = Path(sys.executable).with_name(f"agw{suffix}")
    assert entrypoint.is_file(), "the test environment must install the agw console script"
    return entrypoint


def _run_installed_doctor(
    home: Path,
    *,
    output_format: str = "json",
    extra_environment: dict[str, str] | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("AGW_DEBUG", None)
    if extra_environment is not None:
        environment.update(extra_environment)
    arguments = [_installed_agw(), "doctor"]
    if output_format != "human":
        arguments.extend(("--output", output_format))
    return subprocess.run(  # noqa: S603
        arguments,
        cwd=home,
        env=environment,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _assert_complete_failing_doctor(process: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
    assert process.returncode == 1
    assert process.stderr == b""
    assert process.stdout.endswith(b"\n")
    assert process.stdout.count(b"\n") == 1
    document = cast("dict[str, object]", json.loads(process.stdout))
    assert list(document) == ["schema_version", "command", "data"]
    assert document["schema_version"] == 1
    assert document["command"] == "doctor"
    data = cast("dict[str, object]", document["data"])
    counts = cast("dict[str, int]", data["counts"])
    assert list(counts) == ["ok", "info", "unavailable", "warn", "fail"]
    assert counts["fail"] > 0
    groups = cast("list[dict[str, object]]", data["groups"])
    assert groups and groups[-1]["name"] in {"Database", "Shell completions"}
    return document


def test_installed_entrypoint_propagates_failing_json_doctor_status(tmp_path: Path) -> None:
    """The installed console script emits one report and preserves exit 1."""
    _assert_complete_failing_doctor(_run_installed_doctor(tmp_path))


@pytest.mark.parametrize("value", ["operator-private-version", b"7", 7.5, -1])
def test_inspection_snapshot_rejects_malformed_schema_versions(tmp_path: Path, value: object) -> None:
    import agentworks.db.inspection as inspection_module
    from agentworks.db import LATEST_VERSION, Database
    from agentworks.errors import StateError

    assert inspection_module._validated_schema_version(None) == 0
    assert inspection_module._validated_schema_version(0) == 0
    assert inspection_module._validated_schema_version(LATEST_VERSION) == LATEST_VERSION
    with pytest.raises(StateError, match="^state database inspection snapshot could not be created$"):
        inspection_module._validated_schema_version(True)

    db_path = tmp_path / "malformed-version.db"
    database = Database(db_path)
    database.close()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StateError) as raised, Database.inspection_snapshot(db_path):
        pytest.fail("a malformed schema version must not be yielded")
    message = str(raised.value)
    assert message == "state database inspection snapshot could not be created"
    assert "operator-private" not in message


def test_installed_json_doctor_rejects_text_schema_version_safely(tmp_path: Path) -> None:
    from agentworks.db import Database

    marker = "operator-private-malformed-schema-value"
    config_dir = tmp_path / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    db_path = config_dir / "agentworks.db"
    database = Database(db_path)
    database.close()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (marker,))
        connection.commit()
    finally:
        connection.close()

    process = _run_installed_doctor(tmp_path)
    document = _assert_complete_failing_doctor(process)
    rendered = json.dumps(document)
    assert '"name": "Database", "status": "fail"' in rendered
    assert "database check failed" in rendered
    assert marker not in rendered
    assert str(db_path) not in rendered
    assert "TypeError" not in rendered


@pytest.mark.parametrize("link_kind", ["dangling", "loop"])
def test_installed_json_doctor_fails_closed_for_invalid_symlinks(tmp_path: Path, link_kind: str) -> None:
    config_dir = tmp_path / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    db_path = config_dir / "agentworks.db"
    if link_kind == "dangling":
        db_path.symlink_to("operator-private-missing.db")
    else:
        db_path.symlink_to(db_path.name)

    process = _run_installed_doctor(tmp_path)
    document = _assert_complete_failing_doctor(process)
    rendered = json.dumps(document)
    assert "database check failed" in rendered
    assert "operator-private" not in rendered
    assert str(db_path) not in rendered


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_installed_json_doctor_rejects_fifo_without_blocking_or_path_leak(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    db_path = config_dir / "agentworks.db"
    os.mkfifo(db_path)

    started = monotonic()
    process = _run_installed_doctor(tmp_path, timeout=5)
    elapsed = monotonic() - started
    document = _assert_complete_failing_doctor(process)
    assert elapsed < 5
    rendered = json.dumps(document)
    assert "database check failed" in rendered
    assert str(db_path) not in rendered


def _stub_doctor_environment(monkeypatch: pytest.MonkeyPatch) -> object:
    from agentworks import doctor
    from agentworks.resources import Registry

    config = SimpleNamespace(defaults=SimpleNamespace(site=None))

    def group(name: str) -> doctor.HealthGroup:
        result = doctor.HealthGroup(name)
        result.ok("Fixture")
        return result

    config_group = group("Configuration")
    registry = Registry.empty()
    monkeypatch.setattr(doctor, "_check_config", lambda **_kwargs: (config_group, config, registry))
    for function_name, group_name in (
        ("_check_python", "Python"),
        ("_check_required_tools", "Required tools"),
        ("_check_tailscale", "Tailscale"),
        ("_check_plugins", "System plugins"),
        ("_check_vm_platforms", "VM platforms"),
        ("_check_secret_backends", "Secret backends"),
        ("_check_secrets", "Secrets"),
        ("_check_completions", "Shell completions"),
    ):
        monkeypatch.setattr(doctor, function_name, lambda *_args, _name=group_name, **_kwargs: group(_name))
    return config


@pytest.mark.parametrize("database_state", ["absent", "active"])
@pytest.mark.parametrize("primitive", ["missing_no_follow", "zero_nonblocking"])
def test_doctor_renders_protocol_unavailable_as_complete_nonfailing_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_state: str,
    primitive: str,
) -> None:
    from typer.testing import CliRunner

    import agentworks.db as db_module
    import agentworks.db.inspection as inspection_module
    from agentworks.cli import app
    from agentworks.db import Database

    marker = "operator-private-unavailable-source"
    db_path = tmp_path / "operator-private-unavailable.db"
    writer: Database | None = None
    if database_state == "active":
        writer = Database(db_path)
        writer.set_setting("system_slug", marker)
        assert all(path.exists() for path in _database_files(db_path))
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    _stub_doctor_environment(monkeypatch)
    source_access: list[Path] = []

    def record_source_access(path: Path) -> bool:
        source_access.append(path)
        return True

    monkeypatch.setattr(inspection_module, "_requested_entry_exists", record_source_access)
    if primitive == "missing_no_follow":
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    else:
        monkeypatch.setattr(os, "O_NONBLOCK", 0)

    try:
        human = CliRunner().invoke(app, ["doctor"])
        machine = CliRunner().invoke(app, ["doctor", "--output", "json"])
    finally:
        if writer is not None:
            writer.close()

    expected_message = "secure database inspection is unavailable on this host"
    assert human.exit_code == 0, human.output
    assert human.stderr == ""
    assert human.stdout.count("[unavailable]") == 3
    assert human.stdout.count(expected_message) == 3
    assert "Results: 9 ok, 0 info, 3 unavailable, 0 warn, 0 fail\n" in human.stdout
    assert machine.exit_code == 0, machine.output
    assert machine.stderr == ""
    assert machine.stdout.count("\n") == 1
    document = cast("dict[str, object]", json.loads(machine.stdout))
    data = cast("dict[str, object]", document["data"])
    assert data["counts"] == {"ok": 9, "info": 0, "unavailable": 3, "warn": 0, "fail": 0}
    groups = cast("list[dict[str, object]]", data["groups"])
    unavailable = [
        check
        for group in groups
        for check in cast("list[dict[str, object]]", group["checks"])
        if check["status"] == "unavailable"
    ]
    assert len(unavailable) == 3
    assert {check["name"] for check in unavailable} == {"System slug", "VM sites", "Database"}
    assert all(check["message"] == expected_message and check["hint"] is None for check in unavailable)
    combined = human.stdout + machine.stdout
    assert marker not in combined
    assert str(db_path) not in combined
    assert "\x00" not in combined
    assert source_access == []


def test_installed_doctor_reports_secure_inspection_unavailable_without_failure(tmp_path: Path) -> None:
    from agentworks.db import Database

    marker = "operator-private-installed-unavailable"
    config_dir = tmp_path / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    db_path = config_dir / "agentworks.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", marker)
    assert all(path.exists() for path in _database_files(db_path))

    hook_directory = tmp_path / "python-hook"
    hook_directory.mkdir()
    (hook_directory / "sitecustomize.py").write_text(
        dedent(
            """\
            import os
            from types import SimpleNamespace

            os.O_NOFOLLOW = 0

            from agentworks import doctor
            from agentworks.resources import Registry

            def group(name):
                result = doctor.HealthGroup(name)
                result.ok("Fixture")
                return result

            config_group = group("Configuration")
            config = SimpleNamespace(defaults=SimpleNamespace(site=None))
            registry = Registry.empty()
            doctor._check_config = lambda **_kwargs: (config_group, config, registry)
            for function_name, group_name in (
                ("_check_python", "Python"),
                ("_check_required_tools", "Required tools"),
                ("_check_tailscale", "Tailscale"),
                ("_check_plugins", "System plugins"),
                ("_check_vm_platforms", "VM platforms"),
                ("_check_secret_backends", "Secret backends"),
                ("_check_secrets", "Secrets"),
                ("_check_completions", "Shell completions"),
            ):
                setattr(doctor, function_name, lambda *_args, _name=group_name, **_kwargs: group(_name))
            """
        )
    )
    python_path = str(hook_directory)
    if inherited_path := os.environ.get("PYTHONPATH"):
        python_path += os.pathsep + inherited_path
    environment = {"PYTHONPATH": python_path}

    try:
        machine = _run_installed_doctor(tmp_path, extra_environment=environment)
        human = _run_installed_doctor(tmp_path, output_format="human", extra_environment=environment)
    finally:
        writer.close()

    expected_message = "secure database inspection is unavailable on this host"
    assert machine.returncode == 0
    assert machine.stderr == b""
    assert machine.stdout.count(b"\n") == 1
    document = cast("dict[str, object]", json.loads(machine.stdout))
    data = cast("dict[str, object]", document["data"])
    assert data["counts"] == {"ok": 9, "info": 0, "unavailable": 3, "warn": 0, "fail": 0}
    rendered = json.dumps(document)
    assert rendered.count('"status": "unavailable"') == 3
    assert rendered.count(expected_message) == 3
    assert human.returncode == 0
    assert human.stderr == b""
    human_text = human.stdout.decode()
    assert human_text.count("[unavailable]") == 3
    assert human_text.count(expected_message) == 3
    assert "Results: 9 ok, 0 info, 3 unavailable, 0 warn, 0 fail\n" in human_text
    combined = rendered + human_text
    assert marker not in combined
    assert str(db_path) not in combined
    assert "\x00" not in combined


def test_doctor_projects_one_snapshot_failure_consistently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db as db_module
    from agentworks import doctor

    invalid = tmp_path / "operator-private-dangling.db"
    invalid.symlink_to("missing.db")
    monkeypatch.setattr(db_module, "DB_PATH", invalid)
    _stub_doctor_environment(monkeypatch)

    report = doctor.run_checks()
    groups = {group.name: group for group in report.groups}
    assert groups["System"].checks[-1].status is doctor.Status.WARN
    assert groups["VM sites"].checks[-1].status is doctor.Status.WARN
    assert groups["Database"].checks[-1].status is doctor.Status.FAIL
    for group_name in ("System", "VM sites", "Database"):
        check = groups[group_name].checks[-1]
        assert check.machine_diagnostic is doctor.MachineDiagnostic.DATABASE_UNAVAILABLE
        assert str(invalid) not in (check.message or "")


def test_doctor_collects_large_database_once_per_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db as db_module
    from agentworks import doctor
    from agentworks.db import Database

    db_path = tmp_path / "scale.db"
    database = Database(db_path)
    database.set_setting("system_slug", "one-generation")
    for index in range(128):
        name = f"vm-{index:03d}"
        database.insert_vm(name, site="missing-site", hostname=name)
    database.close()

    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    _stub_doctor_environment(monkeypatch)
    calls = {"snapshot": 0, "setting": 0, "vms": 0, "workspaces": 0}
    raw_snapshot = Database.inspection_snapshot
    raw_get_setting = Database.get_setting
    raw_list_vms = Database.list_vms
    raw_list_workspaces = Database.list_workspaces

    def counted_snapshot(cls: type[Database], path: Path | None = None):  # noqa: ANN202
        del cls
        calls["snapshot"] += 1
        return raw_snapshot(path)

    def counted_get_setting(self: Database, key: str) -> str | None:
        calls["setting"] += 1
        return raw_get_setting(self, key)

    def counted_list_vms(self: Database):  # noqa: ANN202
        calls["vms"] += 1
        return raw_list_vms(self)

    def counted_list_workspaces(self: Database, *, vm_name=None):  # noqa: ANN001, ANN202
        calls["workspaces"] += 1
        return raw_list_workspaces(self, vm_name=vm_name)

    monkeypatch.setattr(Database, "inspection_snapshot", classmethod(counted_snapshot))
    monkeypatch.setattr(Database, "get_setting", counted_get_setting)
    monkeypatch.setattr(Database, "list_vms", counted_list_vms)
    monkeypatch.setattr(Database, "list_workspaces", counted_list_workspaces)

    report = doctor.run_checks()

    assert calls == {"snapshot": 1, "setting": 1, "vms": 1, "workspaces": 1}
    assert [group.name for group in report.groups] == [
        "System",
        "Python",
        "Required tools",
        "Tailscale",
        "System plugins",
        "VM platforms",
        "VM sites",
        "Configuration",
        "Secret backends",
        "Secrets",
        "Database",
    ]
    system = report.groups[0]
    sites = report.groups[6]
    database_group = report.groups[-1]
    assert system.checks[0].message == "one-generation"
    assert sum(check.name.startswith("VM '") for check in sites.checks) == 128
    assert database_group.checks[1].message == "128 VMs, 0 workspaces"


def test_inspection_module_boundaries_and_compatibility_exports() -> None:
    from agentworks.db import Database
    from agentworks.vms.manager import inspect, power
    from agentworks.vms.manager.power import VMDiagnostic

    moved_symbols = (
        "VMListRow",
        "VMListing",
        "VMIssueCode",
        "VMIssue",
        "VMDiagnostic",
        "VMDetailAgent",
        "VMDetailSession",
        "VMDetailWorkspace",
        "VMDetailEvent",
        "VMEventName",
        "VMDetailFacts",
        "VMLiveResources",
        "VMDescription",
        "_project_vm_event_name",
        "vm_listing_data",
        "vm_description_data",
        "_project_vm_issue",
        "vm_listing",
        "render_vm_listing",
        "list_vms",
        "vm_description",
        "render_vm_description",
        "describe_vm",
    )
    for symbol in moved_symbols:
        assert getattr(power, symbol) is getattr(inspect, symbol)
    assert VMDiagnostic is inspect.VMDiagnostic
    assert power._NAME_CELL_WIDTH == inspect._NAME_CELL_WIDTH
    assert callable(Database.inspection_snapshot)

    package = Path(inspect.__file__).parents[2]
    boundaries = (
        package / "doctor.py",
        package / "db" / "database.py",
        package / "vms" / "manager" / "power.py",
    )
    assert all(len(path.read_text().splitlines()) < 1000 for path in boundaries)
    power_source = (package / "vms" / "manager" / "power.py").read_text()
    assert "class VMDescription" not in power_source
    assert "def vm_description(" not in power_source
