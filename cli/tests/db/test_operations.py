"""Durable database operation-ownership contracts."""

from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from agentworks.db import (
    LATEST_VERSION,
    Database,
    OperationClaimState,
    OperationOwnership,
    OperationResourceKind,
    OperationScope,
    create_manual_backup,
    restore_backup,
    validate_restore_source,
)
from agentworks.errors import StateError
from tests.database_support import build_schema

pytestmark = pytest.mark.windows


def _scope(name: str = "vm-one") -> OperationScope:
    return OperationScope(OperationResourceKind.VM, name)


def _simultaneous_claim_worker(path: Path, start: Any, outcomes: Any) -> None:
    from agentworks.db import Database, OperationResourceKind, OperationScope
    from agentworks.errors import StateError

    database = Database(path)
    try:
        assert start.wait(timeout=10)
        try:
            ownership = database.operations.claim(
                OperationScope(OperationResourceKind.VM, "shared"),
                "vm-reinitialize",
            )
        except StateError:
            outcomes.put(("contended", None))
        else:
            outcomes.put(("claimed", ownership.operation_id))
    except Exception as error:  # pragma: no cover - returned for parent assertion
        outcomes.put(("error", f"{type(error).__name__}: {error}"))
    finally:
        database.close()


def test_fresh_schema_has_claim_and_lifecycle_obligation_shape(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()

    connection = sqlite3.connect(path)
    columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(operation_claims)")]
    foreign_keys = connection.execute("PRAGMA foreign_key_list(operation_claims)").fetchall()
    lifecycle_columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(lifecycle_obligations)")]
    lifecycle_foreign_keys = connection.execute("PRAGMA foreign_key_list(lifecycle_obligations)").fetchall()
    connection.close()

    assert columns == ["resource_kind", "resource_name", "operation_id"]
    assert foreign_keys == [(0, 0, "operation_owners", "operation_id", "operation_id", "NO ACTION", "CASCADE", "NONE")]
    assert lifecycle_columns == [
        "operation_id",
        "obligation_id",
        "obligation_kind",
        "state",
        "payload_version",
        "payload",
        "payload_revision",
        "registered_at",
        "updated_at",
    ]
    assert lifecycle_foreign_keys == [
        (0, 0, "operation_owners", "operation_id", "operation_id", "NO ACTION", "CASCADE", "NONE")
    ]


def test_v38_migration_preserves_existing_data_and_adds_empty_claim_store(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    build_schema(path, 38)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO settings (key, value) VALUES ('witness', 'preserved')")
    connection.commit()
    connection.close()

    database = Database(path)
    try:
        assert database.get_setting("witness") == "preserved"
        assert database.operations.inspect(_scope()) is None
    finally:
        database.close()

    connection = sqlite3.connect(path)
    assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone() == (LATEST_VERSION,)
    assert connection.execute("SELECT COUNT(*) FROM operation_claims").fetchone() == (0,)
    connection.close()


def test_claim_inspection_is_bounded_and_disjoint_resources_do_not_contend(db: Database) -> None:
    first = db.operations.claim(_scope("first"), "vm-reinitialize")
    second = db.operations.claim(_scope("second"), "vm-delete")

    first_claim = db.operations.inspect(first.scope)
    second_claim = db.operations.inspect(second.scope)
    assert first_claim is not None
    assert second_claim is not None
    assert first_claim.ownership == first
    assert first_claim.operation_kind == "vm-reinitialize"
    assert first_claim.state is OperationClaimState.RESERVED
    assert second_claim.ownership == second
    assert second_claim.operation_kind == "vm-delete"


def test_independent_connections_refuse_an_owned_scope(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = Database(path)
    second = Database(path)
    try:
        ownership = first.operations.claim(_scope(), "vm-reinitialize")
        with pytest.raises(StateError):
            second.operations.claim(_scope(), "vm-delete")

        assert second.operations.inspect(_scope()) is not None
        first.operations.abandon_reserved(ownership)
        replacement = second.operations.claim(_scope(), "vm-delete")
        assert replacement.operation_id != ownership.operation_id
    finally:
        second.close()
        first.close()


def test_simultaneous_process_claims_have_one_winner_and_rollback_the_loser(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    Database(path).close()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    outcomes = context.Queue()
    processes = [context.Process(target=_simultaneous_claim_worker, args=(path, start, outcomes)) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    try:
        observed = [outcomes.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=15)
        assert all(not process.is_alive() for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert sorted(outcome for outcome, _operation_id in observed) == ["claimed", "contended"]
    operation_id = next(operation_id for outcome, operation_id in observed if outcome == "claimed")
    database = Database(path)
    try:
        claim = database.operations.inspect(_scope("shared"))
        assert claim is not None
        assert claim.ownership.operation_id == operation_id
        assert claim.state is OperationClaimState.RESERVED
    finally:
        database.close()


def test_reserved_claim_can_be_abandoned_before_dispatch(db: Database) -> None:
    ownership = db.operations.claim(_scope(), "vm-reinitialize")
    with pytest.raises(StateError):
        db.operations.record_effects_resolved(ownership)
    with pytest.raises(StateError):
        db.operations.release_resolved(ownership)
    db.operations.abandon_reserved(ownership)
    assert db.operations.inspect(ownership.scope) is None


def test_lifecycle_obligation_states_survive_close_and_require_resolution(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    ownership = database.operations.claim(_scope(), "vm-reinitialize")
    database.close()

    reserved_database = Database(path)
    try:
        retained = reserved_database.operations.inspect(ownership.scope)
        assert retained is not None
        assert retained.ownership == ownership
        assert retained.state is OperationClaimState.RESERVED
        obligation = reserved_database.operations.register_lifecycle_obligation(
            ownership,
            "platform-hold",
            1,
            b"prepared",
        )
        possible = reserved_database.operations.mark_lifecycle_obligation_possible_effect(
            ownership,
            obligation.obligation_id,
        )
        assert possible.state.value == "possible-effect"
    finally:
        reserved_database.close()

    possible_database = Database(path)
    try:
        retained = possible_database.operations.inspect(ownership.scope)
        assert retained is not None
        assert retained.state is OperationClaimState.POSSIBLE_DISPATCH
        with pytest.raises(StateError):
            possible_database.operations.abandon_reserved(ownership)
        with pytest.raises(StateError):
            possible_database.operations.release_resolved(ownership)

        possible_database.operations.resolve_lifecycle_obligation(ownership, possible.obligation_id)
        possible_database.operations.seal_lifecycle_obligations(ownership)
        resolved = possible_database.operations.record_effects_resolved(ownership)
        assert resolved.state is OperationClaimState.RESOLVED
    finally:
        possible_database.close()

    resolved_database = Database(path)
    try:
        retained = resolved_database.operations.inspect(ownership.scope)
        assert retained is not None
        assert retained.state is OperationClaimState.RESOLVED
        resolved_database.operations.release_resolved(ownership)
        assert resolved_database.operations.inspect(ownership.scope) is None
    finally:
        resolved_database.close()


def test_stale_owner_cannot_change_or_delete_a_later_claim(db: Database) -> None:
    stale = db.operations.claim(_scope(), "vm-reinitialize")
    db.operations.abandon_reserved(stale)
    current = db.operations.claim(_scope(), "vm-delete")

    with pytest.raises(StateError):
        db.operations.register_lifecycle_obligation(stale, "platform-hold", 1, b"")
    with pytest.raises(StateError):
        db.operations.abandon_reserved(stale)

    claim = db.operations.inspect(current.scope)
    assert claim is not None
    assert claim.ownership == current
    assert claim.state is OperationClaimState.RESERVED


def test_claim_mutation_refuses_to_join_a_caller_transaction(db: Database) -> None:
    with db.transaction():
        db.set_setting("unrelated", "committed")
        with pytest.raises(StateError):
            db.operations.claim(_scope(), "vm-reinitialize")

    assert db.get_setting("unrelated") == "committed"
    assert db.operations.inspect(_scope()) is None


def test_operation_rollback_cannot_join_or_erase_an_ordinary_transaction(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    operation_started = Event()
    allow_rollback = Event()
    operation_finished = Event()
    worker_errors: list[BaseException] = []

    def roll_back_operation() -> None:
        repository = database.operations
        try:
            with repository._standalone_transaction():  # noqa: SLF001
                repository._connection.execute("INSERT INTO settings (key, value) VALUES ('operation', 'discarded')")  # noqa: SLF001
                operation_started.set()
                assert allow_rollback.wait(timeout=10)
                raise RuntimeError("discard operation state")
        except RuntimeError:
            pass
        except BaseException as error:  # pragma: no cover - reported below
            worker_errors.append(error)
        finally:
            operation_finished.set()

    try:
        with database.transaction():
            database._conn.execute("SELECT 1")  # noqa: SLF001
            worker = Thread(target=roll_back_operation)
            worker.start()
            assert operation_started.wait(timeout=10)
            allow_rollback.set()
            assert operation_finished.wait(timeout=10)
            worker.join(timeout=10)
            assert not worker.is_alive()
            assert not worker_errors
            assert database._conn.in_transaction  # noqa: SLF001
            database.set_setting("ordinary", "committed")

        assert database.get_setting("ordinary") == "committed"
        assert database.get_setting("operation") is None
    finally:
        database.close()


def test_database_close_closes_its_operation_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository = database.operations
    connection = repository._connection  # noqa: SLF001

    database.close()

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_read_only_operation_inspection_uses_a_read_only_connection(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    writer = Database(path)
    ownership = writer.operations.claim(_scope(), "vm-reinitialize")
    writer.close()

    reader = Database(path, read_only=True)
    try:
        claim = reader.operations.inspect(ownership.scope)
        assert claim is not None and claim.ownership == ownership
        with pytest.raises(StateError):
            reader.operations.abandon_reserved(ownership)
    finally:
        reader.close()


def test_claim_does_not_interfere_with_unrelated_database_writes(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    owner = Database(path)
    other = Database(path)
    try:
        ownership = owner.operations.claim(_scope(), "vm-reinitialize")
        other.set_setting("unrelated", "available")
        assert owner.get_setting("unrelated") == "available"
        assert other.operations.inspect(ownership.scope) is not None
    finally:
        other.close()
        owner.close()


def test_malformed_persisted_claim_is_not_treated_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    database = Database(path)
    ownership = database.operations.claim(_scope(), "vm-reinitialize")
    database.close()

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE operation_owners SET state = 'invented' WHERE operation_id = ?",
        (ownership.operation_id,),
    )
    connection.commit()
    connection.close()

    reopened = Database(path)
    try:
        with pytest.raises(StateError) as mutation_error:
            reopened.operations.abandon_reserved(ownership)
        assert mutation_error.value.entity_kind == "database"
        assert "invented" not in str(mutation_error.value)

        with pytest.raises(StateError) as raised:
            reopened.operations.inspect(ownership.scope)
        assert raised.value.entity_kind == "database"
        assert "invented" not in str(raised.value)
    finally:
        reopened.close()


def test_backup_and_restore_preserve_retained_operation_claim(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    database = Database(source)
    ownership = database.operations.claim(
        OperationScope(OperationResourceKind.PLATFORM_HOST, "builder.example"),
        "platform-provision",
    )
    obligation = database.operations.register_lifecycle_obligation(ownership, "platform-hold", 1, b"prepared")
    database.operations.mark_lifecycle_obligation_possible_effect(ownership, obligation.obligation_id)
    database.close()

    backup = create_manual_backup(source)
    assert validate_restore_source(backup) == LATEST_VERSION
    restored = tmp_path / "restored.db"
    restore_backup(backup, restored)

    restored_database = Database(restored)
    try:
        claim = restored_database.operations.inspect(ownership.scope)
        assert claim is not None
        assert claim.ownership == ownership
        assert claim.operation_kind == "platform-provision"
        assert claim.state is OperationClaimState.POSSIBLE_DISPATCH
    finally:
        restored_database.close()


def test_manually_reconstructed_stale_ownership_is_fenced(db: Database) -> None:
    current = db.operations.claim(_scope(), "vm-reinitialize")
    stale = OperationOwnership(current.scope, "0" * 32)

    with pytest.raises(StateError):
        db.operations.register_lifecycle_obligation(stale, "platform-hold", 1, b"")

    claim = db.operations.inspect(current.scope)
    assert claim is not None
    assert claim.ownership == current
