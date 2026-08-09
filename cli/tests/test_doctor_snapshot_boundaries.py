"""Adversarial database snapshot and installed doctor entrypoint tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
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
        directory: Path,
        name: str,
        directory_fd: int | None,
        *,
        expected: tuple[int, int, int, int],
        destination: Path | None = None,
    ):  # noqa: ANN202
        nonlocal checkpointed
        fingerprint = original_fingerprint(
            directory,
            name,
            directory_fd,
            expected=expected,
            destination=destination,
        )
        if directory / name == db_path and destination is not None and checkpointed is None:
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
        directory: Path,
        name: str,
        directory_fd: int | None,
        *,
        expected: tuple[int, int, int, int],
        destination: Path | None = None,
    ):  # noqa: ANN202
        nonlocal committed_files, writer
        fingerprint = original_fingerprint(
            directory,
            name,
            directory_fd,
            expected=expected,
            destination=destination,
        )
        if directory / name == db_path and destination is not None and writer is None:
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


def _installed_agw() -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    entrypoint = Path(sys.executable).with_name(f"agw{suffix}")
    assert entrypoint.is_file(), "the test environment must install the agw console script"
    return entrypoint


def _run_installed_doctor(home: Path, *, timeout: float = 10) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("AGW_DEBUG", None)
    return subprocess.run(  # noqa: S603
        [_installed_agw(), "doctor", "--output", "json"],
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
    assert counts["fail"] > 0
    groups = cast("list[dict[str, object]]", data["groups"])
    assert groups and groups[-1]["name"] in {"Database", "Shell completions"}
    return document


def test_installed_entrypoint_propagates_failing_json_doctor_status(tmp_path: Path) -> None:
    """The installed console script emits one report and preserves exit 1."""
    _assert_complete_failing_doctor(_run_installed_doctor(tmp_path))


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
    ):
        monkeypatch.setattr(doctor, function_name, lambda *_args, _name=group_name, **_kwargs: group(_name))
    return config


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

    assert power.VMDescription is inspect.VMDescription
    assert power.VMEventName is inspect.VMEventName
    assert power.vm_description is inspect.vm_description
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
