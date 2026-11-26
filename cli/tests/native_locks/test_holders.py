"""Holder records and the refusals that name them, crash release, and the older-release interlock."""

from __future__ import annotations

import errno
import json
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agentworks.native_locks import (
    LockMode,
    LockRequest,
    NativeLockBusyError,
    native_locks,
    path_entity,
    vm_entity,
)
from agentworks.native_locks import _records as records_module
from agentworks.native_locks import _sets as sets_module
from agentworks.native_locks._files import holders_directory
from agentworks.native_locks._os import try_lock as os_try_lock

from ._lock_support import (
    VM,
    fake_record,
    holder_process,
    labelled,
    old_release_process,
    record_payload,
    records,
    session,
    workspace,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows

EXCLUSIVE_W = LockRequest(exclusive=(workspace(),))
SHARED_W = LockRequest(shared=(workspace(),))


def _refusal(database: Path, request: LockRequest) -> NativeLockBusyError:
    with pytest.raises(NativeLockBusyError) as caught, native_locks(database, request):
        pass
    return caught.value


# -- Naming the holder ----------------------------------------------------


def test_the_earliest_conflicting_live_holder_is_named(database: Path) -> None:
    with labelled("workspace", "repair"), native_locks(database, SHARED_W):
        time.sleep(0.05)
        with labelled("session", "start"), native_locks(database, SHARED_W):
            error = _refusal(database, EXCLUSIVE_W)
    assert (error.entity_kind, error.entity_name, error.mode) == ("workspace", "w", LockMode.EXCLUSIVE)
    assert error.holder is not None
    assert (error.holder.command, error.holder.entity, error.holder.mode) == (
        ("agw", "workspace", "repair"),
        "workspace/w",
        LockMode.SHARED,
    )
    assert error.holder.held_for >= 0.05


def test_a_holder_is_named_with_its_age(database: Path) -> None:
    payload = record_payload("workspace/w", "exclusive", acquired=time.time() - 192, command=("agw", "vm", "reinit"))
    with fake_record(database, payload, live=True), native_locks(database, SHARED_W):
        error = _refusal(database, EXCLUSIVE_W)
    assert error.holder is not None
    assert error.holder.command == ("agw", "vm", "reinit")
    assert 190 <= error.holder.held_for < 300


def test_a_record_whose_lock_is_free_is_deleted(database: Path) -> None:
    payload = record_payload("workspace/w", "exclusive", acquired=time.time() - 60)
    with fake_record(database, payload, live=False) as stale, labelled("live"), native_locks(database, EXCLUSIVE_W):
        error = _refusal(database, EXCLUSIVE_W)
        assert not stale.exists()
        assert len(records(database)) == 1
    assert error.holder is not None
    assert error.holder.command == ("agw", "live")


def test_a_locked_record_that_does_not_parse_is_skipped(database: Path) -> None:
    with fake_record(database, b'{"pid": 1, "comm', live=True) as partial, native_locks(database, EXCLUSIVE_W):
        error = _refusal(database, EXCLUSIVE_W)
        assert partial.exists()
    assert error.holder is not None
    assert error.holder.command == ("agw",)


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(record_payload("workspace/w", "exclusive", acquired=float("nan"))).encode(),
        json.dumps(record_payload("workspace/w", "exclusive", acquired=float("-inf"))).encode(),
        b"[" * 100_000 + b"]" * 100_000,
        b" " * (records_module.RECORD_MAX_BYTES + 1),
    ],
    ids=["nan-acquired", "infinite-acquired", "deeply-nested", "oversized"],
)
def test_a_locked_record_a_reader_cannot_trust_is_skipped(database: Path, content: bytes) -> None:
    with fake_record(database, content, live=True), native_locks(database, EXCLUSIVE_W):
        error = _refusal(database, EXCLUSIVE_W)
    assert error.holder is not None
    assert error.holder.command == ("agw",)


def test_a_record_the_platform_will_not_lock_leaves_the_refusal_unnamed(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(fd: int, mode: LockMode, *, offset: int = 0) -> bool:
        raise OSError(errno.ENOLCK, "no locks available")

    with native_locks(database, EXCLUSIVE_W):
        monkeypatch.setattr(records_module, "try_lock", no_locks)
        error = _refusal(database, EXCLUSIVE_W)
    assert error.entity_name == "w" and error.holder is None


def test_records_that_do_not_conflict_are_ignored(database: Path) -> None:
    earlier = time.time() - 60
    shared_w = record_payload("workspace/w", "shared", acquired=earlier)
    other = record_payload("workspace/x", "exclusive", acquired=earlier)
    with (
        fake_record(database, shared_w, live=True, name="00000000000000a1"),
        fake_record(database, other, live=True, name="00000000000000a2"),
        labelled("live"),
        native_locks(database, EXCLUSIVE_W),
    ):
        error = _refusal(database, SHARED_W)
    assert error.holder is not None
    assert (error.holder.command, error.holder.mode) == (("agw", "live"), LockMode.EXCLUSIVE)


def test_a_refusal_during_a_holders_release_falls_back(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = sets_module._release
    during_release: list[NativeLockBusyError] = []

    def refuse_before_releasing(handles: list[object]) -> None:
        # The holder has retired its record and still holds its entities.
        monkeypatch.setattr(sets_module, "_release", original)
        during_release.append(_refusal(database, EXCLUSIVE_W))
        original(handles)  # type: ignore[arg-type]

    with native_locks(database, EXCLUSIVE_W):
        monkeypatch.setattr(sets_module, "_release", refuse_before_releasing)
    assert [(error.entity_name, error.holder) for error in during_release] == [("w", None)]


@pytest.mark.skipif(sys.platform == "win32", reason="on Windows a reader cannot remove a file its holder has open")
def test_a_record_removed_before_its_holder_locks_it_is_recreated(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = records_module._lock_record
    probes: list[int] = []

    def reader_first(fd: int, deadline: object) -> bool:
        if not probes:
            directory = holders_directory(database.resolve(), VM)
            probes.append(len(list(directory.glob("*.json"))))
            # The reader finds the new file unlocked and removes it as stale.
            records_module.find_holder(directory, workspace(), LockMode.EXCLUSIVE)
            assert list(directory.glob("*.json")) == []
        return original(fd, deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(records_module, "_lock_record", reader_first)
    with labelled("recreated"), native_locks(database, EXCLUSIVE_W):
        assert probes == [1]
        assert len(records(database)) == 1
        error = _refusal(database, EXCLUSIVE_W)
        assert len(records(database)) == 1
    assert error.holder is not None
    assert error.holder.command == ("agw", "recreated")


def test_a_refusal_at_a_path_names_the_resolved_path(database: Path) -> None:
    with labelled("workspace", "rehome"), native_locks(database, LockRequest(exclusive=(path_entity(VM, "/data/b"),))):
        error = _refusal(database, LockRequest(exclusive=(path_entity(VM, "/data/b/new"),)))
    assert (error.entity_kind, error.entity_name, error.mode) == ("path", "/data/b", LockMode.SHARED)
    assert error.holder is not None
    assert (error.holder.command, error.holder.entity) == (("agw", "workspace", "rehome"), "path/data/b")


def test_a_holder_whose_record_stays_busy_runs_without_one(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os_try_lock

    def record_lock_busy(fd: int, mode: LockMode, *, offset: int = 0) -> bool:
        if mode is LockMode.EXCLUSIVE and offset == records_module.RECORD_LOCK_OFFSET:
            return False
        return original(fd, mode, offset=offset)

    monkeypatch.setattr(records_module, "try_lock", record_lock_busy)
    monkeypatch.setattr(records_module, "RECORD_LOCK_WAIT_SECONDS", 0.05)
    with native_locks(database, EXCLUSIVE_W):
        assert records(database) == []
        error = _refusal(database, EXCLUSIVE_W)
    assert (error.entity_kind, error.entity_name, error.holder) == ("workspace", "w", None)


def test_a_record_names_the_set_and_its_holder(database: Path) -> None:
    with labelled("session", "stop"), native_locks(database, LockRequest(exclusive=(session(),))):
        (path,) = records(database)
        content = json.loads(path.read_bytes())
    assert set(content) == {"pid", "command", "acquired", "entities"}
    assert content["command"] == ["agw", "session", "stop"]
    assert content["entities"] == [
        {"key": f"vm/{VM}", "mode": "shared"},
        {"key": "workspace/w", "mode": "shared"},
        {"key": "account/a", "mode": "shared"},
        {"key": "session/s", "mode": "exclusive"},
    ]
    assert records(database) == []


# -- Crash release --------------------------------------------------------


def test_a_killed_holder_frees_every_entity_and_its_record_goes(database: Path) -> None:
    everything = LockRequest(exclusive=(vm_entity(VM), workspace(), session()))
    with holder_process(database, mode=LockMode.EXCLUSIVE, session_name="s") as process:
        assert len(records(database)) == 1
        process.kill()
        process.wait(timeout=15)
    # Windows may release a terminated process's locks after a short delay.
    deadline = time.monotonic() + 5
    while True:
        try:
            with native_locks(database, everything):
                break
        except NativeLockBusyError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    with native_locks(database, EXCLUSIVE_W):
        _refusal(database, EXCLUSIVE_W)
        assert len(records(database)) == 1


# -- An older agw ---------------------------------------------------------


@pytest.mark.parametrize(
    "request_",
    [LockRequest(shared=(vm_entity(VM),)), LockRequest(exclusive=(vm_entity(VM),)), EXCLUSIVE_W],
    ids=["vm-shared", "vm-exclusive", "below-the-vm"],
)
def test_an_older_holder_refuses_new_sets_without_a_name(database: Path, request_: LockRequest) -> None:
    with old_release_process(database) as process:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        error = _refusal(database, request_)
    assert (error.entity_kind, error.entity_name, error.holder) == ("vm", VM, None)


def test_a_new_shared_holder_refuses_the_older_algorithm(database: Path) -> None:
    with native_locks(database, EXCLUSIVE_W), old_release_process(database) as process:
        assert process.stdout is not None
        outcome, code = process.stdout.readline().split()
    assert outcome == "busy"
    assert int(code) in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
