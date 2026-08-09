"""Exact schema-version table and migration-history inspection tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from time import monotonic

import pytest


def _current_database(path: Path) -> None:
    from agentworks.db import Database

    database = Database(path)
    database.close()


@pytest.mark.parametrize(
    "statements",
    [
        (
            "DROP TABLE schema_version",
            "CREATE TABLE schema_version (version INTEGER NOT NULL, recorded_at TEXT NOT NULL)",
            "INSERT INTO schema_version VALUES (1, 'operator-private')",
        ),
        (
            "DROP TABLE schema_version",
            "CREATE TABLE schema_version (version INTEGER NOT NULL CHECK (version > 0), "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))",
            "INSERT INTO schema_version (version) VALUES (1)",
        ),
        ("UPDATE schema_version SET version = -2 WHERE version = 2",),
        ("DELETE FROM schema_version WHERE version = 2",),
        ("UPDATE schema_version SET version = 2 WHERE version = 3",),
        ("UPDATE schema_version SET version = X'6f70657261746f722d70726976617465' WHERE version = 2",),
        ("UPDATE schema_version SET version = 2.5 WHERE version = 2",),
    ],
    ids=["wrong-column", "wrong-constraint", "negative", "gap", "duplicate", "blob", "float"],
)
def test_snapshot_rejects_malformed_schema_history(
    tmp_path: Path,
    statements: tuple[str, ...],
) -> None:
    from agentworks.db import Database
    from agentworks.errors import StateError

    db_path = tmp_path / "operator-private-schema.db"
    _current_database(db_path)
    connection = sqlite3.connect(db_path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StateError) as raised, Database.inspection_snapshot(db_path):
        pytest.fail("malformed migration history must not yield a snapshot")
    assert str(raised.value) == "state database inspection snapshot could not be created"
    assert "operator-private" not in str(raised.value)


def test_snapshot_accepts_absent_schema_table_as_legacy_v0(tmp_path: Path) -> None:
    from agentworks.db import LATEST_VERSION, Database

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE legacy_state (value TEXT NOT NULL)")
    connection.commit()
    connection.close()

    with Database.inspection_snapshot(db_path) as snapshot:
        assert snapshot == (True, 0, LATEST_VERSION, None)


@pytest.mark.parametrize("versions", [(), (0,), (0, 1, 2, 3), (1, 2, 3)])
def test_snapshot_accepts_maintained_legacy_schema_histories(
    tmp_path: Path,
    versions: tuple[int, ...],
) -> None:
    from agentworks.db import LATEST_VERSION, Database

    db_path = tmp_path / "legacy-version-table.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.executemany("INSERT INTO schema_version VALUES (?)", ((version,) for version in versions))
    connection.commit()
    connection.close()

    expected = max(versions, default=0)
    with Database.inspection_snapshot(db_path) as snapshot:
        assert snapshot == (True, expected, LATEST_VERSION, None)


def test_snapshot_accepts_empty_canonical_schema_table_as_v0(tmp_path: Path) -> None:
    from agentworks.db import LATEST_VERSION, Database

    db_path = tmp_path / "empty-canonical.db"
    _current_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM schema_version")
    connection.commit()
    connection.close()

    with Database.inspection_snapshot(db_path) as snapshot:
        assert snapshot == (True, 0, LATEST_VERSION, None)


def test_snapshot_accepts_exact_current_and_contiguous_stale_histories(tmp_path: Path) -> None:
    from agentworks.db import LATEST_VERSION, Database

    current_path = tmp_path / "current.db"
    _current_database(current_path)
    with Database.inspection_snapshot(current_path) as (exists, current, latest, database):
        assert exists and current == latest == LATEST_VERSION
        assert database is not None

    stale_path = tmp_path / "stale.db"
    _current_database(stale_path)
    connection = sqlite3.connect(stale_path)
    connection.execute("DELETE FROM schema_version WHERE version > 3")
    connection.commit()
    connection.close()
    with Database.inspection_snapshot(stale_path) as snapshot:
        assert snapshot == (True, 3, LATEST_VERSION, None)


def test_schema_history_validator_is_shared_by_read_only_callers(tmp_path: Path) -> None:
    from agentworks.db import Database
    from agentworks.errors import StateError

    db_path = tmp_path / "malformed-read-only.db"
    _current_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM schema_version WHERE version = 2")
    connection.commit()
    connection.close()

    with pytest.raises(StateError):
        Database.check_schema(db_path)
    with pytest.raises(StateError, match="state database is unavailable or malformed"):
        Database(db_path, read_only=True)


def test_snapshot_rejects_maximum_integer_version_without_expanding_a_range(tmp_path: Path) -> None:
    from agentworks.db import Database
    from agentworks.errors import StateError

    db_path = tmp_path / "operator-private-huge-version.db"
    _current_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM schema_version")
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (2**63 - 1,))
    connection.commit()
    connection.close()

    started = monotonic()
    with pytest.raises(StateError) as raised, Database.inspection_snapshot(db_path):
        pytest.fail("a huge noncontiguous version must not yield a snapshot")
    assert monotonic() - started < 10
    assert str(raised.value) == "state database inspection snapshot could not be created"
    assert "operator-private" not in str(raised.value)


@pytest.mark.parametrize("malformation", ["duplicate", "gap"])
def test_snapshot_rejects_large_malformed_history_with_bounded_aggregates(
    tmp_path: Path,
    malformation: str,
) -> None:
    from agentworks.db.inspection import _schema_version_from_history
    from agentworks.errors import StateError

    db_path = tmp_path / f"operator-private-large-{malformation}.db"
    _current_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM schema_version")
    if malformation == "duplicate":
        versions = ((1,) for _ in range(50_000))
    else:
        versions = ((version,) for version in range(1, 50_001) if version != 25_000)
    connection.executemany("INSERT INTO schema_version (version) VALUES (?)", versions)
    connection.commit()
    traced: list[str] = []
    connection.set_trace_callback(traced.append)

    started = monotonic()
    try:
        with pytest.raises(StateError, match="^state database inspection snapshot could not be created$"):
            _schema_version_from_history(connection)
    finally:
        connection.close()
    assert monotonic() - started < 10
    history_queries = [statement for statement in traced if "FROM schema_version" in statement]
    assert len(history_queries) == 1
    assert "COUNT(DISTINCT version)" in history_queries[0]
    assert "ORDER BY rowid" not in history_queries[0]
