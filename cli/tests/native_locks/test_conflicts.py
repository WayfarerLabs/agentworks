"""Which lock sets refuse which: the mode matrix, the entity tree, and the fixed order."""

from __future__ import annotations

import contextlib
import threading
from contextlib import ExitStack
from typing import TYPE_CHECKING

import pytest

from agentworks.native_locks import (
    LockMode,
    LockRequest,
    NativeLockBusyError,
    native_locks,
    vm_entity,
)
from agentworks.native_locks import _sets as sets_module
from agentworks.native_locks._os import try_lock as os_try_lock

from ._lock_support import VM, account, holder_process, request_for, session, workspace

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.windows

MODE_PAIRS = [
    (LockMode.SHARED, LockMode.SHARED, False),
    (LockMode.SHARED, LockMode.EXCLUSIVE, True),
    (LockMode.EXCLUSIVE, LockMode.SHARED, True),
    (LockMode.EXCLUSIVE, LockMode.EXCLUSIVE, True),
]


def _refusal(database: Path, request: LockRequest) -> NativeLockBusyError | None:
    try:
        with native_locks(database, request):
            return None
    except NativeLockBusyError as error:
        return error


@pytest.mark.parametrize(("held", "asked", "refused"), MODE_PAIRS)
def test_mode_matrix_within_one_process(database: Path, held: LockMode, asked: LockMode, refused: bool) -> None:
    with native_locks(database, request_for(workspace(), held)):
        error = _refusal(database, request_for(workspace(), asked))
    assert (error is not None) is refused
    if error is not None:
        assert (error.entity_kind, error.entity_name, error.mode) == ("workspace", "w", asked)


@pytest.mark.parametrize(("held", "asked", "refused"), MODE_PAIRS)
def test_mode_matrix_across_processes(database: Path, held: LockMode, asked: LockMode, refused: bool) -> None:
    with holder_process(database, mode=held, label=("agw", "workspace", "rehome")):
        error = _refusal(database, request_for(workspace(), asked))
    assert (error is not None) is refused
    if error is not None:
        assert (error.entity_kind, error.entity_name, error.mode) == ("workspace", "w", asked)
        assert error.holder is not None
        assert (error.holder.command, error.holder.entity, error.holder.mode) == (
            ("agw", "workspace", "rehome"),
            "workspace/w",
            held,
        )


def test_session_change_and_workspace_change_refuse_each_other(database: Path) -> None:
    session_change = LockRequest(exclusive=(session(),))
    workspace_change = LockRequest(exclusive=(workspace(),))
    with native_locks(database, session_change):
        error = _refusal(database, workspace_change)
    assert error is not None
    assert (error.entity_kind, error.entity_name, error.mode) == ("workspace", "w", LockMode.EXCLUSIVE)
    with native_locks(database, workspace_change):
        error = _refusal(database, session_change)
    assert error is not None
    assert (error.entity_kind, error.entity_name, error.mode) == ("workspace", "w", LockMode.SHARED)


def test_session_change_and_vm_exclusive_refuse_each_other(database: Path) -> None:
    session_change = LockRequest(exclusive=(session(),))
    vm_wide = LockRequest(exclusive=(vm_entity(VM),))
    with native_locks(database, session_change):
        error = _refusal(database, vm_wide)
    assert error is not None
    assert (error.entity_kind, error.entity_name) == ("vm", VM)
    with native_locks(database, vm_wide):
        error = _refusal(database, session_change)
    assert error is not None
    assert (error.entity_kind, error.entity_name, error.mode) == ("vm", VM, LockMode.SHARED)


def test_unrelated_sessions_coexist(database: Path) -> None:
    with ExitStack() as stack:
        stack.enter_context(native_locks(database, LockRequest(exclusive=(session("s1", workspace_name="w1"),))))
        stack.enter_context(
            native_locks(database, LockRequest(exclusive=(session("s2", workspace_name="w2", account_name="b"),)))
        )
        # A session sharing only its workspace with another still proceeds.
        stack.enter_context(native_locks(database, LockRequest(exclusive=(session("s3", workspace_name="w1"),))))


def test_workspace_delete_and_agent_delete_on_a_shared_session_refuse_each_other(database: Path) -> None:
    workspace_delete = LockRequest(exclusive=(workspace(), session()))
    agent_delete = LockRequest(exclusive=(account(), session()))
    # Each holds the other's exclusive parent shared, and workspaces sort before accounts.
    with native_locks(database, workspace_delete):
        error = _refusal(database, agent_delete)
    assert error is not None
    assert (error.entity_kind, error.entity_name, error.mode) == ("workspace", "w", LockMode.SHARED)
    with native_locks(database, agent_delete):
        error = _refusal(database, workspace_delete)
    assert error is not None
    assert (error.entity_kind, error.entity_name, error.mode) == ("workspace", "w", LockMode.EXCLUSIVE)


def test_two_contending_acquisitions_never_both_fail(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two requests naming the same entities in opposite orders, run in lockstep, one lock attempt at a time."""
    original = os_try_lock
    step = threading.Barrier(2)

    def lockstep_try_lock(fd: int, mode: LockMode, *, offset: int = 0) -> bool:
        locked = original(fd, mode, offset=offset)
        # Each thread waits for the other's attempt; once one thread is done, the barrier breaks and the other runs on.
        with contextlib.suppress(threading.BrokenBarrierError):
            step.wait(timeout=0.5)
        return locked

    monkeypatch.setattr(sets_module, "try_lock", lockstep_try_lock)
    outcomes: list[bool] = []

    def contend(request: LockRequest) -> None:
        try:
            with native_locks(database, request):
                pass
        except NativeLockBusyError:
            outcomes.append(False)
        else:
            outcomes.append(True)

    requests = (
        LockRequest(exclusive=(workspace("x"), workspace("y"))),
        LockRequest(exclusive=(workspace("y"), workspace("x"))),
    )
    threads = [threading.Thread(target=contend, args=(request,)) for request in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [False, True]
