"""Requested-parent filesystem preflight regressions for database snapshots."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from textwrap import dedent
from time import monotonic
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from agentworks.db import Database


def _database_files(db_path: Path) -> tuple[Path, Path, Path]:
    return db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")


def _installed_agw() -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    entrypoint = Path(sys.executable).with_name(f"agw{suffix}")
    assert entrypoint.is_file(), "the test environment must install the agw console script"
    return entrypoint


def _healthy_doctor_environment(home: Path) -> dict[str, str]:
    hook_directory = home / "python-hook"
    hook_directory.mkdir(exist_ok=True)
    (hook_directory / "sitecustomize.py").write_text(
        dedent(
            """\
            from types import SimpleNamespace

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
    environment = os.environ.copy()
    environment.update({"HOME": str(home), "PYTHONPATH": python_path})
    environment.pop("AGW_DEBUG", None)
    return environment


def _run_healthy_installed_doctor(home: Path, *, machine: bool) -> subprocess.CompletedProcess[bytes]:
    arguments = [_installed_agw(), "doctor"]
    if machine:
        arguments.extend(("--output", "json"))
    return subprocess.run(  # noqa: S603
        arguments,
        cwd=home,
        env=_healthy_doctor_environment(home),
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_snapshot_treats_database_under_missing_parent_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh state directories remain absent and are not inspection failures."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import LATEST_VERSION, Database

    missing_parent = tmp_path / "fresh-install" / ".config" / "agentworks"
    requested_path = missing_parent / "agentworks.db"
    original_probe = inspection_module._probe_resolved_directory_protocol
    probed_directories: list[Path] = []

    def record_probe(directory: Path) -> None:
        probed_directories.append(directory)
        original_probe(directory)

    monkeypatch.setattr(inspection_module, "_probe_resolved_directory_protocol", record_probe)

    with Database.inspection_snapshot(requested_path) as (exists, current, latest, snapshot):
        assert (exists, current, latest, snapshot) == (False, 0, LATEST_VERSION, None)

    assert probed_directories == [tmp_path.resolve()]
    assert not missing_parent.exists()


def test_installed_doctor_accepts_database_under_missing_parent(tmp_path: Path) -> None:
    """Fresh installs produce complete nonfailing human and JSON reports."""
    missing_parent = tmp_path / ".config" / "agentworks"

    machine = _run_healthy_installed_doctor(tmp_path, machine=True)
    human = _run_healthy_installed_doctor(tmp_path, machine=False)

    assert machine.returncode == 0
    assert machine.stderr == b""
    assert machine.stdout.count(b"\n") == 1
    document = cast("dict[str, object]", json.loads(machine.stdout))
    data = cast("dict[str, object]", document["data"])
    counts = cast("dict[str, int]", data["counts"])
    assert counts["unavailable"] == 0
    assert counts["fail"] == 0
    assert human.returncode == 0
    assert human.stderr == b""
    human_text = human.stdout.decode()
    assert "[unavailable]" not in human_text
    assert "[FAIL]" not in human_text
    assert "0 unavailable, 0 warn, 0 fail" in human_text
    assert not missing_parent.exists()


def _dangling_parent_database_path(home: Path, link_position: str) -> tuple[Path, Path]:
    missing_target = home / "operator-private-missing-parent"
    config_parent = home / ".config"
    if link_position == "parent":
        config_parent.mkdir()
        database_parent = config_parent / "agentworks"
        database_parent.symlink_to(missing_target, target_is_directory=True)
    else:
        config_parent.symlink_to(missing_target, target_is_directory=True)
        database_parent = config_parent / "agentworks"
    return database_parent / "agentworks.db", missing_target


@pytest.mark.parametrize("link_position", ["parent", "component"])
def test_snapshot_rejects_dangling_parent_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_position: str,
) -> None:
    """An existing unresolved parent component is invalid, not absent state."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database
    from agentworks.errors import StateError

    requested_path, missing_target = _dangling_parent_database_path(tmp_path, link_position)
    requested_entry_access: list[Path] = []
    source_entry_access: list[str] = []

    def unexpected_requested_entry(path: Path) -> bool:
        requested_entry_access.append(path)
        return False

    def unexpected_source_entry(name: str, directory_fd: int) -> int:
        source_entry_access.append(name)
        raise AssertionError(f"source entry opened through fd {directory_fd}")

    monkeypatch.setattr(inspection_module, "_requested_entry_exists", unexpected_requested_entry)
    monkeypatch.setattr(inspection_module, "_open_source", unexpected_source_entry)

    started = monotonic()
    with pytest.raises(StateError) as raised, Database.inspection_snapshot(requested_path):
        pytest.fail("a dangling parent component must not yield a snapshot")

    assert monotonic() - started < 1
    assert str(raised.value) == "state database inspection snapshot could not be created"
    assert str(requested_path) not in str(raised.value)
    assert str(missing_target) not in str(raised.value)
    assert "operator-private" not in str(raised.value)
    assert requested_entry_access == []
    assert source_entry_access == []


@pytest.mark.parametrize("link_position", ["parent", "component"])
def test_installed_doctor_rejects_dangling_parent_symlink(
    tmp_path: Path,
    link_position: str,
) -> None:
    """Installed human and JSON doctor fail safely for dangling parents."""
    requested_path, missing_target = _dangling_parent_database_path(tmp_path, link_position)

    machine = _run_healthy_installed_doctor(tmp_path, machine=True)
    human = _run_healthy_installed_doctor(tmp_path, machine=False)

    assert machine.returncode == 1
    assert machine.stderr == b""
    assert machine.stdout.count(b"\n") == 1
    document = cast("dict[str, object]", json.loads(machine.stdout))
    data = cast("dict[str, object]", document["data"])
    counts = cast("dict[str, int]", data["counts"])
    assert counts["unavailable"] == 0
    assert counts["fail"] == 1
    rendered = json.dumps(document)
    assert "database check failed" in rendered
    assert human.returncode == 1
    assert human.stderr == b""
    human_text = human.stdout.decode()
    assert "[unavailable]" not in human_text
    assert human_text.count("[FAIL]") == 1
    assert "0 unavailable" in human_text
    assert "1 fail" in human_text
    combined = rendered + human_text
    assert str(requested_path) not in combined
    assert str(missing_target) not in combined
    assert "operator-private" not in combined


@pytest.mark.parametrize("database_state", ["absent", "active"])
@pytest.mark.parametrize("error_number", [errno.ENOSYS, errno.EOPNOTSUPP])
def test_preflight_probes_resolved_requested_parent_before_source_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_state: str,
    error_number: int,
) -> None:
    """Target-filesystem unavailability closes every fd before source access."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database
    from agentworks.errors import DatabaseInspectionUnavailable

    real_parent = tmp_path / "real-parent"
    target_parent = real_parent / "target-filesystem"
    target_parent.mkdir(parents=True)
    alias_parent = tmp_path / "component-alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    requested_path = alias_parent / target_parent.name / "operator-private.db"
    real_path = target_parent / requested_path.name

    writer: Database | None = None
    if database_state == "active":
        writer = Database(real_path)
        writer.set_setting("system_slug", "active-target-filesystem")
        assert all(path.exists() for path in _database_files(real_path))
    else:
        assert not real_path.exists()

    original_open = os.open
    original_close = os.close
    original_resolve = Path.resolve
    target_directory_fd: int | None = None
    target_probes: list[str] = []
    open_directory_fds: set[int] = set()
    requested_entry_access: list[Path] = []
    requested_entry_resolutions: list[Path] = []
    source_entry_access: list[str] = []

    def unavailable_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal target_directory_fd
        if path == "." and kwargs.get("dir_fd") == target_directory_fd:
            target_probes.append(path)
            raise OSError(error_number, "operator-private-target-probe")
        descriptor = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            open_directory_fds.add(descriptor)
            if path == target_parent.name:
                target_directory_fd = descriptor
        return descriptor

    def record_close(descriptor: int) -> None:
        open_directory_fds.discard(descriptor)
        original_close(descriptor)

    def unexpected_requested_entry(path: Path) -> bool:
        requested_entry_access.append(path)
        return True

    def record_resolve(path: Path, strict: bool = False) -> Path:
        if path == requested_path:
            requested_entry_resolutions.append(path)
        return original_resolve(path, strict=strict)

    def unexpected_source_entry(name: str, directory_fd: int) -> int:
        source_entry_access.append(name)
        raise AssertionError(f"source entry opened through fd {directory_fd}")

    monkeypatch.setattr(inspection_module, "_requested_entry_exists", unexpected_requested_entry)
    monkeypatch.setattr(inspection_module, "_open_source", unexpected_source_entry)
    monkeypatch.setattr(Path, "resolve", record_resolve)
    monkeypatch.setattr(os, "open", unavailable_open)
    monkeypatch.setattr(os, "close", record_close)
    monkeypatch.setattr(os, "supports_dir_fd", frozenset({unavailable_open}))

    started = monotonic()
    try:
        with pytest.raises(DatabaseInspectionUnavailable) as raised, Database.inspection_snapshot(requested_path):
            pytest.fail("target-filesystem unavailability must not yield a snapshot")
    finally:
        if writer is not None:
            writer.close()

    assert monotonic() - started < 1
    assert str(raised.value) == "secure database inspection is unavailable on this host"
    assert "operator-private" not in str(raised.value)
    assert target_probes == ["."]
    assert requested_entry_access == []
    assert requested_entry_resolutions == []
    assert source_entry_access == []
    assert open_directory_fds == set()


@pytest.mark.parametrize("error_number", [errno.ENOSYS, errno.EOPNOTSUPP])
def test_final_symlink_target_is_preflighted_before_database_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    """A final link into an unsupported target filesystem yields no content."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database
    from agentworks.errors import DatabaseInspectionUnavailable

    requested_parent = tmp_path / "requested-filesystem"
    requested_parent.mkdir()
    target_parent = tmp_path / "target-filesystem"
    target_parent.mkdir()
    real_path = target_parent / "operator-private-target.db"
    requested_path = requested_parent / "agentworks.db"
    writer = Database(real_path)
    writer.set_setting("system_slug", "must-not-be-acquired")
    assert all(path.exists() for path in _database_files(real_path))
    requested_path.symlink_to(real_path)

    original_open = os.open
    original_close = os.close
    original_requested_entry_exists = inspection_module._requested_entry_exists
    target_directory_fd: int | None = None
    target_probes: list[str] = []
    open_directory_fds: set[int] = set()
    requested_entry_access: list[Path] = []
    source_entry_access: list[str] = []

    def unavailable_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal target_directory_fd
        if path == "." and kwargs.get("dir_fd") == target_directory_fd:
            target_probes.append(path)
            raise OSError(error_number, "operator-private-target-probe")
        descriptor = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            open_directory_fds.add(descriptor)
            if path == target_parent.name:
                target_directory_fd = descriptor
        return descriptor

    def record_close(descriptor: int) -> None:
        open_directory_fds.discard(descriptor)
        original_close(descriptor)

    def record_requested_entry(path: Path) -> bool:
        requested_entry_access.append(path)
        return original_requested_entry_exists(path)

    def unexpected_source_entry(name: str, directory_fd: int) -> int:
        source_entry_access.append(name)
        raise AssertionError(f"source entry opened through fd {directory_fd}")

    monkeypatch.setattr(inspection_module, "_requested_entry_exists", record_requested_entry)
    monkeypatch.setattr(inspection_module, "_open_source", unexpected_source_entry)
    monkeypatch.setattr(os, "open", unavailable_open)
    monkeypatch.setattr(os, "close", record_close)
    monkeypatch.setattr(os, "supports_dir_fd", frozenset({unavailable_open}))

    started = monotonic()
    try:
        with pytest.raises(DatabaseInspectionUnavailable) as raised, Database.inspection_snapshot(requested_path):
            pytest.fail("target-filesystem unavailability must not yield a snapshot")
    finally:
        writer.close()

    assert monotonic() - started < 1
    assert str(raised.value) == "secure database inspection is unavailable on this host"
    assert "operator-private" not in str(raised.value)
    assert target_probes == ["."]
    assert requested_entry_access == [requested_path]
    assert source_entry_access == []
    assert open_directory_fds == set()


def test_drive_relative_requested_path_is_rejected() -> None:
    """A drive-relative path cannot be rooted safely against the process cwd."""
    import agentworks.db.inspection as inspection_module

    requested_path = cast("Path", PureWindowsPath("C:operator-private.db"))

    with pytest.raises(inspection_module._UnsupportedSnapshotEntry):
        inspection_module._rooted_lexical_requested_path(requested_path)
