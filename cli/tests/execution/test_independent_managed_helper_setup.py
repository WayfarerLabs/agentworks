"""Unused independent helper setup failures preserve original control and custody."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType, TracebackType
from typing import NoReturn

import pytest

from agentworks.errors import StateError
from agentworks.execution import _managed_disposal_access as disposal
from agentworks.execution import _managed_job_access as job
from agentworks.execution import _managed_observe_access as observe
from agentworks.execution import _managed_stop_access as stop
from agentworks.execution._managed_runs import ManagedRunRepository
from agentworks.execution.models import Script, Shell
from agentworks.operations import OperationBorrow, OperationOwner

from .test_managed_job_access import RUN as START_RUN
from .test_managed_job_access import _call
from .test_managed_observation import ScriptedCarrier
from .test_managed_observe_access import RUN, _options
from .test_managed_result import _confirmed, _reply

pytestmark = pytest.mark.windows

SETUPS = (
    (observe, "BorrowedFixedHelperCarrier"),
    (stop, "BorrowedFixedHelperCarrier"),
    (stop, "ManagedActionCustody"),
    (disposal, "BorrowedFixedHelperCarrier"),
    (disposal, "ManagedActionCustody"),
    (job, "BorrowedFixedHelperCarrier"),
)


def _invoke(
    module: ModuleType, repository: ManagedRunRepository, owner: OperationOwner, carrier: ScriptedCarrier
) -> None:
    if module is observe:
        observe.observe_bound_managed_run(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
    elif module is stop:
        stop.stop_bound_managed_run(repository, RUN, obligation_id="e" * 32, **_options(owner, carrier))  # type: ignore[arg-type]
    elif module is disposal:
        disposal.dispose_bound_managed_run(repository, RUN, obligation_id="e" * 32, **_options(owner, carrier))  # type: ignore[arg-type]
    else:
        _call(repository, owner, carrier, invocation=Script("true", Shell.USER_DEFAULT))  # type: ignore[arg-type]


@pytest.mark.parametrize("module,constructor", SETUPS)
@pytest.mark.parametrize("failure_type", (MemoryError, KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize("close_fails", (False, True))
def test_unused_setup_retains_original_failure_and_exact_borrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    constructor: str,
    failure_type: type[BaseException],
    close_fails: bool,
) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(_reply)
    original_cause = OSError("prior cause")
    failure = failure_type("setup interrupted")
    failure.__cause__ = original_cause
    tracebacks: list[TracebackType] = []
    borrows: list[OperationBorrow] = []
    closes: list[OperationBorrow] = []
    registrations: list[object] = []
    original_borrow: Callable[[], OperationBorrow] = owner.borrow
    original_close = OperationBorrow.close
    original_register = OperationBorrow.install_dispatch_obligation

    def capture_borrow() -> OperationBorrow:
        borrow = original_borrow()
        borrows.append(borrow)
        return borrow

    def fail_allocation(*args: object, **kwargs: object) -> NoReturn:
        try:
            raise failure
        except BaseException:
            assert failure.__traceback__ is not None
            tracebacks.append(failure.__traceback__)
            raise

    def capture_close(borrow: OperationBorrow) -> None:
        closes.append(borrow)
        if close_fails:
            raise KeyboardInterrupt("unused borrow close interrupted")
        original_close(borrow)

    def forbid_registration(*args: object, **kwargs: object) -> NoReturn:
        registrations.append(args)
        raise AssertionError("setup must not register")

    try:
        before = repository.inspect(RUN)
        monkeypatch.setattr(owner, "borrow", capture_borrow)
        monkeypatch.setattr(OperationBorrow, "close", capture_close)
        monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", forbid_registration)
        monkeypatch.setattr(module, constructor, fail_allocation)
        with pytest.raises(failure_type) as raised:
            _invoke(module, repository, owner, carrier)
        assert raised.value is failure
        assert failure.__cause__ is original_cause
        current: TracebackType | None = raised.tb
        while current is not None and current is not tracebacks[0]:
            current = current.tb_next
        assert current is tracebacks[0]
        assert len(borrows) == 1
        assert closes == borrows
        assert carrier.calls == 0
        assert registrations == []
        assert owner.list_pending_lifecycle_obligations() == ()
        assert repository.inspect(RUN) == before
        assert repository.inspect(START_RUN) is None
        monkeypatch.setattr(OperationBorrow, "close", original_close)
        monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", original_register)
        if close_fails:
            assert owner._active_borrow is borrows[0]  # noqa: SLF001
            with pytest.raises(StateError):
                original_borrow()
            borrows[0].close()
        next_borrow = original_borrow()
        next_borrow.close()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()
