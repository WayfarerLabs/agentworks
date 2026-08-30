"""Instance-state schema, repository, transaction, and ownership contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentworks.db import (
    MIGRATIONS,
    AppliedStateKey,
    AppliedStateSlice,
    Database,
    DesiredOverlayRecord,
    InspectedAppliedStateSlice,
    InspectedDesiredOverlay,
    InstanceRecordDiagnostic,
    InstanceRecordMetadata,
    MigrationContext,
    SessionMode,
    VersionedPayload,
)
from agentworks.db import instance_state as state_module
from agentworks.errors import StateError

_HISTORIC_SESSION_NAME = f"{'w' * 40}--{'a' * 30}"


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


def _insert_raw_applied_row(
    path: Path,
    *,
    record_key: str,
    value_json: str = "{}",
    operation: str | None = "op",
    ignore_checks: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    if ignore_checks:
        connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("vm", "alpha", "applied-state", record_key, 1, value_json, "2026-08-23T12:00:00Z", operation),
    )
    connection.commit()
    connection.close()


def test_v32_migration_is_additive_and_has_the_exact_record_key_and_batch_index(tmp_path: Path) -> None:
    fresh_path = tmp_path / "fresh.db"
    fresh = Database(fresh_path)
    fresh.close()

    connection = sqlite3.connect(fresh_path)
    columns = connection.execute("PRAGMA table_info(instance_records)").fetchall()
    assert [column[1] for column in columns] == [
        "instance_kind",
        "instance_name",
        "record_type",
        "record_key",
        "payload_version",
        "value_json",
        "recorded_at",
        "operation",
    ]
    assert [column[5] for column in columns] == [1, 2, 3, 4, 0, 0, 0, 0]
    indexes = {
        row[1]: [item[2] for item in connection.execute(f"PRAGMA index_info('{row[1]}')")]
        for row in connection.execute("PRAGMA index_list(instance_records)")
    }
    assert indexes["idx_instance_records_kind_type"] == [
        "instance_kind",
        "record_type",
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
            "(instance_kind, instance_name, record_type, record_key, payload_version, "
            "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    connection.close()


def test_v32_envelope_does_not_invent_constraints_for_future_record_types(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
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
        assert raw["payload_version"] == 3
        assert raw["operation"] is None

        database.instance_state.clear_desired_overlay("vm", "alpha")
        assert database.instance_state.get_desired_overlay("vm", "alpha") is None
    finally:
        database.close()


def test_persistence_accepts_safe_historic_owner_names(db: Database) -> None:
    assert len(_HISTORIC_SESSION_NAME) > 64

    expected = db.instance_state.put_desired_overlay("session", _HISTORIC_SESSION_NAME, _payload("intent"))

    assert db.instance_state.get_desired_overlay("session", _HISTORIC_SESSION_NAME) == expected


@pytest.mark.parametrize("instance_name", ["", 1, True, "bad\nname", "victim' forged-owner"])
def test_desired_mutation_rejects_invalid_owner_name_before_writing(
    db: Database,
    instance_name: object,
) -> None:
    with pytest.raises(ValueError):
        db.instance_state.put_desired_overlay(
            "vm",
            instance_name,  # type: ignore[arg-type]
            _payload("intent"),
        )

    assert db.instance_state.list_desired_overlays("vm") == ()


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
        {
            AppliedStateKey.SSH_IDENTITY: _payload("old-ssh"),
            AppliedStateKey.HARDWARE_PROVENANCE: _payload("old-hardware"),
        },
    )
    assert [record.key for record in first] == [
        AppliedStateKey.HARDWARE_PROVENANCE,
        AppliedStateKey.SSH_IDENTITY,
    ]
    assert {record.recorded_at for record in first} == {"2026-08-23T12:00:00Z"}
    assert {record.operation for record in first} == {"vm-create"}

    second = db.instance_state.replace_applied_slices(
        "vm",
        "alpha",
        "vm-reinit",
        {AppliedStateKey.HARDWARE_PROVENANCE: _payload("new-hardware", version=2)},
    )
    assert [record.key for record in second] == [
        AppliedStateKey.HARDWARE_PROVENANCE,
        AppliedStateKey.SSH_IDENTITY,
    ]
    by_key = {record.key: record for record in second}
    assert by_key[AppliedStateKey.SSH_IDENTITY] == first[1]
    assert by_key[AppliedStateKey.HARDWARE_PROVENANCE].payload == _payload("new-hardware", version=2)
    assert by_key[AppliedStateKey.HARDWARE_PROVENANCE].recorded_at == "2026-08-23T12:01:00Z"
    assert by_key[AppliedStateKey.HARDWARE_PROVENANCE].operation == "vm-reinit"

    db.instance_state.replace_applied_slices(
        "vm",
        "zeta",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: _payload("z")},
    )
    listed = db.instance_state.list_applied_slices("vm")
    assert [(record.instance_name, record.key) for record in listed] == [
        ("alpha", AppliedStateKey.HARDWARE_PROVENANCE),
        ("alpha", AppliedStateKey.SSH_IDENTITY),
        ("zeta", AppliedStateKey.SSH_IDENTITY),
    ]


def test_empty_applied_replace_is_a_no_op(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = db.instance_state.replace_applied_slices(
        "vm",
        "one",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: _payload("v")},
    )

    def fail_timestamp() -> str:
        raise AssertionError("an empty replacement must not mint provenance")

    monkeypatch.setattr(state_module, "_utc_now", fail_timestamp)
    assert db.instance_state.replace_applied_slices("vm", "one", "vm-create", {}) == existing


def test_clear_applied_slice_removes_only_requested_registered_key(db: Database) -> None:
    db.instance_state.replace_applied_slices(
        "vm",
        "one",
        "vm-create",
        {
            AppliedStateKey.HARDWARE_PROVENANCE: _payload("hardware"),
            AppliedStateKey.SSH_IDENTITY: _payload("ssh"),
        },
    )
    db.instance_state.replace_applied_slices(
        "vm",
        "two",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: _payload("other")},
    )

    db.instance_state.clear_applied_slice("vm", "one", AppliedStateKey.SSH_IDENTITY)

    assert [record.key for record in db.instance_state.get_applied_slices("vm", "one")] == [
        AppliedStateKey.HARDWARE_PROVENANCE
    ]
    assert [record.key for record in db.instance_state.get_applied_slices("vm", "two")] == [
        AppliedStateKey.SSH_IDENTITY
    ]
    db.instance_state.clear_applied_slice("vm", "one", AppliedStateKey.SSH_IDENTITY)


def test_clear_applied_slice_rejects_unregistered_and_cross_kind_keys(db: Database) -> None:
    with pytest.raises(TypeError):
        db.instance_state.clear_applied_slice("vm", "one", "ssh-identity")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        db.instance_state.clear_applied_slice("agent", "one", AppliedStateKey.SSH_IDENTITY)


def test_clear_applied_slice_joins_enclosing_transaction(db: Database) -> None:
    db.instance_state.replace_applied_slices(
        "vm",
        "one",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: _payload("ssh")},
    )

    with pytest.raises(RuntimeError), db.transaction():
        db.instance_state.clear_applied_slice("vm", "one", AppliedStateKey.SSH_IDENTITY)
        raise RuntimeError("roll back")

    assert [record.key for record in db.instance_state.get_applied_slices("vm", "one")] == [
        AppliedStateKey.SSH_IDENTITY
    ]


@pytest.mark.parametrize("operation", ["", 1, True])
def test_applied_replace_rejects_invalid_operation_before_writing(db: Database, operation: object) -> None:
    with pytest.raises(ValueError):
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            operation,  # type: ignore[arg-type]
            {AppliedStateKey.HARDWARE_PROVENANCE: _payload("value")},
        )

    assert db.instance_state.get_applied_slices("vm", "alpha") == ()


def test_applied_replace_rejects_unknown_key_before_writing(db: Database) -> None:
    with pytest.raises(TypeError):
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create",
            {"unknown": _payload("value")},  # type: ignore[dict-item]
        )

    assert db.instance_state.get_applied_slices("vm", "alpha") == ()


def test_registered_applied_keys_follow_the_persisted_key_grammar() -> None:
    assert all(state_module._is_well_formed_applied_key(key.value) for key in AppliedStateKey)  # noqa: SLF001


@pytest.mark.parametrize("instance_kind", ["workspace", "agent", "session"])
@pytest.mark.parametrize(
    "key",
    [AppliedStateKey.HARDWARE_PROVENANCE, AppliedStateKey.SSH_IDENTITY],
)
def test_applied_replace_rejects_cross_kind_keys_before_writing(
    db: Database,
    instance_kind: str,
    key: AppliedStateKey,
) -> None:
    with pytest.raises(ValueError):
        db.instance_state.replace_applied_slices(
            instance_kind,  # type: ignore[arg-type]
            "alpha",
            "resume",
            {key: _payload("value")},
        )

    assert db.instance_state.get_applied_slices(instance_kind, "alpha") == ()  # type: ignore[arg-type]


def test_replace_preencodes_all_payloads_before_writing(db: Database) -> None:
    invalid_value = {"value": "initially-valid"}
    invalid = VersionedPayload(1, invalid_value)
    invalid_value["invalid"] = object()  # type: ignore[assignment]
    with pytest.raises(TypeError):
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create",
            {
                AppliedStateKey.HARDWARE_PROVENANCE: _payload("valid"),
                AppliedStateKey.SSH_IDENTITY: invalid,
            },
        )
    assert db.instance_state.get_applied_slices("vm", "alpha") == ()


def test_replace_rolls_back_every_slice_when_one_statement_fails(db: Database) -> None:
    db._conn.execute(  # noqa: SLF001
        "CREATE TRIGGER reject_second_slice BEFORE INSERT ON instance_records "
        "WHEN NEW.record_key = 'ssh-identity' BEGIN SELECT RAISE(ABORT, 'rejected'); END"
    )
    db._conn.commit()  # noqa: SLF001

    with pytest.raises(sqlite3.IntegrityError):
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create",
            {
                AppliedStateKey.HARDWARE_PROVENANCE: _payload("one"),
                AppliedStateKey.SSH_IDENTITY: _payload("two"),
            },
        )
    assert db.instance_state.get_applied_slices("vm", "alpha") == ()


def test_failed_nested_replace_does_not_poison_or_partially_write_outer_transaction(db: Database) -> None:
    db._conn.execute(  # noqa: SLF001
        "CREATE TRIGGER reject_second_slice BEFORE INSERT ON instance_records "
        "WHEN NEW.record_key = 'ssh-identity' BEGIN SELECT RAISE(ABORT, 'rejected'); END"
    )
    db._conn.commit()  # noqa: SLF001

    with db.transaction():
        with pytest.raises(sqlite3.IntegrityError):
            db.instance_state.replace_applied_slices(
                "vm",
                "alpha",
                "vm-create",
                {
                    AppliedStateKey.HARDWARE_PROVENANCE: _payload("one"),
                    AppliedStateKey.SSH_IDENTITY: _payload("two"),
                },
            )
        expected = db.instance_state.put_desired_overlay("vm", "alpha", _payload("intent"))

    assert db.instance_state.get_applied_slices("vm", "alpha") == ()
    assert db.instance_state.get_desired_overlay("vm", "alpha") == expected


def test_repository_writes_join_an_enclosing_transaction(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction():
        db.instance_state.put_desired_overlay("vm", "alpha", _payload("intent"))
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create",
            {AppliedStateKey.HARDWARE_PROVENANCE: _payload("fact")},
        )
        raise RuntimeError("roll back")

    assert db.instance_state.get_desired_overlay("vm", "alpha") is None
    assert db.instance_state.get_applied_slices("vm", "alpha") == ()


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


def test_write_transaction_establishes_snapshot_before_its_first_read(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    snapshot = Database(path)
    writer = Database(path)
    try:
        with snapshot.transaction():
            assert snapshot.list_vms() == []
            writer.insert_vm("new-vm", site="local", hostname="new-vm")
            assert snapshot.list_vms() == []

        assert [vm.name for vm in snapshot.list_vms()] == ["new-vm"]
    finally:
        snapshot.close()
        writer.close()


def test_malformed_persisted_record_raises_instead_of_becoming_absent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    secret = "do-not-disclose-this-persisted-value"
    connection.execute(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("vm", "alpha", "desired-overlay", "spec", 1, secret, "2026-08-23T12:00:00Z", None),
    )
    connection.commit()
    connection.close()

    database = Database(path, read_only=True)
    try:
        with pytest.raises(StateError) as raised:
            database.instance_state.get_desired_overlay("vm", "alpha")
        assert raised.value.entity_kind == "vm"
        assert raised.value.entity_name == "alpha"
        assert raised.value.hint is not None
        assert "alpha" in str(raised.value)
        assert secret not in str(raised.value)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("instance_kind", "record_key"),
    [
        *[
            (instance_kind, key.value)
            for instance_kind in ("workspace", "agent", "session")
            for key in (AppliedStateKey.HARDWARE_PROVENANCE, AppliedStateKey.SSH_IDENTITY)
        ],
    ],
)
def test_persisted_cross_kind_applied_key_is_malformed(
    tmp_path: Path,
    instance_kind: str,
    record_key: str,
) -> None:
    path = tmp_path / f"{instance_kind}.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (instance_kind, "alpha", "applied-state", record_key, 1, "{}", "2026-08-23T12:00:00Z", "op"),
    )
    connection.commit()
    connection.close()

    database = Database(path, read_only=True)
    try:
        with pytest.raises(StateError) as raised:
            database.instance_state.get_applied_slices(instance_kind, "alpha")  # type: ignore[arg-type]
        assert raised.value.entity_kind == instance_kind
        assert raised.value.entity_name == "alpha"
        assert raised.value.hint is not None
    finally:
        database.close()


def test_persisted_unknown_applied_key_is_ignored_and_preserved(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    connection.executemany(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("vm", "alpha", "applied-state", "future-fact", 1, "{}", "2026-08-23T12:00:00Z", "future"),
            (
                "vm",
                "alpha",
                "applied-state",
                AppliedStateKey.HARDWARE_PROVENANCE,
                1,
                "{}",
                "2026-08-23T12:00:00Z",
                "vm-create",
            ),
        ],
    )
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        assert [record.key for record in database.instance_state.get_applied_slices("vm", "alpha")] == [
            AppliedStateKey.HARDWARE_PROVENANCE
        ]
        assert [record.key for record in database.instance_state.list_applied_slices("vm")] == [
            AppliedStateKey.HARDWARE_PROVENANCE
        ]

        database.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create",
            {AppliedStateKey.SSH_IDENTITY: _payload("identity")},
        )

        raw_keys = database._conn.execute(  # noqa: SLF001
            "SELECT record_key FROM instance_records "
            "WHERE instance_kind = 'vm' AND instance_name = 'alpha' AND record_type = 'applied-state' "
            "ORDER BY record_key"
        ).fetchall()
        assert [row[0] for row in raw_keys] == ["future-fact", "hardware-provenance", "ssh-identity"]
        assert [record.key for record in database.instance_state.get_applied_slices("vm", "alpha")] == [
            AppliedStateKey.HARDWARE_PROVENANCE,
            AppliedStateKey.SSH_IDENTITY,
        ]
        database.instance_state.clear_applied_slice("vm", "alpha", AppliedStateKey.HARDWARE_PROVENANCE)
        database.instance_state.clear_applied_slice("vm", "alpha", AppliedStateKey.SSH_IDENTITY)
        remaining_keys = database._conn.execute(  # noqa: SLF001
            "SELECT record_key FROM instance_records "
            "WHERE instance_kind = 'vm' AND instance_name = 'alpha' AND record_type = 'applied-state'"
        ).fetchall()
        assert [row[0] for row in remaining_keys] == ["future-fact"]
    finally:
        database.close()


@pytest.mark.parametrize("record_key", [" ", "bad\nkey", "bad\0key", "x" * 65])
def test_persisted_malformed_unknown_applied_key_raises(tmp_path: Path, record_key: str) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    _insert_raw_applied_row(path, record_key=record_key)

    database = Database(path, read_only=True)
    try:
        with pytest.raises(StateError) as raised:
            database.instance_state.get_applied_slices("vm", "alpha")
        assert raised.value.entity_kind == "vm"
        assert raised.value.entity_name == "alpha"
        assert raised.value.hint is not None
        assert repr(record_key) not in str(raised.value)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("value_json", "operation"),
    [
        ("not-json", "future"),
        ("{}", None),
    ],
)
def test_persisted_valid_unknown_key_with_malformed_envelope_raises(
    tmp_path: Path,
    value_json: str,
    operation: str | None,
) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    _insert_raw_applied_row(
        path,
        record_key="future-fact",
        value_json=value_json,
        operation=operation,
        ignore_checks=True,
    )

    database = Database(path, read_only=True)
    try:
        with pytest.raises(StateError) as raised:
            database.instance_state.get_applied_slices("vm", "alpha")
        assert raised.value.entity_kind == "vm"
        assert raised.value.entity_name == "alpha"
        assert raised.value.hint is not None
    finally:
        database.close()


def test_owner_inspection_keeps_recognized_and_future_records_closed(db: Database) -> None:
    db.insert_vm("alpha", site="local", hostname="alpha")
    desired = db.instance_state.put_desired_overlay("vm", "alpha", _payload("intent"))
    [applied] = db.instance_state.replace_applied_slices(
        "vm",
        "alpha",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: _payload("identity")},
    )
    db._conn.execute(  # noqa: SLF001
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("vm", "alpha", "future-record", "opaque", 7, '{"secret":"hidden"}', "2026-08-23T12:00:00Z", None),
    )
    db._conn.commit()  # noqa: SLF001

    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)  # noqa: SLF001
    try:
        inspection = db.instance_state.inspect_owner_state("vm", "alpha")
    finally:
        db._conn.set_trace_callback(None)  # noqa: SLF001

    assert not any("EXISTS(" in statement.upper() for statement in statements)
    assert inspection.desired_overlays == (
        InspectedDesiredOverlay(
            desired,
            InstanceRecordMetadata(
                "vm",
                "alpha",
                "desired-overlay",
                "spec",
                1,
                desired.recorded_at,
                True,
            ),
        ),
    )
    assert inspection.applied_slices == (
        InspectedAppliedStateSlice(
            applied,
            InstanceRecordMetadata(
                "vm",
                "alpha",
                "applied-state",
                "ssh-identity",
                1,
                applied.recorded_at,
                True,
            ),
        ),
    )
    assert len(inspection.unconsumed_records) == 1
    metadata = inspection.unconsumed_records[0].metadata
    assert (metadata.record_type, metadata.record_key, metadata.payload_version) == (
        "future-record",
        "opaque",
        7,
    )
    assert "hidden" not in repr(inspection)
    assert inspection.malformed_records == ()


def test_fleet_inspection_isolates_malformed_rows_and_reports_orphans(db: Database) -> None:
    db.insert_vm("live", site="local", hostname="live")
    db.instance_state.put_desired_overlay("vm", "live", _payload("intent"))
    db.instance_state.put_desired_overlay("agent", "orphan", _payload("intent"))
    db.instance_state.replace_applied_slices(
        "vm",
        "broken",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: VersionedPayload(1, {})},
    )
    db._conn.execute(  # noqa: SLF001
        "UPDATE instance_records SET value_json = ? WHERE instance_kind = 'vm' AND instance_name = 'broken'",
        ("not-json-private-payload",),
    )
    db._conn.commit()  # noqa: SLF001

    inspection = db.instance_state.inspect_all_instance_state()

    assert [(item.record.instance_name, item.metadata.owner_exists) for item in inspection.desired_overlays] == [
        ("orphan", False),
        ("live", True),
    ]
    assert inspection.applied_slices == ()
    assert len(inspection.malformed_records) == 1
    malformed = inspection.malformed_records[0]
    assert malformed.metadata.instance_name == "broken"
    assert malformed.metadata.owner_exists is False
    assert "not-json-private-payload" not in repr(malformed)


def test_fleet_inspection_classifies_unknown_applied_key_without_payload(db: Database) -> None:
    db.insert_vm("alpha", site="local", hostname="alpha")
    db._conn.execute(  # noqa: SLF001
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "vm",
            "alpha",
            "applied-state",
            "future-fact",
            2,
            '{"private":"hidden"}',
            "2026-08-23T12:00:00Z",
            "vm-create",
        ),
    )
    db._conn.commit()  # noqa: SLF001

    [unconsumed] = db.instance_state.inspect_all_instance_state().unconsumed_records

    assert unconsumed.metadata.owner_exists is True
    assert unconsumed.metadata.record_key == "future-fact"
    assert "hidden" not in repr(unconsumed)


def test_malformed_inspection_drops_exception_and_raw_payload(db: Database) -> None:
    db._conn.execute("PRAGMA ignore_check_constraints = ON")  # noqa: SLF001
    db._conn.execute(  # noqa: SLF001
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "vm",
            "broken",
            "applied-state",
            "ssh-identity",
            1,
            "private-raw-payload",
            "2026-08-23T12:00:00Z",
            "vm-create",
        ),
    )
    db._conn.commit()  # noqa: SLF001

    [malformed] = db.instance_state.inspect_all_instance_state().malformed_records

    assert malformed.diagnostic is InstanceRecordDiagnostic.INVALID_PAYLOAD
    assert not hasattr(malformed, "error")
    assert "private-raw-payload" not in repr(malformed)


def test_future_inspection_omits_unsafe_optional_metadata(db: Database) -> None:
    db._conn.execute(  # noqa: SLF001
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "vm",
            "alpha",
            "future/type",
            "PRIVATE KEY MATERIAL",
            4,
            "{}",
            "2026-08-23T12:00:00Z",
            None,
        ),
    )
    db._conn.commit()  # noqa: SLF001

    [unconsumed] = db.instance_state.inspect_all_instance_state().unconsumed_records

    assert unconsumed.metadata.record_type == "future/type"
    assert unconsumed.metadata.record_key is None
    assert "PRIVATE KEY MATERIAL" not in repr(unconsumed)


@pytest.mark.parametrize("unsafe_name", ["bad\nname", "victim' forged-row"])
def test_fleet_inspection_never_retains_unsafe_owner_text(db: Database, unsafe_name: str) -> None:
    db._conn.execute(  # noqa: SLF001
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "vm",
            unsafe_name,
            "future-record",
            "opaque",
            1,
            "{}",
            "2026-08-23T12:00:00Z",
            None,
        ),
    )
    db._conn.commit()  # noqa: SLF001

    [malformed] = db.instance_state.inspect_all_instance_state().malformed_records

    assert malformed.diagnostic is InstanceRecordDiagnostic.INVALID_INSTANCE_NAME
    assert malformed.metadata.instance_name is None
    assert unsafe_name not in repr(malformed)


def test_applied_operation_is_safe_on_write_and_inspection(db: Database) -> None:
    with pytest.raises(ValueError):
        db.instance_state.replace_applied_slices(
            "vm",
            "alpha",
            "vm-create\nforged",
            {AppliedStateKey.SSH_IDENTITY: _payload("identity")},
        )

    db._conn.execute("PRAGMA ignore_check_constraints = ON")  # noqa: SLF001
    db._conn.execute(  # noqa: SLF001
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "vm",
            "alpha",
            "applied-state",
            "ssh-identity",
            1,
            "{}",
            "2026-08-23T12:00:00Z",
            "vm-create\nforged",
        ),
    )
    db._conn.commit()  # noqa: SLF001

    [malformed] = db.instance_state.inspect_all_instance_state().malformed_records

    assert malformed.diagnostic is InstanceRecordDiagnostic.INVALID_APPLIED_OPERATION
    assert "vm-create\nforged" not in repr(malformed)


@pytest.mark.parametrize("unsafe_name", ["bad\nname", "x" * 1_000])
def test_malformed_unsafe_owner_identity_retains_kind_context(tmp_path: Path, unsafe_name: str) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    secret = "do-not-disclose-this-persisted-value"
    connection.execute(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("vm", unsafe_name, "desired-overlay", "spec", 1, secret, "2026-08-23T12:00:00Z", None),
    )
    connection.commit()
    connection.close()

    database = Database(path, read_only=True)
    try:
        with pytest.raises(StateError) as raised:
            database.instance_state.list_desired_overlays("vm")
        assert raised.value.entity_kind == "vm"
        assert raised.value.entity_name is None
        assert raised.value.hint is not None
        assert unsafe_name not in str(raised.value)
        assert secret not in str(raised.value)
    finally:
        database.close()


@pytest.mark.parametrize(
    ("instance_kind", "instance_name"),
    [
        ("session", _HISTORIC_SESSION_NAME),
        ("vm", "legacy.vm"),
    ],
)
def test_malformed_printable_legacy_owner_is_attributed(
    tmp_path: Path,
    instance_kind: str,
    instance_name: str,
) -> None:
    path = tmp_path / f"{instance_kind}.db"
    Database(path).close()
    connection = sqlite3.connect(path)
    secret = "do-not-disclose-this-persisted-value"
    connection.execute(
        "INSERT INTO instance_records "
        "(instance_kind, instance_name, record_type, record_key, payload_version, "
        "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (instance_kind, instance_name, "desired-overlay", "spec", 1, secret, "2026-08-23T12:00:00Z", None),
    )
    connection.commit()
    connection.close()

    database = Database(path, read_only=True)
    try:
        with pytest.raises(StateError) as raised:
            database.instance_state.list_desired_overlays(instance_kind)  # type: ignore[arg-type]
        assert raised.value.entity_kind == instance_kind
        assert raised.value.entity_name == instance_name
        assert instance_name in str(raised.value)
        assert secret not in str(raised.value)
    finally:
        database.close()


def _create_owner_tree(database: Database, suffix: str = "") -> tuple[str, str, str, str]:
    vm = f"vm{suffix}"
    workspace = f"workspace{suffix}"
    agent = f"agent{suffix}"
    session = f"session{suffix}"
    database.insert_vm(vm, site="local", hostname=vm)
    database.insert_workspace(workspace, f"/tmp/workspace{suffix}", vm, f"workspace-group{suffix}")
    database.insert_agent(agent, vm, f"agent-user{suffix}")
    database.insert_session(
        session,
        workspace,
        "template",
        SessionMode.AGENT,
        agent,
        socket_path=f"/tmp/socket{suffix}",
    )
    return vm, workspace, agent, session


def test_vm_backup_snapshot_projects_exact_owner_tree_overlays(db: Database) -> None:
    owner_tree = _create_owner_tree(db)
    other_tree = _create_owner_tree(db, "-other")
    kinds = ("vm", "workspace", "agent", "session")
    for kind, name in zip(kinds, owner_tree, strict=True):
        db.instance_state.put_desired_overlay(kind, name, _payload(f"included-{kind}"))
    for kind, name in zip(kinds, other_tree, strict=True):
        db.instance_state.put_desired_overlay(kind, name, _payload(f"excluded-{kind}"))

    *_, desired_overlays, applied_slices = db.snapshot_vm_backup_data(owner_tree[0])

    assert {(record.instance_kind, record.instance_name) for record in desired_overlays} == set(
        zip(kinds, owner_tree, strict=True)
    )
    assert applied_slices == ()
    assert {record.payload.value["value"] for record in desired_overlays} == {f"included-{kind}" for kind in kinds}


def test_vm_owner_tree_overlay_queries_decode_only_selected_rows(db: Database) -> None:
    owner_tree = _create_owner_tree(db)
    other_tree = _create_owner_tree(db, "-other")
    db.instance_state.put_desired_overlay("vm", owner_tree[0], _payload("included"))
    db.instance_state.put_desired_overlay("vm", other_tree[0], _payload("unrelated"))
    db._conn.execute(  # noqa: SLF001
        "UPDATE instance_records SET value_json = ? "
        "WHERE instance_kind = 'vm' AND instance_name = ? AND record_type = 'desired-overlay'",
        ('{"value": "unrelated"}', other_tree[0]),
    )
    db._conn.commit()  # noqa: SLF001

    assert db.instance_state.has_vm_owner_tree_desired_overlay(owner_tree[0]) is True
    records = db.instance_state.list_vm_owner_tree_desired_overlays(owner_tree[0])
    assert [(record.instance_kind, record.instance_name) for record in records] == [("vm", owner_tree[0])]
    *_, snapshot_records, applied_slices = db.snapshot_vm_backup_data(owner_tree[0])
    assert snapshot_records == records
    assert applied_slices == ()

    db._conn.execute(  # noqa: SLF001
        "UPDATE instance_records SET value_json = ? "
        "WHERE instance_kind = 'vm' AND instance_name = ? AND record_type = 'desired-overlay'",
        ('{"value": "included"}', owner_tree[0]),
    )
    db._conn.commit()  # noqa: SLF001

    with pytest.raises(StateError):
        db.snapshot_vm_backup_data(owner_tree[0])


def test_owner_and_aggregate_deletes_remove_all_owned_records(db: Database) -> None:
    vm, workspace, agent, session = _create_owner_tree(db)
    for kind, name in (("vm", vm), ("workspace", workspace), ("agent", agent), ("session", session)):
        db.instance_state.put_desired_overlay(kind, name, _payload(name))
    db.instance_state.replace_applied_slices(
        "vm",
        vm,
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: _payload("hardware")},
    )

    db.delete_vm(vm)
    for kind, name in (("vm", vm), ("workspace", workspace), ("agent", agent), ("session", session)):
        assert db.instance_state.get_desired_overlay(kind, name) is None
    assert db.instance_state.get_applied_slices("vm", vm) == ()

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


def test_standalone_desired_mutations_commit_before_returning(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    writer = Database(path)
    reader = Database(path, read_only=True)
    try:
        expected = writer.instance_state.put_desired_overlay("vm", "alpha", _payload("intent"))
        assert reader.instance_state.get_desired_overlay("vm", "alpha") == expected

        writer.instance_state.clear_desired_overlay("vm", "alpha")
        assert reader.instance_state.get_desired_overlay("vm", "alpha") is None
    finally:
        reader.close()
        writer.close()


def test_carriers_are_frozen() -> None:
    payload = _payload("value")
    desired = DesiredOverlayRecord("vm", "alpha", payload, "2026-08-23T12:00:00Z")
    applied = AppliedStateSlice(
        "vm",
        "alpha",
        AppliedStateKey.HARDWARE_PROVENANCE,
        payload,
        "vm-create",
        "2026-08-23T12:00:00Z",
    )
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
