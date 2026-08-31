"""Shared/exclusive VM checkpoint-operation exclusion."""

from __future__ import annotations

import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from agentworks.db import Database
from agentworks.errors import StateError
from agentworks.vms.manager.operation_guard import (
    exclusive_vm_operation_guard,
    shared_vm_operation_guard,
)


def test_shared_holders_coexist_and_exclusive_waits_for_release(tmp_path) -> None:
    path = tmp_path / "state.db"
    first = Database(path)
    second = Database(path)
    entered = Barrier(3)
    release = Event()

    def _hold_shared(database: Database) -> None:
        with shared_vm_operation_guard(database, "box", operation="reader"):
            entered.wait(timeout=5)
            release.wait(timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            readers = [
                executor.submit(_hold_shared, first),
                executor.submit(_hold_shared, second),
            ]
            entered.wait(timeout=5)
            with (
                pytest.raises(StateError),
                exclusive_vm_operation_guard(
                    second,
                    "box",
                    operation="checkpoint",
                ),
            ):
                pass
            release.set()
            for reader in readers:
                reader.result()
        with exclusive_vm_operation_guard(second, "box", operation="checkpoint"):
            pass
    finally:
        second.close()
        first.close()


def test_exclusive_holder_may_reenter_shared_but_shared_cannot_upgrade(db: Database) -> None:
    with (
        exclusive_vm_operation_guard(db, "box", operation="checkpoint"),
        shared_vm_operation_guard(
            db,
            "box",
            operation="nested status",
        ),
    ):
        pass

    with (
        shared_vm_operation_guard(db, "box", operation="status"),
        pytest.raises(RuntimeError),
        exclusive_vm_operation_guard(db, "box", operation="checkpoint"),
    ):
        pass


def test_guard_file_is_private_regular_file(db: Database) -> None:
    with shared_vm_operation_guard(db, "box", operation="status"):
        paths = list(db.path.parent.glob("agentworks-vm-operation-*.lock"))

    assert len(paths) == 1
    mode = paths[0].stat().st_mode
    assert stat.S_ISREG(mode)
    assert stat.S_IMODE(mode) == 0o600


def test_guard_does_not_remap_sqlite_errors_from_the_command_body(db: Database) -> None:
    body_error = sqlite3.OperationalError("main database write failed")

    with (
        pytest.raises(sqlite3.OperationalError) as caught,
        exclusive_vm_operation_guard(
            db,
            "box",
            operation="checkpoint",
        ),
    ):
        raise body_error

    assert caught.value is body_error
