"""Bounded schema checks for the ordinary doctor database path."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from agentworks.db import LATEST_VERSION, Database
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
    assert "state database is unavailable or malformed" in human.stdout
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
            "message": "state database is unavailable or malformed",
            "hint": None,
        }
    ]
    combined = human.stdout + human.stderr + machine.stdout + machine.stderr
    assert "private_marker" not in combined
    assert str(database_path) not in combined
