"""Core-owned lifetime custody for serial private foreground inline execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.errors import StateError, ValidationError
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_launcher import _validate_plan
from agentworks.execution._inline import (
    InlineCandidateResult,
    PreparedInlineCandidate,
    execute_inline_candidate,
    prepare_inline_candidate,
)
from agentworks.execution._managed_job_access import _explicit_managed_shell
from agentworks.execution._managed_operation_run import ManagedOperationRun
from agentworks.execution._managed_request_adapter import compose_managed_body
from agentworks.execution._managed_runs import (
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRepository,
    ManagedRunSpec,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution.carrier import Dispatch, ExitStatus
from agentworks.execution.models import JobRef
from agentworks.operations import LifecycleObligation, OperationAttempt, release_borrow_after_custody

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection, _NumericGuestBootstrap
    from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Carrier, Deadline
    from agentworks.execution.models import Command, Input, Output, Script
    from agentworks.operations import OperationBorrow, OperationOwner


@dataclass(frozen=True, slots=True, repr=False)
class OwnedInlineOutcome:
    """Captured inline facts and independent operation-ownership state.

    A completed call retains its candidate for its immediate caller. Custody
    records that can outlive the call omit it, avoiding retention of helper
    transcript bytes. Escaping control flow also exposes only a safe ownership
    fact through its exception cause.
    """

    candidate: InlineCandidateResult | None = None
    deadline_exceeded: bool = False
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class InlineExecutionControlFact(Exception):
    """Safe custody state attached to an escaping inline control flow."""

    def __init__(self, outcome: OwnedInlineOutcome) -> None:
        self.outcome = outcome
        super().__init__("private inline execution stopped with retained operation state")


class ManagedExecutionControlFact(Exception):
    """An exact run reference, not successful launch or aggregate completion."""

    def __init__(self, reference: JobRef) -> None:
        self.reference = reference
        super().__init__("Managed start retained its originating operation")


@dataclass(slots=True, repr=False)
class _ActiveInlineCall:
    carrier: Carrier
    borrow: OperationBorrow
    prepared: PreparedInlineCandidate
    operation: BorrowedFixedHelperCarrier
    installed: bool = False
    armed: bool = False
    bookkeeping_retained: bool = False
    candidate: InlineCandidateResult | None = None
    outcome: OwnedInlineOutcome | None = None


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedInlineExecution:
    """Captured non-payload custody for a call whose owner remains retained."""

    outcome: OwnedInlineOutcome


class ExecutionOperation:
    """Run serial inline candidates under one bounded lifetime obligation."""

    def __init__(
        self,
        owner: OperationOwner,
        target: ManagedTargetIdentity,
        *,
        bootstrap: _NumericGuestBootstrap | None = None,
        managed_repository: ManagedRunRepository | None = None,
        native_binding: NativeExecutionBinding | None = None,
        wsl2_route: WSL2OwnedOperation | None = None,
    ) -> None:
        """Bind inline calls to one exact owner scope and selected target."""
        scope = owner.ownership.scope
        if (
            type(target) is not ManagedTargetIdentity
            or target.kind.value != scope.resource_kind.value
            or target.name != scope.resource_name
        ):
            raise ValidationError("Execution operation target must match its owner scope")
        if bootstrap is not None and (
            target.kind is not ManagedTargetKind.VM or vm_guest_boot_id(bootstrap.guest) != target.boot_id
        ):
            raise ValidationError("Numeric execution bootstrap must match the selected VM boot")
        self._owner = owner
        self._target = target
        self._bootstrap = bootstrap
        self._managed_repository = managed_repository
        self._native_binding = native_binding
        self._wsl2_route = wsl2_route
        self._managed_runs: list[ManagedOperationRun] = []
        self._active_inline_calls: dict[int, _ActiveInlineCall] = {}
        self._unfinished_inline_executions: list[UnfinishedInlineExecution] = []
        self._admission_guard = Lock()
        self._finishing = False
        self._finished = False
        self._dispatch_id: str | None = None
        self._dispatch_obligation: LifecycleObligation | None = None

    @property
    def managed_runs(self) -> tuple[ManagedOperationRun, ...]:
        return tuple(self._managed_runs)

    def start_managed(
        self,
        carrier: Carrier,
        invocation: Command | Script,
        *,
        plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
        deadline: Deadline,
        input: Input,
        output: Output,
        env: Mapping[str, str] | None,
        cwd: str | None,
        sensitive: bool,
    ) -> JobRef:
        """Freeze a body, retain its run, then reserve and start exactly once."""
        with self._admission_guard:
            binding, repository, bootstrap = self._native_binding, self._managed_repository, self._bootstrap
            if self._finishing or self._finished:
                raise StateError("Execution operation is closing")
            with self._owner._guard:  # noqa: SLF001
                self._owner._require_dispatch_admission_locked()  # noqa: SLF001
            if binding is None or binding._new_managed_delivery is None or repository is None or bootstrap is None:
                raise StateError("Managed delivery is unavailable for this bound operation")
            if carrier is not binding.carrier or runtime_selection != binding.runtime_selection:
                raise ValidationError("Managed start requires the selected native binding")
            identity = ManagedRunIdentity(uuid4().hex)
            spec = ManagedRunSpec(
                self._target,
                _validate_plan(plan),
                _explicit_managed_shell(invocation),
                ManagedRunOwner(ManagedRunOwnerKind.OPERATION, self._owner.ownership.operation_id),
                ManagedRunLifetime.OPERATION,
            )
            body = compose_managed_body(
                invocation,
                input=input,
                output=output,
                env=env,
                cwd=cwd,
                sensitive=sensitive,
                identity=identity,
                spec=spec,
            )
            receipt = ManagedRunReceipt(identity, "agw-managed-" + identity.run_id + ".service", spec)
            run = ManagedOperationRun(
                repository,
                receipt,
                carrier,
                binding._new_managed_delivery(),
                owner=self._owner,
                start_obligation_id=uuid4().hex,
                keeper_obligation_id=uuid4().hex,
                target=self._target,
                guest=bootstrap.guest,
                root_plan=bootstrap.root_entry,
                runtime_selection=runtime_selection,
            )
            self._managed_runs.append(run)
            reference = JobRef(identity.run_id)
            try:
                route = self._wsl2_route
                run.start(
                    body,
                    deadline,
                    before_dispatch=(lambda: route.require_selected_route(deadline)) if route is not None else None,
                )
            except BaseException as control:
                fact = ManagedExecutionControlFact(reference)
                fact.__cause__ = control.__cause__
                raise control from fact
            if not run.acknowledged:
                raise StateError("Managed start was not acknowledged") from ManagedExecutionControlFact(reference)
            return reference

    @property
    def active_inline_calls(self) -> tuple[_ActiveInlineCall, ...]:
        """Return calls whose borrow has not yet been relinquished."""
        return tuple(self._active_inline_calls.values())

    @property
    def unfinished_inline_executions(self) -> tuple[UnfinishedInlineExecution, ...]:
        """Return custody records for attempts that cannot release ownership."""
        return tuple(self._unfinished_inline_executions)

    def finish(self) -> None:
        """Permanently stop inline admission and resolve only this lifetime row.

        A refused finish remains closed to new calls. Retrying repeats only
        exact resolution, including when its previous reply was lost.
        """
        with self._admission_guard:
            self._finishing = True
            if self._finished:
                return
            if self._active_inline_calls:
                self._retry_bookkeeping(finishing=True)
            if self._unfinished_inline_executions:
                raise StateError("Inline execution retains unsettled work")
            if self._dispatch_obligation is not None:
                self._dispatch_obligation.resolve()
            self._finished = True

    def retry_inline_bookkeeping(self) -> None:
        """Relinquish a failed call using exact bookkeeping, without dispatch.

        The retained call supplies its original borrow and candidate evidence.
        Unknown helper attempts remain recovery custody and cannot be settled.
        """
        with self._admission_guard:
            self._retry_bookkeeping(finishing=self._finishing)

    def _retry_bookkeeping(self, *, finishing: bool) -> None:
        for active in tuple(self._active_inline_calls.values()):
            if not active.bookkeeping_retained:
                raise StateError("Inline execution still has an active call")
            operation = active.operation
            candidate = active.candidate
            if candidate is not None and self._known_termination(candidate):
                # The recorded candidate proves termination independently of
                # whether the exact local settlement returned successfully.
                if not active.borrow.has_outstanding_attempt:
                    operation.outstanding_attempt = None
                else:
                    operation.settle(candidate.dispatch, candidate.carrier_completion)
                operation.coordination_uncertain = False
            elif not operation.pending_remote_effects and operation.outstanding_attempt is None:
                # begin_attempt can lose its reply before the wrapper receives
                # permission. The actual carrier was never entered in this case.
                with self._owner._guard:  # noqa: SLF001
                    self._owner._reconcile_transition_locked()  # noqa: SLF001
                    borrower = self._owner._active_borrow  # noqa: SLF001
                    if borrower is not active.borrow and (borrower is not None or not active.borrow._closed):  # noqa: SLF001
                        raise StateError("Inline call no longer owns its admission borrow")
                    attempt = self._owner._outstanding_attempt  # noqa: SLF001
                    if attempt is not None and (
                        not isinstance(attempt, OperationAttempt) or attempt._borrow is not active.borrow  # noqa: SLF001
                    ):
                        raise StateError("Inline call no longer owns its admission attempt")
                if attempt is not None:
                    attempt.settle()
                operation.coordination_uncertain = False
            elif operation.requires_owner_retention:
                raise StateError("Inline execution retains unknown helper custody")
            if not active.armed:
                if finishing:
                    if not active.installed and not self._borrow_closed(active):
                        self._recover_registration(active)
                    if not self._borrow_closed(active):
                        active.borrow.close()
                    self._active_inline_calls.pop(id(active))
                    continue
                self._admit(active)
            outcome = OwnedInlineOutcome(
                pending_remote_effects=operation.pending_remote_effects,
                coordination_uncertain=operation.coordination_uncertain,
                requires_owner_retention=operation.requires_owner_retention,
            )
            self._capture(active, outcome)

    def _recover_registration(self, active: _ActiveInlineCall) -> None:
        """Read an interrupted, never-dispatched registration during finish."""
        owner = self._owner
        borrow = active.borrow
        with owner._guard:  # noqa: SLF001
            owner._reconcile_transition_locked()  # noqa: SLF001
            if owner._active_borrow is not borrow or borrow._attempt_started:  # noqa: SLF001
                raise StateError("Inline registration no longer owns its unused borrow")
            rows = owner._repository.list_lifecycle_obligations(owner.ownership)  # noqa: SLF001
            row = next((row for row in rows if row.obligation_id == self._dispatch_id), None)
            if row is None:
                if self._dispatch_obligation is not None:
                    raise StateError("Inline lifetime obligation disappeared")
                return
            if (row.obligation_kind, row.payload_version, row.payload) != ("carrier-dispatch", 1, b""):
                raise StateError("Inline lifetime obligation identity conflicts")
            borrow._dispatch_obligation = row  # noqa: SLF001
            self._dispatch_obligation = LifecycleObligation(owner, row)
            active.installed = True

    @staticmethod
    def _known_termination(candidate: InlineCandidateResult) -> bool:
        return candidate.dispatch is Dispatch.NOT_SENT or (
            candidate.dispatch is Dispatch.SENT and candidate.carrier_completion == ExitStatus(code=0)
        )

    def _admit(self, active: _ActiveInlineCall) -> None:
        dispatch_id = self._dispatch_id
        assert dispatch_id is not None
        if not active.installed:
            self._dispatch_obligation = active.borrow.install_dispatch_obligation(
                dispatch_id, "carrier-dispatch", payload_version=1, payload=b""
            )
            active.installed = True
        if not active.armed:
            active.borrow.arm_dispatch_obligation()
            active.armed = True

    def run_inline(
        self,
        carrier: Carrier,
        request: Command | Script,
        *,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
        stdin: bytes = b"",
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        capture_limit: int | None = 4_096,
        sensitive: bool = False,
    ) -> OwnedInlineOutcome:
        """Prepare, dispatch and settle one inline candidate without replay."""
        prepared = prepare_inline_candidate(
            request,
            plan=plan,
            stdin=stdin,
            env=env,
            cwd=cwd,
            capture_limit=capture_limit,
            sensitive=sensitive,
            runtime_selection=runtime_selection,
            bootstrap=self._bootstrap,
        )
        if deadline.expired:
            raise ValidationError("Inline execution deadline expired during preparation")
        with self._admission_guard:
            if self._finishing or self._active_inline_calls or self._unfinished_inline_executions:
                raise StateError("Inline execution cannot admit new work")
            borrow = self._owner.borrow()
            try:
                operation = BorrowedFixedHelperCarrier(carrier, borrow)
                active = _ActiveInlineCall(carrier, borrow, prepared, operation)
                if self._dispatch_id is None:
                    self._dispatch_id = uuid4().hex
                self._active_inline_calls[id(active)] = active
            except BaseException:
                borrow.close()
                raise
        try:
            self._admit(active)
            candidate = execute_inline_candidate(operation, prepared, deadline=deadline)
            active.candidate = candidate
            operation.settle(candidate.dispatch, candidate.carrier_completion)
            outcome = self._outcome(active, operation, deadline, include_candidate=True)
        except BaseException as control:
            self._raise_control(control, active, operation, deadline)

        with self._admission_guard:
            try:
                self._capture(active, outcome)
            except BaseException:
                active.bookkeeping_retained = True
                raise
        return outcome

    def _outcome(
        self,
        active: _ActiveInlineCall,
        operation: BorrowedFixedHelperCarrier,
        deadline: Deadline,
        *,
        include_candidate: bool,
    ) -> OwnedInlineOutcome:
        return OwnedInlineOutcome(
            candidate=active.candidate if include_candidate else None,
            deadline_exceeded=deadline.expired,
            pending_remote_effects=operation.pending_remote_effects,
            coordination_uncertain=operation.coordination_uncertain,
            requires_owner_retention=operation.requires_owner_retention,
        )

    def _raise_control(
        self,
        control: BaseException,
        active: _ActiveInlineCall,
        operation: BorrowedFixedHelperCarrier,
        deadline: Deadline,
    ) -> None:
        with self._admission_guard:
            active.bookkeeping_retained = True
            try:
                outcome = self._outcome(active, operation, deadline, include_candidate=False)
                retryable = (active.candidate is not None and self._known_termination(active.candidate)) or (
                    operation.coordination_uncertain and not operation.pending_remote_effects
                )
                if active.armed and not retryable:
                    self._capture(active, outcome)
                else:
                    outcome = replace(outcome, coordination_uncertain=True, requires_owner_retention=True)
                    active.outcome = outcome
                fact = InlineExecutionControlFact(outcome)
            except BaseException:
                raise control from None
        raise control from fact

    def _capture(self, active: _ActiveInlineCall, outcome: OwnedInlineOutcome) -> None:
        active.outcome = outcome
        # Both handoffs are local core transitions. A reply can be lost after
        # the borrow relinquishes authority, so observe that exact local fact
        # before repeating a handoff. Never default-close the lifetime row.
        if not self._borrow_closed(active):
            release_borrow_after_custody(active.borrow, retain_effect=True)
        if outcome.requires_owner_retention:
            self._unfinished_inline_executions.append(UnfinishedInlineExecution(replace(outcome, candidate=None)))
        self._active_inline_calls.pop(id(active))

    def _borrow_closed(self, active: _ActiveInlineCall) -> bool:
        with self._owner._guard:  # noqa: SLF001
            return active.borrow._closed  # noqa: SLF001
