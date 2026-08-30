"""Bounded schema checks for the ordinary doctor database path."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.config import Config
from agentworks.db import LATEST_VERSION, AppliedStateKey, Database, SessionMode, VersionedPayload
from agentworks.doctor import HealthGroup, InstanceStateHealthFactType, Status
from agentworks.doctor_state import (
    _live_resource_counts,
    _report_contents,
    append_vm_site_database_checks,
    check_database,
    check_system,
)
from agentworks.errors import StateError
from agentworks.resources.live import LIVE_RESOURCE_KINDS
from agentworks.ssh_identity import UnverifiableSSHIdentity, VerifiedSSHIdentity
from agentworks.vms.applied_state import encode_ssh_identity

_FINGERPRINT = f"SHA256:{'A' * 43}"
_OTHER_FINGERPRINT = f"SHA256:{'E' * 43}"


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


def test_doctor_compares_fleet_ssh_evidence_with_one_configured_key_read(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = Path(db._conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr("agentworks.db.DB_PATH", database_path)
    for name in ("alpha", "beta"):
        db.insert_vm(name, site="local", hostname=name)
        db.instance_state.replace_applied_slices(
            "vm",
            name,
            "vm-create",
            {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/old/key", VerifiedSSHIdentity(_FINGERPRINT))},
        )
    reads = 0

    def read_identity(path: Path) -> VerifiedSSHIdentity:
        nonlocal reads
        reads += 1
        return VerifiedSSHIdentity(_OTHER_FINGERPRINT)

    monkeypatch.setattr("agentworks.ssh_identity.read_private_ssh_identity", read_identity)
    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key=Path("/configured/key")))

    group = check_database(cast("Config", config))

    comparisons = [
        check
        for check in group.checks
        if check.instance_state is not None
        and check.instance_state.fact_type is InstanceStateHealthFactType.APPLIED_COMPARISON
    ]
    assert reads == 1
    assert [(check.instance_state.instance_name, check.status) for check in comparisons if check.instance_state] == [
        ("alpha", Status.FAIL),
        ("beta", Status.FAIL),
    ]


def test_doctor_keeps_independent_instance_state_facts_when_one_row_is_malformed(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = Path(db._conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr("agentworks.db.DB_PATH", database_path)
    db.insert_vm("healthy", site="local", hostname="healthy")
    db.insert_vm("broken", site="local", hostname="broken")
    db.instance_state.replace_applied_slices(
        "vm",
        "healthy",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/old/key", VerifiedSSHIdentity(_FINGERPRINT))},
    )
    db.instance_state.replace_applied_slices(
        "vm",
        "broken",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/old/key", VerifiedSSHIdentity(_FINGERPRINT))},
    )
    db._conn.execute(  # noqa: SLF001
        "UPDATE instance_records SET value_json = ? WHERE instance_name = 'broken'",
        ("private-malformed-payload",),
    )
    db._conn.execute(  # noqa: SLF001
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "agent",
            "orphan",
            "future/type",
            "PRIVATE KEY",
            9,
            '{"private":"hidden-future-payload"}',
            "2026-08-29T12:00:00Z",
            None,
        ),
    )
    db._conn.commit()  # noqa: SLF001
    monkeypatch.setattr(
        "agentworks.ssh_identity.read_private_ssh_identity",
        lambda path: VerifiedSSHIdentity(_FINGERPRINT),
    )
    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key=Path("/configured/key")))

    group = check_database(cast("Config", config))

    facts = [check.instance_state for check in group.checks if check.instance_state is not None]
    assert {fact.fact_type for fact in facts} >= {
        InstanceStateHealthFactType.APPLIED_COMPARISON,
        InstanceStateHealthFactType.MALFORMED_RECORD,
        InstanceStateHealthFactType.ORPHAN_RECORD,
        InstanceStateHealthFactType.UNCONSUMED_RECORD,
    }
    assert any(fact.instance_name == "healthy" and fact.comparison == "match" for fact in facts)
    assert not any(fact.instance_name == "broken" and fact.comparison == "not-recorded" for fact in facts)
    assert "private-malformed-payload" not in repr(group)
    assert "hidden-future-payload" not in repr(group)


def test_doctor_never_projects_unsafe_persisted_diagnostics(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = Path(db._conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr("agentworks.db.DB_PATH", database_path)
    db.insert_vm("box", site="local", hostname="box")
    db.instance_state.put_desired_overlay(
        "vm",
        "box",
        VersionedPayload(2, {"vm": {"PRIVATE-RAW-KEY": "private-raw-value"}}),
    )
    db._conn.execute("PRAGMA ignore_check_constraints = ON")  # noqa: SLF001
    db._conn.executemany(  # noqa: SLF001
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "vm",
                "box",
                "applied-state",
                "ssh-identity",
                1,
                "{}",
                "2026-08-29T12:00:00Z",
                "vm-create\nforged-operation",
            ),
            (
                "agent",
                "orphan",
                "future/type",
                "PRIVATE KEY MATERIAL",
                9,
                "{}",
                "2026-08-29T12:00:00Z",
                None,
            ),
            (
                "agent",
                "victim' forged-owner",
                "future/type",
                "opaque",
                9,
                "{}",
                "2026-08-29T12:00:00Z",
                None,
            ),
        ],
    )
    db._conn.commit()  # noqa: SLF001

    rendered = json.dumps(
        [
            {
                "name": check.name,
                "message": check.message,
                "hint": check.hint,
                "instance_state": None
                if check.instance_state is None
                else {
                    "instance_name": check.instance_state.instance_name,
                    "record_key": check.instance_state.record_key,
                },
            }
            for check in check_database(None).checks
        ]
    )

    for unsafe in (
        "PRIVATE-RAW-KEY",
        "private-raw-value",
        "vm-create\nforged-operation",
        "PRIVATE KEY MATERIAL",
        "victim' forged-owner",
    ):
        assert unsafe not in rendered


def test_doctor_inspects_records_without_config_and_reports_one_coverage_gap(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = Path(db._conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr("agentworks.db.DB_PATH", database_path)
    db.insert_vm("box", site="local", hostname="box")
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/old/key", VerifiedSSHIdentity(_FINGERPRINT))},
    )
    monkeypatch.setattr(
        "agentworks.ssh_identity.read_private_ssh_identity",
        lambda path: pytest.fail(f"unexpected key read: {path}"),
    )

    group = check_database(None)

    coverage = [
        check
        for check in group.checks
        if check.instance_state is not None and check.instance_state.fact_type is InstanceStateHealthFactType.COVERAGE
    ]
    assert len(coverage) == 1
    assert coverage[0].status is Status.WARN


def test_doctor_reports_legacy_unverifiable_evidence_without_parsing_current_key(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = Path(db._conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr("agentworks.db.DB_PATH", database_path)
    db.insert_vm("box", site="local", hostname="box")
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/old/key", UnverifiableSSHIdentity())},
    )
    monkeypatch.setattr(
        "agentworks.ssh_identity.read_private_ssh_identity",
        lambda path: pytest.fail(f"unexpected key read: {path}"),
    )

    group = check_database(None)

    comparisons = [
        check
        for check in group.checks
        if check.instance_state is not None
        and check.instance_state.fact_type is InstanceStateHealthFactType.APPLIED_COMPARISON
    ]
    assert [(check.status, check.instance_state.comparison) for check in comparisons if check.instance_state] == [
        (Status.INFO, "unverifiable")
    ]


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

    counts = _live_resource_counts(db, db.list_vms())
    assert set(counts) == LIVE_RESOURCE_KINDS
    assert counts == {
        "vm": 1,
        "workspace": 2,
        "agent": 3,
        "session": 4,
        "console": 5,
    }

    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    group = HealthGroup("Database")
    _report_contents(group, db)

    assert warnings == []


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
