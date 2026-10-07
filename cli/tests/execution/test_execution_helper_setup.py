"""Unused helper borrows settle before any execution or keeper drain."""

from __future__ import annotations

from typing import Any, NoReturn
from uuid import uuid4

import pytest

from agentworks.execution import _execution_operation as operation_module
from agentworks.execution._execution_operation import ManagedExecutionControlFact
from agentworks.execution.carrier import Deadline
from agentworks.execution.models import Command, Lifetime, Output
from agentworks.execution.profiles import Protection

from .test_managed_default_shell import lookup as lookup
from .test_managed_default_shell import start
from .test_managed_execution_access import bound as bound
from .test_managed_execution_access import view as view

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("entry", ["default-shell", "inline", "observe", "stop"])
@pytest.mark.parametrize("point", ["carrier", "active", "dispatch-id", "tracking"])
def test_setup_allocation_failure_releases_unused_borrow(view, lookup, monkeypatch, entry, point):
    database, workflow, access, main, keeper = view
    operation = workflow.views.execution_operation
    drains = []
    if entry in {"observe", "stop"}:
        reference = access.start(
            Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
        )
        (run,) = operation.managed_runs
        original_drain = run.keeper.drain

        def drain(deadline):
            drains.append(deadline)
            return original_drain(deadline)

        monkeypatch.setattr(run.keeper, "drain", drain)
    cause = ValueError("original allocation cause")
    control = MemoryError()

    def unavailable(*args: Any, **kwargs: Any) -> NoReturn:
        raise control from cause

    with monkeypatch.context() as patch:
        if point in {"carrier", "active"}:
            patch.setattr(
                operation_module,
                "BorrowedFixedHelperCarrier" if point == "carrier" else "_ActiveHelperCall",
                unavailable,
            )
        elif point == "dispatch-id":
            calls = 0

            def allocate():
                nonlocal calls
                calls += 1
                if calls == (2 if entry == "default-shell" else 1):
                    unavailable()
                return uuid4()

            patch.setattr(operation_module, "uuid4", allocate)
        else:

            class UnavailableTracking(dict):
                def __setitem__(self, key, value):
                    unavailable()

            patch.setattr(operation, "_active_inline_calls", UnavailableTracking())
        with pytest.raises(MemoryError) as caught:
            if entry == "default-shell":
                start(access)
            elif entry == "inline":
                access.run(Command(["/bin/true"]), profile=Protection.DIRECT, output=Output.discard())
            else:
                getattr(access, entry)(reference)
        assert caught.value is control
        retained_cause = control.__cause__
        if entry in {"observe", "stop"}:
            assert isinstance(retained_cause, ManagedExecutionControlFact)
            assert retained_cause.reference == reference
            retained_cause = retained_cause.__cause__
        assert retained_cause is cause
        assert workflow.owner._active_borrow is None and workflow.owner._outstanding_attempt is None
        assert not operation.active_inline_calls and lookup[0].calls == 0
        assert main.observe.calls == main.stop.calls == keeper.stop.calls == 0 and not drains
        expected_runs = 1 if entry in {"observe", "stop"} else 0
        assert main.start.calls == len(operation.managed_runs) == expected_runs
        assert database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == expected_runs
    next_borrow = workflow.owner.borrow()
    next_borrow.close()
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert database.operations.inspect(workflow.owner.ownership.scope) is None
