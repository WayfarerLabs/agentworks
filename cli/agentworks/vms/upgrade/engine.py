"""Small, fakeable coordinator for journaled package actions."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from .journal import AttemptOutcome, JournalState, UpgradeAction, UpgradePair


class ActionDisposition(StrEnum):
    """What inspection can prove about an active attempt."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    REPAIR_REQUIRED = "repair-required"


class ActionResult(Protocol):
    @property
    def disposition(self) -> ActionDisposition: ...

    @property
    def detail(self) -> str | None: ...


class UpgradeExecution(Protocol):
    """External systemd and package postcondition boundary."""

    def inspect(self, action: UpgradeAction, state: JournalState) -> ActionResult: ...

    def start(self, action: UpgradeAction, attempt_id: str) -> ActionResult: ...

    def wait(self, action: UpgradeAction, state: JournalState) -> ActionResult: ...


class UpgradeJournal(Protocol):
    """Atomic journal operations used by the coordinator."""

    def load(self, pair: UpgradePair) -> JournalState: ...

    def claim(self, pair: UpgradePair, action: UpgradeAction) -> JournalState: ...

    def complete(
        self,
        pair: UpgradePair,
        action: UpgradeAction,
        attempt_id: str,
    ) -> JournalState: ...

    def fail(
        self,
        pair: UpgradePair,
        action: UpgradeAction,
        attempt_id: str,
        detail: str,
        *,
        repair_required: bool,
    ) -> JournalState: ...

    def retry(self, pair: UpgradePair, attempt_id: str) -> JournalState: ...


class UpgradeActionError(Exception):
    """An action stopped without a proved postcondition."""

    def __init__(self, action: UpgradeAction, detail: str, *, repair_required: bool) -> None:
        super().__init__(detail)
        self.action = action
        self.detail = detail
        self.repair_required = repair_required


class UpgradeEngine:
    """Advance one named action without replaying a proved postcondition."""

    def __init__(self, journal: UpgradeJournal, execution: UpgradeExecution) -> None:
        self._journal = journal
        self._execution = execution

    def advance_action(self, pair: UpgradePair, action: UpgradeAction) -> JournalState:
        state = self._journal.load(pair)
        if state.active_action is not None:
            if state.active_action is not action:
                raise UpgradeActionError(action, "a different upgrade action is active", repair_required=True)
            inspected = self._execution.inspect(action, state)
            if inspected.disposition is ActionDisposition.SUCCEEDED:
                assert state.attempt_id is not None
                state = self._journal.complete(pair, action, state.attempt_id)
                return state
            if inspected.disposition is ActionDisposition.RUNNING:
                return self._finish_running(pair, action, state)
            if inspected.disposition is ActionDisposition.REPAIR_REQUIRED:
                detail = inspected.detail or "action inspection requires manual repair"
                if state.outcome is not AttemptOutcome.REPAIR_REQUIRED:
                    assert state.attempt_id is not None
                    self._journal.fail(
                        pair,
                        action,
                        state.attempt_id,
                        detail,
                        repair_required=True,
                    )
                raise UpgradeActionError(action, detail, repair_required=True)
            if state.outcome is AttemptOutcome.RUNNING:
                detail = inspected.detail or "action stopped before its postcondition was met"
                assert state.attempt_id is not None
                state = self._journal.fail(
                    pair,
                    action,
                    state.attempt_id,
                    detail,
                    repair_required=False,
                )
            if state.outcome is AttemptOutcome.REPAIR_REQUIRED:
                raise UpgradeActionError(action, state.failure or "manual repair required", repair_required=True)
            assert state.attempt_id is not None
            state = self._journal.retry(pair, state.attempt_id)
        else:
            if state.next_action is not action:
                raise UpgradeActionError(action, "journal is not ready for this action", repair_required=True)
            state = self._journal.claim(pair, action)

        assert state.attempt_id is not None
        started = self._execution.start(action, state.attempt_id)
        if started.disposition is ActionDisposition.RUNNING:
            return self._finish_running(pair, action, state)
        return self._finish_result(pair, action, state, started)

    def _finish_running(self, pair: UpgradePair, action: UpgradeAction, state: JournalState) -> JournalState:
        return self._finish_result(pair, action, state, self._execution.wait(action, state))

    def _finish_result(
        self,
        pair: UpgradePair,
        action: UpgradeAction,
        state: JournalState,
        result: ActionResult,
    ) -> JournalState:
        attempt_id = state.attempt_id
        if attempt_id is None:
            raise UpgradeActionError(action, "active action has no attempt identity", repair_required=True)
        if result.disposition is ActionDisposition.SUCCEEDED:
            state = self._journal.complete(pair, action, attempt_id)
            return state
        detail = result.detail or "action did not satisfy its postcondition"
        repair_required = result.disposition is ActionDisposition.REPAIR_REQUIRED
        self._journal.fail(
            pair,
            action,
            attempt_id,
            detail,
            repair_required=repair_required,
        )
        raise UpgradeActionError(action, detail, repair_required=repair_required)
