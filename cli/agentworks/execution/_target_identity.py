"""Owned preparation of private ordinary and optional elevated identity plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from agentworks.errors import ValidationError
from agentworks.execution._account import (
    AccountObservationState,
    AccountResolutionResult,
    resolve_account,
)
from agentworks.execution._account_protocol import AccountRequest, AccountRequestError, encode_account_request
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteState,
    RuntimeSelection,
)
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.operations import OperationOwner, release_borrow_after_custody

if TYPE_CHECKING:
    from agentworks.execution._helper_identity import IdentityExpectation
    from agentworks.execution.carrier import Carrier

_VALIDATION_NONCE = "0" * 32


class TargetIdentityStatus(StrEnum):
    PREPARED = "prepared"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class TargetIdentityFailure(StrEnum):
    DEADLINE = "deadline"
    DISPATCH = "dispatch"
    TERMINATION = "termination"
    RUNTIME_PREREQUISITE = "runtime_prerequisite"
    ACCOUNT = "account"
    OBSERVATION = "observation"
    IDENTITY_PATH = "identity_path"


@dataclass(frozen=True, slots=True, repr=False)
class TargetIdentityPreparation:
    """Closed preparation facts without account names or provider diagnostics."""

    status: TargetIdentityStatus
    ordinary_plan: IdentityPlan | None
    elevated_plan: IdentityPlan | None
    delivery_result: AccountResolutionResult | None
    workload_result: AccountResolutionResult | None
    root_result: AccountResolutionResult | None
    failure: TargetIdentityFailure | None = None
    deadline_exceeded: bool = False
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class TargetIdentityControlFact(Exception):
    """Safe bounded preparation state attached to escaping control flow."""

    def __init__(self, preparation: TargetIdentityPreparation) -> None:
        self.preparation = preparation
        super().__init__("private target identity preparation stopped with retained operation state")


@dataclass(slots=True, repr=False)
class _State:
    operation: BorrowedFixedHelperCarrier
    ordinary_plan: IdentityPlan | None = None
    elevated_plan: IdentityPlan | None = None
    delivery_result: AccountResolutionResult | None = None
    workload_result: AccountResolutionResult | None = None
    root_result: AccountResolutionResult | None = None
    failure: TargetIdentityFailure | None = None
    deadline_exceeded: bool = False

    def fail(self, failure: TargetIdentityFailure) -> None:
        if self.failure is None:
            self.failure = failure

    def finish(self) -> TargetIdentityPreparation:
        pending = self.operation.pending_remote_effects
        coordination = self.operation.coordination_uncertain
        retain = self.operation.requires_owner_retention
        if self.ordinary_plan is not None and self.failure is None and not retain:
            status = TargetIdentityStatus.PREPARED
        elif retain:
            status = TargetIdentityStatus.UNCERTAIN
        else:
            status = TargetIdentityStatus.FAILED
        return TargetIdentityPreparation(
            status,
            self.ordinary_plan,
            self.elevated_plan,
            self.delivery_result,
            self.workload_result,
            self.root_result,
            self.failure,
            self.deadline_exceeded,
            pending,
            coordination,
            retain,
        )


class _Composer:
    def __init__(
        self,
        operation: BorrowedFixedHelperCarrier,
        *,
        delivery_account: str,
        workload_account: str,
        include_elevated: bool,
        runtime_selection: RuntimeSelection,
        deadline: Deadline,
        state: _State,
    ) -> None:
        self._operation = operation
        self._delivery_account = delivery_account
        self._workload_account = workload_account
        self._include_elevated = include_elevated
        self._runtime_selection = runtime_selection
        self._deadline = deadline
        self._state = state
        self._results: dict[str, tuple[AccountResolutionResult, IdentityExpectation]] = {}

    def run(self) -> TargetIdentityPreparation:
        delivery = self._resolve("delivery", self._delivery_account)
        if delivery is None:
            return self._state.finish()
        workload = self._resolve("workload", self._workload_account)
        if workload is None:
            return self._state.finish()
        if self._expired():
            return self._state.finish()

        self._state.ordinary_plan = self._ordinary_plan(delivery, workload)
        if self._state.ordinary_plan is None:
            self._expired()
            return self._state.finish()
        if self._include_elevated:
            if delivery.euid == 0:
                self._state.elevated_plan = IdentityPlan(delivery, IdentityMode.DIRECT)
            else:
                root = self._resolve("root", "root")
                if root is not None and root.euid == 0:
                    self._state.elevated_plan = IdentityPlan(root, IdentityMode.SUDO_ROOT)
                elif root is not None:
                    self._state.fail(TargetIdentityFailure.IDENTITY_PATH)

        if self._expired():
            self._state.ordinary_plan = None
            self._state.elevated_plan = None
        return self._state.finish()

    def _ordinary_plan(
        self,
        delivery: IdentityExpectation,
        workload: IdentityExpectation,
    ) -> IdentityPlan | None:
        if delivery == workload:
            return IdentityPlan(workload, IdentityMode.DIRECT)
        if delivery.euid == 0 and workload.euid != 0:
            return IdentityPlan(workload, IdentityMode.DEMOTE)
        self._state.fail(TargetIdentityFailure.IDENTITY_PATH)
        return None

    def _resolve(
        self,
        role: Literal["delivery", "workload", "root"],
        account: str,
    ) -> IdentityExpectation | None:
        if self._expired():
            return None
        cached = self._results.get(account)
        if cached is not None:
            result, identity = cached
            self._record(role, result)
            return identity
        result = resolve_account(
            self._operation,
            account,
            self._deadline,
            self._runtime_selection,
        )
        self._record(role, result)
        normal = self._operation.settle(result.dispatch, result.carrier_completion)
        if not normal:
            self._state.fail(
                TargetIdentityFailure.DISPATCH
                if result.dispatch is Dispatch.NOT_SENT
                else TargetIdentityFailure.TERMINATION
            )
            self._expired()
            return None
        if self._expired():
            return None
        if result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY:
            self._state.fail(TargetIdentityFailure.RUNTIME_PREREQUISITE)
            return None
        observation = result.observation
        if (
            observation is None
            or observation.state is not AccountObservationState.RESOLVED
            or observation.identity is None
        ):
            self._state.fail(
                TargetIdentityFailure.ACCOUNT
                if observation is not None and observation.state is AccountObservationState.REFUSED
                else TargetIdentityFailure.OBSERVATION
            )
            return None
        self._results[account] = (result, observation.identity)
        return observation.identity

    def _record(
        self,
        role: Literal["delivery", "workload", "root"],
        result: AccountResolutionResult,
    ) -> None:
        if role == "delivery":
            self._state.delivery_result = result
        elif role == "workload":
            self._state.workload_result = result
        else:
            self._state.root_result = result

    def _expired(self) -> bool:
        if not self._deadline.expired:
            return False
        self._state.deadline_exceeded = True
        self._state.fail(TargetIdentityFailure.DEADLINE)
        return True


def prepare_target_identity(
    carrier: Carrier,
    *,
    delivery_account: str,
    workload_account: str,
    include_elevated: bool,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
) -> TargetIdentityPreparation:
    """Prepare ordinary and explicitly included elevated identity under one borrow."""
    _validate_inputs(
        delivery_account=delivery_account,
        workload_account=workload_account,
        include_elevated=include_elevated,
        runtime_selection=runtime_selection,
        deadline=deadline,
        owner=owner,
    )
    if deadline.expired:
        return TargetIdentityPreparation(
            TargetIdentityStatus.FAILED,
            None,
            None,
            None,
            None,
            None,
            TargetIdentityFailure.DEADLINE,
            deadline_exceeded=True,
        )

    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    state = _State(operation)
    composer = _Composer(
        operation,
        delivery_account=delivery_account,
        workload_account=workload_account,
        include_elevated=include_elevated,
        runtime_selection=runtime_selection,
        deadline=deadline,
        state=state,
    )
    try:
        try:
            return composer.run()
        except BaseException as control:
            state.deadline_exceeded = state.deadline_exceeded or deadline.expired
            if state.deadline_exceeded:
                state.fail(TargetIdentityFailure.DEADLINE)
            raise control from TargetIdentityControlFact(state.finish())
    finally:
        release_borrow_after_custody(borrow)


def _validate_inputs(
    *,
    delivery_account: str,
    workload_account: str,
    include_elevated: bool,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
) -> None:
    for account in (delivery_account, workload_account):
        valid = True
        try:
            encode_account_request(AccountRequest(_VALIDATION_NONCE, account))
        except AccountRequestError:
            valid = False
        if not valid:
            raise ValidationError("Target identity preparation requires valid account names")
    if type(include_elevated) is not bool:
        raise ValidationError("Target identity preparation requires an explicit elevation preparation choice")
    if type(runtime_selection) is not RuntimeSelection:
        raise ValidationError("Target identity preparation requires an explicit runtime selection")
    if type(deadline) is not Deadline:
        raise ValidationError("Target identity preparation requires one deadline")
    if not isinstance(owner, OperationOwner):
        raise ValidationError("Target identity preparation requires an operation owner")
