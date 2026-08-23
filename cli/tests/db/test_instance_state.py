"""Instance-state schema, repository, transaction, and ownership contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentworks.db import (
    MIGRATIONS,
    AppliedStateSlice,
    Database,
    DesiredOverlayRecord,
    MigrationContext,
    SessionMode,
    VersionedPayload,
)
from agentworks.db import instance_state as state_module
from agentworks.errors import StateError


def _build_schema(path: Path, target_version: int) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "CREATE TABLE schema_version ("
        "version INTEGER NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
    )
    context = MigrationContext()
    for version in range(1, target_version + 1):
        step = MIGRATIONS[version]
        if callable(step):
            step(connection, context)
        else:
            for statement in step.split(";"):
                if statement.strip():
                    connection.execute(statement)
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        connection.commit()
    connection.close()


def _payload(value: str, *, version: int = 1) -> VersionedPayload:
    return VersionedPayload(version, {"value": value})


def _raw_rows(path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute("SELECT * FROM instance_records ORDER BY instance_kind, instance_name").fetchall()
    connection.close()
    return rows


def test_v32_migration_is_additive_and_has_the_exact_record_key_and_batch_index(tmp_path: Path) -> None:
    fresh_path = tmp_path / "fresh.db"
    fresh = Database(fresh_path)
    fresh.close()

    connection = sqlite3.connect(fresh_path)
    columns = connection.execute("PRAGMA table_info(instance_records)").fetchall()
    assert [column[1] for column in columns] == [
        "instance_kind",
        "instance_name",
        "record_kind",
        "record_key",
        "schema_version",
        "value_json",
        "recorded_at",
        "operation",
    ]
    assert [column[5] for column in columns] == [1, 2, 3, 4, 0, 0, 0, 0]
    indexes = {
        row[1]: [item[2] for item in connection.execute(f"PRAGMA index_info('{row[1]}')")]
        for row in connection.execute("PRAGMA index_list(instance_records)")
    }
    assert indexes["idx_instance_records_kind_record"] == [
        "instance_kind",
        "record_kind",
        "instance_name",
        "record_key",
    ]
    assert connection.execute("SELECT COUNT(*) FROM instance_records").fetchone()[0] == 0
    connection.close()

    old_path = tmp_path / "v31.db"
    _build_schema(old_path, 31)
    old_connection = sqlite3.connect(old_path)
    old_connection.execute("INSERT INTO vms (name, site, hostname) VALUES ('existing', 'local', 'existing')")
    old_connection.commit()
    old_connection.close()

    migrated = Database(old_path)
    assert migrated.get_vm("existing") is not None
    assert migrated.instance_state.get_applied_slices("vm", "existing") == ()
    assert migrated.instance_state.get_desired_overlay("vm", "existing") is None
    migrated.close()
    assert _raw_rows(old_path) == []


@pytest.mark.parametrize(
    ("values", "expected_error"),
    [
        (("invalid", "name", "applied-state", "key", 1, "{}", "2026-08-23T12:00:00Z", "op"), "kind"),
        (("vm", "name", "applied-state", "key", 0, "{}", "2026-08-23T12:00:00Z", "op"), "version"),
        (("vm", "name", "desired-overlay", "other", 1, "{}", "2026-08-23T12:00:00Z", None), "desired"),
        (("vm", "name", "applied-state", "key", 1, "{}", "2026-08-23T12:00:00Z", None), "operation"),
    ],
)
def test_v32_constraints_reject_invalid_envelopes(
    tmp_path: Path,
    values: tuple[object, ...],
    expected_error: str,
) -> None:
    path = tmp_path / f"{expected_error}.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO instance_records "
            "(instance_kind, instance_name, record_kind, record_key, schema_version, "
            "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    connection.close()


def test_v32_envelope_does_not_invent_constraints_for_future_record_kinds(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_kind, record_key, schema_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("vm", "alpha", "future-consumer", "opaque", 1, "{}", "2026-08-23T12:00:00Z", None),
    )
    assert connection.execute("SELECT COUNT(*) FROM instance_records").fetchone()[0] == 1
    connection.close()


def test_desired_overlay_round_trip_upsert_clear_and_canonical_storage(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    try:
        assert database.instance_state.get_desired_overlay("vm", "alpha") is None
        first = database.instance_state.put_desired_overlay(
            "vm",
            "alpha",
            VersionedPayload(2, {"z": [2, 1], "a": {"enabled": True}}),
        )
        assert isinstance(first, DesiredOverlayRecord)
        assert database.instance_state.get_desired_overlay("vm", "alpha") == first

        second = database.instance_state.put_desired_overlay("vm", "alpha", _payload("new", version=3))
        assert database.instance_state.get_desired_overlay("vm", "alpha") == second
        assert len(database.instance_state.list_desired_overlays("vm")) == 1

        raw = _raw_rows(path)[0]
        assert raw["value_json"] == '{"value":"new"}'
        assert raw["schema_version"] == 3
        assert raw["operation"] is None

        database.instance_state.clear_desired_overlay("vm", "alpha")
        assert database.instance_state.get_desired_overlay("vm", "alpha") is None
    finally:
        database.close()


def test_applied_partial_replace_preserves_unrelated_slices_and_orders_results(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter(["2026-08-23T12:00:00Z", "2026-08-23T12:01:00Z", "2026-08-23T12:02:00Z"])
    monkeypatch.setattr(state_module, "_utc_now", lambda: next(timestamps))

    first = db.instance_state.replace_applied_slices(
        "vm",
        "alpha",
        "vm-create",
        {"ssh": _payload("old-ssh"), "config": _payload("old-config")},
    )
    assert [record.key for record in first] == ["config", "ssh"]
    assert {record.recorded_at for record in first} == {"2026-08-23T12:00:00Z"}
    assert {record.operation for record in first} == {"vm-create"}

    second = db.instance_state.replace_applied_slices(
        "vm",
        "alpha",
        "vm-reinit",
        {"config": _payload("new-config", version=2), "packages": _payload("new-packages")},
    )
    assert [record.key for record in second] == ["config", "packages", "ssh"]
    by_key = {record.key: record for record in second}
    assert by_key["ssh"] == first[1]
    assert by_key["config"].payload == _payload("new-config", version=2)
    assert by_key["config"].recorded_at == by_key["packages"].recorded_at == "2026-08-23T12:01:00Z"
    assert by_key["config"].operation == by_key["packages"].operation == "vm-reinit"

    db.instance_state.replace_applied_slices("vm", "zeta", "vm-create", {"later": _payload("z")})
    listed = db.instance_state.list_applied_slices("vm")
    assert [(record.instance_name, record.key) for record in listed] == [
        ("alpha", "config"),
        ("alpha", "packages"),
        ("alpha", "ssh"),
        ("zeta", "later"),
    ]


def test_empty_applied_replace_is_a_no_op(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = db.instance_state.replace_applied_slices("session", "one", "resume", {"spec": _payload("v")})

    def fail_timestamp() -> str:
        raise AssertionError("an empty replacement must not mint provenance")

    monkeypatch.setattr(state_module, "_utc_now", fail_timestamp)
    assert db.instance_state.replace_applied_slices("session", "one", "resume", {}) == existing


def test_replace_preencodes_all_payloads_before_writing(db: Database) -> None:
    invalid_value = {"value": "initially-valid"}
    invalid = VersionedPayload(1, invalid_value)
    invalid_value["invalid"] = object()  # type: ignore[assignment]
    with pytest.raises(TypeError):
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create",
            {"first": _payload("valid"), "second": invalid},
        )
    assert db.instance_state.get_applied_slices("vm", "alpha") == ()


def test_replace_rolls_back_every_slice_when_one_statement_fails(db: Database) -> None:
    db._conn.execute(  # noqa: SLF001
        "CREATE TRIGGER reject_second_slice BEFORE INSERT ON instance_records "
        "WHEN NEW.record_key = 'second' BEGIN SELECT RAISE(ABORT, 'rejected'); END"
    )
    db._conn.commit()  # noqa: SLF001

    with pytest.raises(sqlite3.IntegrityError):
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create",
            {"first": _payload("one"), "second": _payload("two")},
        )
    assert db.instance_state.get_applied_slices("vm", "alpha") == ()


def test_failed_nested_replace_does_not_poison_or_partially_write_outer_transaction(db: Database) -> None:
    db._conn.execute(  # noqa: SLF001
        "CREATE TRIGGER reject_second_slice BEFORE INSERT ON instance_records "
        "WHEN NEW.record_key = 'second' BEGIN SELECT RAISE(ABORT, 'rejected'); END"
    )
    db._conn.commit()  # noqa: SLF001

    with db.transaction():
        with pytest.raises(sqlite3.IntegrityError):
            db.instance_state.replace_applied_slices(
                "vm",
                "alpha",
                "vm-create",
                {"first": _payload("one"), "second": _payload("two")},
            )
        expected = db.instance_state.put_desired_overlay("vm", "alpha", _payload("intent"))

    assert db.instance_state.get_applied_slices("vm", "alpha") == ()
    assert db.instance_state.get_desired_overlay("vm", "alpha") == expected


def test_repository_writes_join_an_enclosing_transaction(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction():
        db.instance_state.put_desired_overlay("workspace", "alpha", _payload("intent"))
        db.instance_state.replace_applied_slices("workspace", "alpha", "repair", {"spec": _payload("fact")})
        raise RuntimeError("roll back")

    assert db.instance_state.get_desired_overlay("workspace", "alpha") is None
    assert db.instance_state.get_applied_slices("workspace", "alpha") == ()


def test_transaction_joins_an_existing_implicit_transaction(db: Database) -> None:
    db._conn.execute(  # noqa: SLF001
        "INSERT INTO vms (name, site, hostname) VALUES ('pending', 'local', 'pending')"
    )

    with pytest.raises(RuntimeError), db.transaction():
        db.instance_state.put_desired_overlay("vm", "pending", _payload("intent"))
        raise RuntimeError("roll back")

    assert db.get_vm("pending") is None
    assert db.instance_state.get_desired_overlay("vm", "pending") is None


def test_repository_reads_share_a_read_only_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    reader = Database(path, read_only=True)
    writer = Database(path)
    try:
        with reader.read_transaction():
            assert reader.instance_state.get_desired_overlay("agent", "alpha") is None
            writer.instance_state.put_desired_overlay("agent", "alpha", _payload("intent"))
            assert reader.instance_state.get_desired_overlay("agent", "alpha") is None

        assert reader.instance_state.get_desired_overlay("agent", "alpha") is not None
    finally:
        reader.close()
        writer.close()


def test_malformed_persisted_record_raises_instead_of_becoming_absent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_kind, record_key, schema_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("vm", "alpha", "desired-overlay", "spec", 1, "not-json", "2026-08-23T12:00:00Z", None),
    )
    connection.commit()
    connection.close()

    database = Database(path, read_only=True)
    try:
        with pytest.raises(StateError) as raised:
            database.instance_state.get_desired_overlay("vm", "alpha")
        assert raised.value.entity_kind == "database"
    finally:
        database.close()


def _create_owner_tree(database: Database, suffix: str = "") -> tuple[str, str, str, str]:
    vm = f"vm{suffix}"
    workspace = f"workspace{suffix}"
    agent = f"agent{suffix}"
    session = f"session{suffix}"
    database.insert_vm(vm, site="local", hostname=vm)
    database.insert_workspace(workspace, "/tmp/workspace", vm, "workspace-group")
    database.insert_agent(agent, vm, "agent-user")
    database.insert_session(session, workspace, "template", SessionMode.AGENT, agent, socket_path="/tmp/socket")
    return vm, workspace, agent, session


def test_owner_and_aggregate_deletes_remove_all_owned_records(db: Database) -> None:
    vm, workspace, agent, session = _create_owner_tree(db)
    for kind, name in (("vm", vm), ("workspace", workspace), ("agent", agent), ("session", session)):
        db.instance_state.put_desired_overlay(kind, name, _payload(name))

    db.delete_vm(vm)
    for kind, name in (("vm", vm), ("workspace", workspace), ("agent", agent), ("session", session)):
        assert db.instance_state.get_desired_overlay(kind, name) is None

    _, workspace, agent, session = _create_owner_tree(db, "-individual")
    for kind, name in (("workspace", workspace), ("agent", agent), ("session", session)):
        db.instance_state.put_desired_overlay(kind, name, _payload(name))
    db.delete_session(session)
    db.delete_agent(agent)
    assert db.instance_state.get_desired_overlay("session", session) is None
    assert db.instance_state.get_desired_overlay("agent", agent) is None

    second_session = f"{session}-second"
    db.insert_session(second_session, workspace, "template", SessionMode.ADMIN, socket_path="/tmp/socket-2")
    db.instance_state.put_desired_overlay("session", second_session, _payload(second_session))
    db.delete_workspace(workspace)
    assert db.instance_state.get_desired_overlay("workspace", workspace) is None
    assert db.instance_state.get_desired_overlay("session", second_session) is None

    _, batch_workspace, _, batch_session = _create_owner_tree(db, "-batch")
    other_session = f"{batch_session}-other"
    db.insert_session(other_session, batch_workspace, "template", SessionMode.ADMIN, socket_path="/tmp/socket-3")
    for name in (batch_session, other_session):
        db.instance_state.put_desired_overlay("session", name, _payload(name))
    deleted = db.delete_sessions_for_workspace(batch_workspace)
    assert {row.name for row in deleted} == {batch_session, other_session}
    assert db.instance_state.get_desired_overlay("session", batch_session) is None
    assert db.instance_state.get_desired_overlay("session", other_session) is None


def test_owner_delete_rolls_back_record_cleanup_when_owner_delete_fails(db: Database) -> None:
    vm, _, _, _ = _create_owner_tree(db)
    expected = db.instance_state.put_desired_overlay("vm", vm, _payload("intent"))
    db._conn.execute(  # noqa: SLF001
        "CREATE TRIGGER reject_vm_delete BEFORE DELETE ON vms BEGIN SELECT RAISE(ABORT, 'rejected'); END"
    )
    db._conn.commit()  # noqa: SLF001

    with pytest.raises(sqlite3.IntegrityError):
        db.delete_vm(vm)
    assert db.get_vm(vm) is not None
    assert db.instance_state.get_desired_overlay("vm", vm) == expected


def test_standalone_mutations_commit_before_returning(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    writer = Database(path)
    reader = Database(path, read_only=True)
    try:
        expected = writer.instance_state.put_desired_overlay("vm", "alpha", _payload("intent"))
        assert reader.instance_state.get_desired_overlay("vm", "alpha") == expected

        writer.instance_state.delete_instance_records("vm", "alpha")
        assert reader.instance_state.get_desired_overlay("vm", "alpha") is None
    finally:
        reader.close()
        writer.close()


def test_carriers_are_frozen() -> None:
    payload = _payload("value")
    desired = DesiredOverlayRecord("vm", "alpha", payload, "2026-08-23T12:00:00Z")
    applied = AppliedStateSlice("vm", "alpha", "spec", payload, "vm-create", "2026-08-23T12:00:00Z")
    with pytest.raises(AttributeError):
        desired.instance_name = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        applied.operation = "other"  # type: ignore[misc]


def test_versioned_payload_rejects_invalid_envelope_values() -> None:
    with pytest.raises(ValueError):
        VersionedPayload(0, {})
    with pytest.raises(TypeError):
        VersionedPayload(1, [])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        VersionedPayload(1, {1: "coerced-key"})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        VersionedPayload(1, {"value": float("nan")})
