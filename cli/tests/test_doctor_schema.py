"""Bounded schema checks for the ordinary doctor database path."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from agentworks.db import LATEST_VERSION, Database, SessionMode
from agentworks.doctor import HealthGroup, Status
from agentworks.doctor_state import _report_contents, append_vm_site_database_checks, check_database, check_system
from agentworks.errors import StateError


def _installed_agw() -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    return Path(sys.executable).with_name(f"agw{suffix}")


def _run_installed_doctor(home: Path, *, output: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("AGW_DEBUG", None)
    return subprocess.run(
        [str(_installed_agw()), "doctor", "--output", output],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
        timeout=30,
    )


def test_schema_check_distinguishes_absent_table_from_malformed_shape(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    connection = sqlite3.connect(database_path)
    connection.close()
    assert Database.check_schema(database_path) == (True, 0, LATEST_VERSION)

    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE schema_version (private_marker TEXT NOT NULL)")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="schema is unavailable or malformed") as raised:
        Database.check_schema(database_path)
    assert "private_marker" not in str(raised.value)
    assert str(database_path) not in str(raised.value)


def test_schema_check_rejects_a_non_table_schema_entry(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE VIEW schema_version AS SELECT 1 AS version")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="schema is unavailable or malformed"):
        Database.check_schema(database_path)


@pytest.mark.parametrize("version", [-1, 1.5, "private-version-marker"])
def test_schema_check_rejects_invalid_scalar_versions(tmp_path: Path, version: object) -> None:
    database_path = tmp_path / "state.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE schema_version (version)")
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="schema version is invalid") as raised:
        Database.check_schema(database_path)
    assert str(version) not in str(raised.value)
    assert str(database_path) not in str(raised.value)

    with pytest.raises(StateError, match="schema version is invalid"):
        Database(database_path, read_only=True)


@pytest.mark.parametrize(
    ("schema", "expected_status", "expected_text"),
    [
        ((False, 0, LATEST_VERSION), Status.OK, "does not exist yet"),
        ((True, LATEST_VERSION - 1, LATEST_VERSION), Status.WARN, "normal Agentworks command"),
        ((True, LATEST_VERSION + 1, LATEST_VERSION), Status.FAIL, "downgrade?"),
    ],
)
def test_doctor_distinguishes_absent_stale_and_newer_schema(
    monkeypatch: pytest.MonkeyPatch,
    schema: tuple[bool, int, int],
    expected_status: Status,
    expected_text: str,
) -> None:
    monkeypatch.setattr(Database, "check_schema", staticmethod(lambda path=None: schema))

    check = check_database().checks[0]

    assert check.status is expected_status
    assert expected_text in (check.message or "")


def test_doctor_warns_without_echoing_an_unexpected_vm_initialization_state(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = Path(db._conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr("agentworks.db.DB_PATH", database_path)
    db.insert_vm("box", site="lima-local", hostname="box")
    db._conn.execute("UPDATE vms SET init_status = ? WHERE name = ?", ("PRIVATE_FUTURE_STATE", "box"))
    db._conn.commit()

    checks = check_database().checks

    warning = next(check for check in checks if check.name == "VM 'box'")
    assert warning.status is Status.WARN
    assert warning.message == "unexpected initialization state"


def test_doctor_reports_each_database_backed_live_resource_count(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="lima-local", hostname="box")
    for index in range(2):
        db.insert_workspace(f"work-{index}", f"/work/{index}", "box", f"work-{index}")
    for index in range(3):
        db.insert_agent(f"agent-{index}", "box", f"agent-{index}")
    for index in range(4):
        db.insert_session(f"session-{index}", "work-0", "default", SessionMode.ADMIN)
    db._conn.execute(
        "UPDATE sessions SET harness_integration_state = ? WHERE name = ?",
        ("not-json", "session-0"),
    )
    db._conn.commit()
    for index in range(5):
        db.insert_console(f"console-{index}", "box")

    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    group = HealthGroup("Database")
    _report_contents(group, db)

    assert warnings == []
    message = group.checks[0].message
    assert message is not None
    assert [int(value) for value in re.findall(r"\d+", message)] == [1, 2, 3, 4, 5]


def test_doctor_database_errors_remain_shared_actionable_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "database permission denied"
    monkeypatch.setattr(
        Database,
        "check_schema",
        staticmethod(lambda path=None: (_ for _ in ()).throw(PermissionError(marker))),
    )

    system = check_system().checks[0]
    database = check_database().checks[0]
    sites = HealthGroup("VM sites")
    append_vm_site_database_checks(sites, sites={}, not_ready={})

    assert system.message == f"could not check the database: {marker}"
    assert database.message == marker
    assert sites.checks[0].message == f"could not check the database: {marker}"


def test_installed_doctor_reports_malformed_schema_in_human_and_json(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "agentworks"
    config_dir.mkdir(parents=True)
    database_path = config_dir / "agentworks.db"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE schema_version (private_marker TEXT NOT NULL)")
    connection.commit()
    connection.close()

    human = _run_installed_doctor(tmp_path, output="human")
    machine = _run_installed_doctor(tmp_path, output="json")

    assert human.returncode == machine.returncode == 1
    assert "Database:" in human.stdout
    assert "state database schema is unavailable or malformed" in human.stdout
    document = cast("dict[str, object]", json.loads(machine.stdout))
    assert machine.stdout.count("\n") == 1
    assert machine.stderr == ""
    data = cast("dict[str, object]", document["data"])
    groups = cast("list[dict[str, object]]", data["groups"])
    database = next(group for group in groups if group["name"] == "Database")
    checks = cast("list[dict[str, object]]", database["checks"])
    assert checks == [
        {
            "name": "Database",
            "status": "fail",
            "message": "state database schema is unavailable or malformed",
            "hint": None,
        }
    ]
    combined = human.stdout + human.stderr + machine.stdout + machine.stderr
    assert "private_marker" not in combined
    assert str(database_path) not in combined
