"""Core-owned lifetime custody for serial private ordinary execution helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.db import LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.execution._direct_input_operation import note_input_activation_closed, run_direct_input
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_launcher import _validate_plan
from agentworks.execution._inline import (
    InlineCandidateResult,
    PreparedInlineCandidate,
    execute_inline_candidate,
)
from agentworks.execution._managed_bound_run import ManagedDeadlineExpired, preflight_bound_run
from agentworks.execution._managed_disposal_access import (
    MANAGED_DISPOSAL_OBLIGATION_KIND,
    MANAGED_DISPOSAL_PAYLOAD_VERSION,
    ManagedDisposalControlFact,
    ManagedDisposalOutcome,
    dispose_bound_managed_run,
    encode_managed_disposal_obligation,
)
from agentworks.execution._managed_disposal_exchange import DisposalCandidate, DisposalState
from agentworks.execution._managed_job_access import (
    ManagedJobShellFact,
    ManagedJobShellRefusal,
    _explicit_managed_shell,
)
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_live_exchange import (
    ManagedLiveCandidate,
    PreparedManagedLive,
    execute_managed_live,
    prepare_managed_live,
)
from agentworks.execution._managed_observation_exchange import (
    ManagedObservationCandidate,
    PreparedManagedRead,
    execute_managed_read,
    prepare_managed_read,
)
from agentworks.execution._managed_observation_protocol import ManagedOperation
from agentworks.execution._managed_observe_access import (
    ManagedObserveControlFact,
    ManagedObserveOutcome,
    ManagedReadOutputOutcome,
    _without_output,
    observe_bound_managed_run,
    read_bound_managed_output,
)
from agentworks.execution._managed_operation_run import ManagedOperationRun
from agentworks.execution._managed_request_adapter import compose_managed_body
from agentworks.execution._managed_resource_disposal import (
    ManagedDisposalBinding,
    capture_disposal_call,
    dispose_resource_job,
    settle_unused_disposal_call,
)
from agentworks.execution._managed_resource_start import (
    ManagedResourceStartBinding,
    ResourceStartAcknowledgement,
    capture_resource_start,
    start_resource_job,
)
from agentworks.execution._managed_resource_stop import stop_resource_job
from agentworks.execution._managed_result import ManagedResultOutcome, wait_bound_managed_result
from agentworks.execution._managed_runs import (
    ManagedOutputMode,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRepository,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution._managed_start_operation import ManagedStartControlFact, ManagedStartOutcome
from agentworks.execution._managed_stop_exchange import ManagedStopCandidate, ManagedStopState, stop_managed_run
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution._workload_shell import (
    WorkloadShellObservationResult,
    WorkloadShellObservationState,
    observe_workload_shell,
)
from agentworks.execution.carrier import Dispatch, ExitStatus, Retention
from agentworks.execution.jobs import JobDisposal, JobStop
from agentworks.execution.models import JobRef, Script, Shell
from agentworks.execution.result import ApplicationState, ExecutionFailure, ExecutionOutput, ExecutionResult
from agentworks.operations import (
    LifecycleObligation,
    OperationAttempt,
    _is_pre_registration_refusal,
    release_borrow_after_custody,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.execution._capture_cleanup import CaptureCleanup
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._file_snapshot_exchange import FileSnapshotCandidateResult
    from agentworks.execution._helper_closure import HelperClosureDelivery
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._managed_job_request import ManagedLiveIO
    from agentworks.execution._managed_job_store import Stream
    from agentworks.execution._managed_start_exchange import ManagedStartCandidate
    from agentworks.execution._prepared_helper_stage import PreparedHelperStage
    from agentworks.execution._prepared_stage_closing import PreparedStageCleanup
    from agentworks.execution._runtime_prerequisite import (
        HelperClosureExpectation,
        RuntimeSelection,
        _NumericGuestBootstrap,
    )
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import ByteSink, ByteSource, Carrier, Deadline
    from agentworks.execution.models import Command, Input, Output
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
    helper_requires_owner_retention: bool = False
    scratch_cleanup_pending: bool = False

    @property
    def requires_owner_retention(self) -> bool:
        """Retain ownership for either helper custody or separate input cleanup."""
        return self.helper_requires_owner_retention or self.scratch_cleanup_pending


@dataclass(frozen=True, slots=True, repr=False)
class ManagedStopOutcome:
    """One stop attempt and its proven response after custody accounting."""

    candidate: ManagedStopCandidate | None = field(default=None, repr=False)
    state: ManagedStopState | None = None


class InlineExecutionControlFact(Exception):
    """Safe custody state attached to an escaping inline control flow."""

    def __init__(self, outcome: OwnedInlineOutcome) -> None:
        self.outcome = outcome
        super().__init__("private inline execution stopped with retained operation state")


class ManagedExecutionControlFact(Exception):
    """An exact run reference, not successful launch or aggregate completion."""

    def __init__(self, reference: JobRef) -> None:
        self.reference = reference
        super().__init__("Managed execution retained its originating operation")


@dataclass(slots=True, repr=False)
class _ActiveHelperCall:
    carrier: Carrier
    borrow: OperationBorrow
    prepared: PreparedInlineCandidate | PreparedManagedRead | PreparedManagedLive | None
    operation: BorrowedFixedHelperCarrier
    inline: bool = False
    closure_delivery: HelperClosureDelivery | None = None
    closure_expectation: HelperClosureExpectation | None = None
    not_sent: bool = False
    installed: bool = False
    armed: bool = False
    bookkeeping_retained: bool = False
    candidate: (
        InlineCandidateResult
        | ManagedObservationCandidate
        | ManagedLiveCandidate
        | ManagedStopCandidate
        | DisposalCandidate
        | ManagedStartCandidate
        | WorkloadShellObservationResult
        | FileSnapshotCandidateResult
        | None
    ) = None
    outcome: OwnedInlineOutcome | None = None
    disposal: ManagedDisposalBinding | None = None
    start: ManagedResourceStartBinding | None = None
    tracking_key: object = field(default_factory=object)
    capture: CaptureCleanup | None = None
    input_stage: PreparedHelperStage | None = None
    input_delivery: LocalDeliveryCustody | None = None


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedInlineExecution:
    """Captured non-payload custody for a call whose owner remains retained."""

    outcome: OwnedInlineOutcome


class ExecutionOperation:
    """Own serial helper custody, with exact start/disposal action rows."""

    def __init__(
        self,
        owner: OperationOwner,
        target: ManagedTargetIdentity | None,
        *,
        bootstrap: _NumericGuestBootstrap | None = None,
        managed_repository: ManagedRunRepository | None = None,
        native_binding: NativeExecutionBinding | None = None,
        resource_owner: ManagedRunOwner | None = None,
        route_check: Callable[[Deadline], None] | None = None,
    ) -> None:
        """Bind helper custody to an owner; host DIRECT calls need no managed target."""
        scope = owner.ownership.scope
        if resource_owner is not None and (
            type(resource_owner) is not ManagedRunOwner or resource_owner.kind is not ManagedRunOwnerKind.RESOURCE
        ):
            raise ValidationError("Execution resource binding requires an immutable RESOURCE owner")
        if target is None:
            if scope.resource_kind is not OperationResourceKind.PLATFORM_HOST:
                raise ValidationError("Execution without a managed target requires a platform-host owner")
        elif (
            type(target) is not ManagedTargetIdentity
            or target.kind.value != scope.resource_kind.value
            or target.name != scope.resource_name
        ):
            raise ValidationError("Execution operation target must match its owner scope")
        if bootstrap is not None and (
            target is None
            or target.kind is not ManagedTargetKind.VM
            or vm_guest_boot_id(bootstrap.guest) != target.boot_id
        ):
            raise ValidationError("Numeric execution bootstrap must match the selected VM boot")
        self._owner = owner
        self._managed_target = target
        self._bootstrap = bootstrap
        self._managed_repository = managed_repository
        self._native_binding = native_binding
        self._resource_owner = resource_owner
        self._route_check = route_check
        self._managed_runs: list[ManagedOperationRun] = []
        self._prepared_stages: list[PreparedStageCleanup] = []
        self._captures: list[CaptureCleanup] = []
        self._active_inline_calls: dict[object, _ActiveHelperCall] = {}
        self._unfinished_inline_executions: list[UnfinishedInlineExecution] = []
        self._admission_guard = Lock()
        self._inline_preparation: object | None = None
        self._finishing = False
        self._finished = False
        self._dispatch_id: str | None = None
        self._dispatch_obligation: LifecycleObligation | None = None

    @property
    def _target(self) -> ManagedTargetIdentity:
        return self._require_managed_target()

    def _require_managed_target(self) -> ManagedTargetIdentity:
        target = self._managed_target
        if target is None:
            raise StateError("Managed delivery is unavailable without a managed target")
        return target

    def _require_no_inline_preparation(self, reservation: object | None = None) -> None:
        """Fence new helper admission while a serial DIRECT preparation is live."""
        if self._inline_preparation is not None and self._inline_preparation is not reservation:
            raise StateError("Execution operation retains an active DIRECT preparation")

    @property
    def _scratch_cleanup_pending(self) -> bool:
        return any(capture.pending for capture in self._captures) or any(
            cleanup._stage._begun and (not cleanup._stage.cleaned or cleanup._stage.cleanup_resolution_uncertain)
            for cleanup in self._prepared_stages
        )

    @property
    def managed_runs(self) -> tuple[ManagedOperationRun, ...]:
        return tuple(self._managed_runs)

    def require_job_binding(self, carrier: Carrier, runtime_selection: RuntimeSelection) -> None:
        """Fence an ordinary job call to this live selected operation binding."""
        self._require_no_inline_preparation()
        self._require_managed_target()
        binding = self._native_binding
        if self._finishing or self._finished:
            raise StateError("Execution operation is closing")
        if binding is None:
            raise StateError("Managed delivery is unavailable for this bound operation")
        if carrier is not binding.carrier or runtime_selection != binding.runtime_selection:
            raise ValidationError("Managed access requires the selected native binding")
        with self._owner._guard:  # noqa: SLF001
            self._owner._require_dispatch_admission_locked()  # noqa: SLF001
        # A passive generation check is admission evidence, not a replacement
        # for the separate persisted fence immediately before delivery.
        self._owner.list_pending_lifecycle_obligations()

    def _require_managed_context(
        self,
        *,
        repository: ManagedRunRepository,
        owner: OperationOwner,
        target: ManagedTargetIdentity,
        guest: VMGuestIdentity,
        root_plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
    ) -> None:
        """Match exact context facts before inspecting any managed reservation."""
        self._require_managed_target()
        bootstrap, binding = self._bootstrap, self._native_binding
        if (
            owner is not self._owner
            or repository is not self._managed_repository
            or target != self._target
            or bootstrap is None
            or guest != bootstrap.guest
            or root_plan != bootstrap.root_entry
            or binding is None
            or runtime_selection != binding.runtime_selection
        ):
            raise ValidationError("Managed access does not match its originating operation")

    def require_managed_access(
        self,
        identity: ManagedRunIdentity,
        *,
        repository: ManagedRunRepository,
        owner: OperationOwner,
        target: ManagedTargetIdentity,
        guest: VMGuestIdentity,
        root_plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
    ) -> ManagedOperationRun | ManagedRunOwner:
        """Select planned OP custody explicitly, otherwise require bound RESOURCE."""
        if any(run.receipt.identity == identity for run in self._managed_runs):
            return self.require_managed_run(
                identity,
                repository=repository,
                owner=owner,
                target=target,
                guest=guest,
                root_plan=root_plan,
                runtime_selection=runtime_selection,
            )
        self._require_managed_context(
            repository=repository,
            owner=owner,
            target=target,
            guest=guest,
            root_plan=root_plan,
            runtime_selection=runtime_selection,
        )
        if self._resource_owner is None:
            raise ValidationError("Managed resource access requires this operation's core resource binding")
        return self._resource_owner

    def require_managed_run(
        self,
        identity: ManagedRunIdentity,
        *,
        repository: ManagedRunRepository,
        owner: OperationOwner,
        target: ManagedTargetIdentity,
        guest: VMGuestIdentity,
        root_plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
    ) -> ManagedOperationRun:
        """Resolve authority from this operation's retained acknowledged start."""
        self._require_managed_context(
            repository=repository,
            owner=owner,
            target=target,
            guest=guest,
            root_plan=root_plan,
            runtime_selection=runtime_selection,
        )
        run = next((run for run in self._managed_runs if run.receipt.identity == identity), None)
        if (
            run is None
            or not run.acknowledged
            or run.reservation_uncertain
            or run.receipt.spec.target != target
            or run.receipt.spec.owner != ManagedRunOwner(ManagedRunOwnerKind.OPERATION, owner.ownership.operation_id)
            or run.receipt.spec.lifetime is not ManagedRunLifetime.OPERATION
            or run.reserved is None
            or run.reserved.spec != run.receipt.spec
        ):
            raise ValidationError("Managed access requires an exact retained acknowledged operation run")
        return run

    def observe_managed(
        self,
        carrier: Carrier,
        *,
        expected_launch: bytes,
        root_plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
        deadline: Deadline,
        guest: VMGuestIdentity,
        stream: Stream | None,
        offset: int | None = None,
        count: int | None = None,
    ) -> tuple[ManagedObservationCandidate, ManagedObserveOutcome]:
        """Run one fixed read using the same lifetime row as inline execution."""
        self._require_managed_target()
        prepared = prepare_managed_read(
            operation=ManagedOperation.OBSERVE if stream is None else ManagedOperation.READ_OUTPUT,
            expected_launch=expected_launch,
            stream=stream,
            plan=root_plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            guest=guest,
            offset=offset,
            count=count,
        )
        active = None
        try:
            with self._admission_guard:
                self._require_no_inline_preparation()
                if self._finishing or self._finished or self._active_inline_calls or self._unfinished_inline_executions:
                    raise StateError("Execution operation cannot admit a managed read")
                binding = self._native_binding
                if binding is None or carrier is not binding.carrier:
                    raise ValidationError("Managed read requires its selected native carrier")
                active = self._borrow_helper_call(carrier, prepared)
                operation = active.operation
            try:
                if binding._new_helper_delivery is not None and prepared.closure_expectation is not None:
                    active.closure_expectation = prepared.closure_expectation
                    active.closure_delivery = binding._new_helper_delivery(prepared.closure_expectation)
                    active.operation = BorrowedFixedHelperCarrier(active.closure_delivery, active.borrow)
                    operation = active.operation
                self._admit(active)
                if self._route_check is not None:
                    self._route_check(deadline)
                candidate = execute_managed_read(operation, prepared, deadline=deadline)
                active.candidate = _without_output(candidate)
                completion = candidate.carrier_completion
                if active.closure_delivery is not None and not active.closure_delivery.closure_proven:
                    completion = None
                operation.settle(candidate.dispatch, completion)
                custody = OwnedInlineOutcome(
                    pending_remote_effects=operation.pending_remote_effects,
                    coordination_uncertain=operation.coordination_uncertain,
                    helper_requires_owner_retention=operation.requires_owner_retention,
                )
                with self._admission_guard:
                    self._capture(active, custody)
            except BaseException as control:
                self._raise_control(control, active, operation, deadline)
            return candidate, ManagedObserveOutcome(
                _without_output(candidate),
                custody.pending_remote_effects,
                custody.coordination_uncertain,
                custody.requires_owner_retention,
            )
        finally:
            prepared.clear()
            if active is not None:
                self._detach_helper_payload(active)
                active.prepared = None

    def unobserved_managed_output(self, reference: JobRef) -> ExecutionOutput:
        """Return original OP output policy without borrowing or observing again."""
        self._require_managed_target()
        with self._admission_guard:
            run = next((run for run in self._managed_runs if run.receipt.identity.run_id == reference.run_id), None)
            if run is None or not run.acknowledged or run.reserved is None:
                raise ValidationError("Managed output policy requires its acknowledged operation run")
            retention = {
                ManagedOutputMode.CAPTURE: Retention.CAPTURED,
                ManagedOutputMode.DISCARD: Retention.DISCARDED,
                ManagedOutputMode.SENSITIVITY_SUPPRESSED: Retention.SUPPRESSED,
            }[run.reserved.output_policy.mode]
            return ExecutionOutput(retention=retention)

    def relay_managed(
        self,
        reference: JobRef,
        carrier: Carrier,
        *,
        deadline: Deadline,
        source: ByteSource | None = None,
        stdout: ByteSink | None = None,
        stderr: ByteSink | None = None,
        sensitive: bool = False,
    ) -> tuple[ManagedLiveCandidate, OwnedInlineOutcome]:
        """Borrow one foreground relay for an exact acknowledged OPERATION run."""
        self._require_managed_target()
        prepared = None
        active = None
        try:
            with self._admission_guard:
                self._require_no_inline_preparation()
                if self._finishing or self._finished or self._active_inline_calls or self._unfinished_inline_executions:
                    raise StateError("Execution operation cannot admit a managed relay")
                binding, bootstrap = self._native_binding, self._bootstrap
                if binding is None or bootstrap is None:
                    raise ValidationError("Managed relay requires its selected native binding")
                self.require_job_binding(carrier, binding.runtime_selection)
                run = self._control_run(reference, deadline)
                if (
                    run.live_io is None
                    or run.live_relay_attempted
                    or run.cleanup_complete
                    or run.disposal_attempted
                    or run.disposal_confirmed
                ):
                    raise StateError("Managed relay requires an unused live operation run")
                if not carrier.features.live_stdio:
                    raise ValidationError("Managed relay requires live carrier I/O")
                prepared = prepare_managed_live(
                    expected_launch=encode_managed_job_fact(run.receipt),
                    live_io=run.live_io,
                    bootstrap=bootstrap,
                    runtime_selection=binding.runtime_selection,
                    deadline=deadline,
                    source=source,
                    stdout=stdout,
                    stderr=stderr,
                    sensitive=sensitive,
                )
                # Admission consumes the local attempt even if validation or
                # subsequent dispatch refuses. Consumed input is never replayed.
                run.live_relay_attempted = True
                carrier.validate(prepared.invocation, io=prepared.io)
                active = self._borrow_helper_call(carrier, prepared)
                operation = active.operation
            try:
                if binding._new_helper_delivery is not None:
                    active.closure_expectation = prepared.closure_expectation
                    active.closure_delivery = binding._new_helper_delivery(prepared.closure_expectation)
                    active.operation = BorrowedFixedHelperCarrier(active.closure_delivery, active.borrow)
                    operation = active.operation
                self._admit(active)
                if self._route_check is not None:
                    self._route_check(deadline)
                candidate = execute_managed_live(operation, prepared, deadline=deadline)
                active.candidate = candidate
                completion = candidate.carrier_completion
                if active.closure_delivery is not None and not active.closure_delivery.closure_proven:
                    completion = None
                operation.settle(candidate.dispatch, completion)
                custody = self._outcome(active, operation, deadline, include_candidate=False)
                with self._admission_guard:
                    self._capture(active, custody)
            except BaseException as control:
                self._raise_control(control, active, operation, deadline)
            return candidate, custody
        finally:
            if prepared is not None:
                prepared.clear()
            if active is not None:
                self._detach_helper_payload(active)
                active.prepared = None

    def observe_job(self, reference: JobRef, carrier: Carrier, deadline: Deadline) -> ManagedObserveOutcome:
        """Observe a retained OP job or an exact constructor-bound RESOURCE job."""
        self._require_managed_target()
        repository, bootstrap = self._managed_repository, self._bootstrap
        if type(reference) is not JobRef or repository is None or bootstrap is None or self._native_binding is None:
            raise ValidationError("Managed observation requires its bound operation")
        outcome = observe_bound_managed_run(
            repository,
            ManagedRunIdentity(reference.run_id),
            target=self._target,
            guest=bootstrap.guest,
            root_plan=bootstrap.root_entry,
            carrier=carrier,
            runtime_selection=self._native_binding.runtime_selection,
            deadline=deadline,
            owner=self._owner,
            execution_operation=self,
        )
        if outcome.terminal_proved and any(
            run.receipt.identity.run_id == reference.run_id for run in self._managed_runs
        ):
            self.retain_job_terminal_observation(reference, outcome.candidate)
        return outcome

    def retain_job_terminal_observation(
        self,
        reference: JobRef,
        candidate: ManagedObservationCandidate | None,
    ) -> bool:
        """Retain only positive closure, without caching absent application facts."""
        self._require_managed_target()
        run = next((run for run in self._managed_runs if run.receipt.identity.run_id == reference.run_id), None)
        if run is None or not run.terminal_proved(candidate):
            return False
        run.terminal_observation = candidate
        return True

    def read_job(
        self,
        reference: JobRef,
        carrier: Carrier,
        deadline: Deadline,
        stream: Stream,
        *,
        offset: int,
        count: int,
    ) -> ManagedReadOutputOutcome:
        """Read a closed output stream through retained operation custody."""
        self._require_managed_target()
        repository, bootstrap = self._managed_repository, self._bootstrap
        if type(reference) is not JobRef or repository is None or bootstrap is None or self._native_binding is None:
            raise ValidationError("Managed output requires its bound operation")
        return read_bound_managed_output(
            repository,
            ManagedRunIdentity(reference.run_id),
            stream=stream,
            offset=offset,
            count=count,
            target=self._target,
            guest=bootstrap.guest,
            root_plan=bootstrap.root_entry,
            carrier=carrier,
            runtime_selection=self._native_binding.runtime_selection,
            deadline=deadline,
            owner=self._owner,
            execution_operation=self,
        )

    def wait_job(self, reference: JobRef, carrier: Carrier, deadline: Deadline) -> ManagedResultOutcome:
        """Wait only for observations; the selected keeper remains independent."""
        self._require_managed_target()
        repository, bootstrap = self._managed_repository, self._bootstrap
        if (
            type(reference) is not JobRef
            or repository is None
            or bootstrap is None
            or self._native_binding is None
            or carrier is not self._native_binding.carrier
        ):
            raise ValidationError("Managed wait requires its bound operation")
        try:
            return wait_bound_managed_result(
                repository,
                ManagedRunIdentity(reference.run_id),
                target=self._target,
                guest=bootstrap.guest,
                root_plan=bootstrap.root_entry,
                carrier=carrier,
                runtime_selection=self._native_binding.runtime_selection,
                deadline=deadline,
                owner=self._owner,
                execution_operation=self,
            )
        except ManagedDeadlineExpired as control:
            if control.__cause__ is not None or not any(
                run.receipt.identity.run_id == reference.run_id for run in self._managed_runs
            ):
                raise
            # Launch acknowledgement may consume the selected wait budget.
            # Resolve only retained local evidence: no renewed deadline, I/O
            # or invented application-entry/termination precision after expiry.
            run = self.require_managed_run(
                ManagedRunIdentity(reference.run_id),
                repository=repository,
                owner=self._owner,
                target=self._target,
                guest=bootstrap.guest,
                root_plan=bootstrap.root_entry,
                runtime_selection=self._native_binding.runtime_selection,
            )
            assert run.reserved is not None
            retention = {
                ManagedOutputMode.CAPTURE: Retention.CAPTURED,
                ManagedOutputMode.DISCARD: Retention.DISCARDED,
                ManagedOutputMode.SENSITIVITY_SUPPRESSED: Retention.SUPPRESSED,
            }[run.reserved.output_policy.mode]
            missing = ExecutionOutput(retention=retention)
            return ManagedResultOutcome(
                ExecutionResult(
                    Dispatch.UNKNOWN,
                    ApplicationState.UNKNOWN,
                    stdout=missing,
                    stderr=missing,
                    failure=ExecutionFailure.DEADLINE,
                    deadline_exceeded=True,
                ),
                (),
            )

    def _control_run(self, reference: JobRef, deadline: Deadline) -> ManagedOperationRun:
        repository, bootstrap, binding = self._managed_repository, self._bootstrap, self._native_binding
        if type(reference) is not JobRef or repository is None or bootstrap is None or binding is None:
            raise ValidationError("Managed control requires its bound operation")
        identity = ManagedRunIdentity(reference.run_id)
        preflight_bound_run(
            repository,
            identity,
            target=self._target,
            guest=bootstrap.guest,
            root_plan=bootstrap.root_entry,
            runtime_selection=binding.runtime_selection,
            deadline=deadline,
            owner=self._owner,
            execution_operation=self,
        )
        return self.require_managed_run(
            identity,
            repository=repository,
            owner=self._owner,
            target=self._target,
            guest=bootstrap.guest,
            root_plan=bootstrap.root_entry,
            runtime_selection=binding.runtime_selection,
        )

    def stop_job(self, reference: JobRef, deadline: Deadline) -> JobStop:
        """Stop one exact OP or RESOURCE job without adopting independent custody."""
        self._require_managed_target()
        if type(reference) is not JobRef:
            raise ValidationError("Managed control requires its bound operation")
        if not any(run.receipt.identity.run_id == reference.run_id for run in self._managed_runs):
            return stop_resource_job(self, reference, deadline)
        run = self._control_run(reference, deadline)
        if run.disposal_confirmed:
            return JobStop(reference, True, True)
        outcome = self._stop_managed(encode_managed_job_fact(run.receipt), deadline, run=run)
        accepted = outcome.state in {ManagedStopState.ACCEPTED, ManagedStopState.TERMINATED}
        if not accepted:
            return JobStop(
                reference,
                False if outcome.candidate is not None and outcome.candidate.dispatch is Dispatch.NOT_SENT else None,
                False,
                ExecutionFailure.DEADLINE if deadline.expired else ExecutionFailure.OBSERVATION,
                deadline.expired,
            )
        if deadline.expired:
            return JobStop(reference, True, False, ExecutionFailure.DEADLINE, True)
        binding = self._native_binding
        assert binding is not None
        try:
            terminal = self.observe_job(reference, binding.carrier, deadline)
        except (StateError, ValidationError):
            return JobStop(reference, True, False, ExecutionFailure.OBSERVATION, deadline.expired)
        proved = not terminal.requires_owner_retention and self.retain_job_terminal_observation(
            reference, terminal.candidate
        )
        if proved:
            run.cleanup_complete = True
        return JobStop(
            reference,
            True,
            proved,
            ExecutionFailure.DEADLINE if deadline.expired else None if proved else ExecutionFailure.OBSERVATION,
            deadline.expired,
        )

    def _stop_managed(
        self, expected_launch: bytes, deadline: Deadline, *, run: ManagedOperationRun | None = None
    ) -> ManagedStopOutcome:
        """Acquire ordinary lifetime custody; drain only an actual selected OP keeper."""
        binding, bootstrap = self._native_binding, self._bootstrap
        assert binding is not None and bootstrap is not None
        with self._admission_guard:
            self._require_no_inline_preparation()
            if self._finishing or self._finished or self._active_inline_calls or self._unfinished_inline_executions:
                raise StateError("Execution operation cannot admit a managed stop")
            active = self._borrow_helper_call(binding.carrier, None)
            operation = active.operation
        candidate = None
        try:
            self._admit(active)
            if run is None or run.keeper.drain(deadline).drained:
                if run is not None:
                    # A drained worker does not clear an earlier unknown closing
                    # helper; reuse its existing local-custody/binding gate.
                    run.keeper._require_cleanup(deadline)  # noqa: SLF001
                if self._route_check is not None:
                    self._route_check(deadline)
                candidate = stop_managed_run(
                    operation,
                    expected_launch=expected_launch,
                    plan=bootstrap.root_entry,
                    deadline=deadline,
                    runtime_selection=binding.runtime_selection,
                    guest=bootstrap.guest,
                )
                active.candidate = candidate
                if run is not None:
                    run.keeper.last_stop = candidate
                operation.settle(candidate.dispatch, candidate.carrier_completion)
            custody = self._outcome(active, operation, deadline, include_candidate=False)
        except BaseException as control:
            self._raise_control(control, active, operation, deadline)
        with self._admission_guard:
            try:
                self._capture(active, custody)
            except BaseException:
                active.bookkeeping_retained = True
                raise
        state = None if candidate is None or candidate.observation is None else candidate.observation.state
        return ManagedStopOutcome(
            candidate,
            state
            if not custody.helper_requires_owner_retention
            and candidate is not None
            and candidate.dispatch is Dispatch.SENT
            else None,
        )

    def dispose_job(self, reference: JobRef, carrier: Carrier, deadline: Deadline) -> JobDisposal:
        """Observe terminal proof before draining, then dispose with exact retry custody."""
        self._require_managed_target()
        if type(reference) is JobRef and not any(
            run.receipt.identity.run_id == reference.run_id for run in self._managed_runs
        ):
            return dispose_resource_job(self, reference, carrier, deadline)
        run = self._control_run(reference, deadline)
        if run.disposal_confirmed:
            return JobDisposal(reference, True)
        if run.disposal_attempted:
            row = self._owner.inspect_lifecycle_obligation(run.disposal_obligation_id)
            if row is not None:
                if (
                    row.ownership != self._owner.ownership
                    or row.obligation_kind != MANAGED_DISPOSAL_OBLIGATION_KIND
                    or row.payload_version != MANAGED_DISPOSAL_PAYLOAD_VERSION
                    or row.payload != encode_managed_disposal_obligation(reference.run_id)
                    or row.payload_revision != 0
                ):
                    raise StateError("Managed disposal lifecycle binding changed")
                if row.state is LifecycleObligationState.RESOLVED:
                    # A settled helper may lose its reply before fresh-ID or
                    # confirmation publication. Never rearm its resolved row.
                    # Keep attempted=True and retained proof: disposal may
                    # already have removed launch artifacts before that loss.
                    run.disposal_obligation_id = uuid4().hex
        if not run.disposal_attempted:
            observed = self.observe_job(reference, carrier, deadline)
            if observed.requires_owner_retention:
                return JobDisposal(reference, None, ExecutionFailure.OBSERVATION, deadline.expired)
            if not run.terminal_proved(observed.candidate):
                return JobDisposal(
                    reference, False, ExecutionFailure.DEADLINE if deadline.expired else None, deadline.expired
                )
            run.terminal_observation = observed.candidate
        if not run.terminal_proved(run.terminal_observation):
            raise StateError("Managed disposal retains no positive terminal proof")
        with self._admission_guard:
            self._require_no_inline_preparation()
            if self._active_inline_calls or self._unfinished_inline_executions:
                raise StateError("Managed disposal cannot overlap unfinished ordinary delivery")
            if not run.keeper.drain(deadline).drained:
                return JobDisposal(reference, None, ExecutionFailure.CLEANUP, deadline.expired)
            if deadline.expired:
                return JobDisposal(reference, False, ExecutionFailure.DEADLINE, True)
            run.keeper._require_cleanup(deadline)  # noqa: SLF001
            repository, bootstrap, binding = self._managed_repository, self._bootstrap, self._native_binding
            assert repository is not None and bootstrap is not None and binding is not None
            if self._route_check is not None:
                self._route_check(deadline)
            run.disposal_attempted = True
            try:
                outcome = dispose_bound_managed_run(
                    repository,
                    run.receipt.identity,
                    target=self._target,
                    guest=bootstrap.guest,
                    root_plan=bootstrap.root_entry,
                    carrier=carrier,
                    runtime_selection=binding.runtime_selection,
                    deadline=deadline,
                    owner=self._owner,
                    obligation_id=run.disposal_obligation_id,
                    execution_operation=self,
                )
            except BaseException as control:
                if isinstance(control.__cause__, ManagedDisposalControlFact):
                    retained = control.__cause__.outcome
                    if not (
                        retained.requires_owner_retention
                        or retained.pending_remote_effects
                        or retained.coordination_uncertain
                    ):
                        run.disposal_obligation_id = uuid4().hex
                        run.disposal_attempted = False
                raise
            confirmed = outcome.state is DisposalState.DISPOSED and not outcome.requires_owner_retention
            if confirmed:
                run.disposal_confirmed = True
            elif not (
                outcome.requires_owner_retention or outcome.pending_remote_effects or outcome.coordination_uncertain
            ):
                # A clean refusal resolved its one-attempt row. A later
                # explicit attempt must register a fresh row, never reopen it.
                # Publish its fresh identity first: interruption may retain
                # attempted=True, but terminal proof and old helper settlement
                # still permit a safe receipt-bound attempt on this new row.
                run.disposal_obligation_id = uuid4().hex
                run.disposal_attempted = False
            return JobDisposal(
                reference,
                True if confirmed else None if outcome.requires_owner_retention else False,
                ExecutionFailure.DEADLINE
                if deadline.expired
                else ExecutionFailure.OBSERVATION
                if outcome.requires_owner_retention
                else None,
                deadline.expired,
            )

    def start_resource_managed(
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
    ) -> ResourceStartAcknowledgement:
        """Start exact RESOURCE work without adopting its job lifetime."""
        self.require_job_binding(carrier, runtime_selection)
        return start_resource_job(
            self,
            carrier,
            invocation,
            plan=plan,
            runtime_selection=runtime_selection,
            deadline=deadline,
            input=input,
            output=output,
            env=env,
            cwd=cwd,
            sensitive=sensitive,
        )

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
        live_io: ManagedLiveIO | None = None,
    ) -> JobRef:
        """Freeze a body, retain its run, then reserve and start exactly once."""
        self.require_job_binding(carrier, runtime_selection)
        with self._admission_guard:
            self._require_no_inline_preparation()
            binding, repository, bootstrap = self._native_binding, self._managed_repository, self._bootstrap
            if self._finishing or self._finished or self._active_inline_calls or self._unfinished_inline_executions:
                raise StateError("Execution operation is closing")
            with self._owner._guard:  # noqa: SLF001
                self._owner._require_dispatch_admission_locked()  # noqa: SLF001
            if binding is None or binding._new_managed_delivery is None or repository is None or bootstrap is None:
                raise StateError("Managed delivery is unavailable for this bound operation")
            if carrier is not binding.carrier or runtime_selection != binding.runtime_selection:
                raise ValidationError("Managed start requires the selected native binding")
            if live_io is not None and not carrier.features.live_stdio:
                raise ValidationError("Managed foreground I/O requires a live carrier")
            identity = ManagedRunIdentity(uuid4().hex)
            user_default = isinstance(invocation, Script) and invocation.shell is Shell.USER_DEFAULT
            shell = (
                ManagedShellIdentity(Shell.USER_DEFAULT, "/bin/sh", invocation.login, invocation.interactive)
                if user_default and isinstance(invocation, Script)
                else _explicit_managed_shell(invocation)
            )
            spec = ManagedRunSpec(
                self._target,
                _validate_plan(plan),
                shell,
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
                live_io=live_io,
            )
            if live_io is not None:
                prepared = prepare_managed_live(
                    expected_launch=body.request.launch,
                    live_io=live_io,
                    bootstrap=bootstrap,
                    runtime_selection=runtime_selection,
                    deadline=deadline,
                    source=input.source,
                    stdout=output.stdout,
                    stderr=output.stderr,
                    sensitive=sensitive or input.is_sensitive,
                )
                try:
                    carrier.validate(prepared.invocation, io=prepared.io)
                finally:
                    prepared.clear()
        if self._route_check is not None:
            self._route_check(deadline)
        if user_default:
            resolved = self._observe_workload_shell(carrier, plan, runtime_selection, deadline)
            spec = replace(spec, shell=replace(shell, resolved_executable=resolved))
            # Rebind only the launch identity. Caller input was frozen before
            # lookup and must not be read again after guest effects.
            receipt = ManagedRunReceipt(identity, identity.unit_name, spec)
            body = replace(body, request=replace(body.request, launch=encode_managed_job_fact(receipt)))
            if live_io is not None:
                prepared = prepare_managed_live(
                    expected_launch=body.request.launch,
                    live_io=live_io,
                    bootstrap=bootstrap,
                    runtime_selection=runtime_selection,
                    deadline=deadline,
                    source=input.source,
                    stdout=output.stdout,
                    stderr=output.stderr,
                    sensitive=sensitive or input.is_sensitive,
                )
                try:
                    carrier.validate(prepared.invocation, io=prepared.io)
                finally:
                    prepared.clear()
        with self._admission_guard:
            self._require_no_inline_preparation()
            if self._finishing or self._finished or self._active_inline_calls or self._unfinished_inline_executions:
                raise StateError("Execution operation cannot admit a managed start")
            with self._owner._guard:  # noqa: SLF001
                self._owner._require_dispatch_admission_locked()  # noqa: SLF001
            if deadline.expired:
                raise ValidationError("Managed start deadline expired before reservation")
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
                live_io=live_io,
                route_check=self._route_check,
                new_helper_delivery=binding._new_helper_delivery,
            )
            self._managed_runs.append(run)
            reference = JobRef(identity.run_id)
            try:
                run.start(body, deadline)
            except BaseException as control:
                fact = ManagedExecutionControlFact(reference)
                fact.__cause__ = control.__cause__
                raise control from fact
            if not run.acknowledged:
                raise StateError("Managed start was not acknowledged") from ManagedExecutionControlFact(reference)
            return reference

    def _observe_workload_shell(
        self,
        carrier: Carrier,
        plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
        deadline: Deadline,
    ) -> str:
        """Resolve the selected account through the tracked helper lifetime."""
        self._require_managed_target()
        with self._admission_guard:
            self._require_no_inline_preparation()
            if self._finishing or self._finished or self._active_inline_calls or self._unfinished_inline_executions:
                raise StateError("Execution operation cannot admit a shell lookup")
            active = self._borrow_helper_call(carrier, None)
            operation = active.operation
        try:
            self._admit(active)
            if self._route_check is not None:
                self._route_check(deadline)
            result = observe_workload_shell(
                operation, plan=plan, runtime_selection=runtime_selection, deadline=deadline, bootstrap=self._bootstrap
            )
            active.candidate = result
            operation.settle(result.dispatch, result.carrier_completion)
            custody = self._outcome(active, operation, deadline, include_candidate=False)
        except BaseException as control:
            self._raise_control(control, active, operation, deadline)
        try:
            with self._admission_guard:
                self._capture(active, custody)
        except BaseException as control:
            self._raise_control(control, active, operation, deadline)
        observed = result.observation
        if (
            result.dispatch is not Dispatch.SENT
            or result.carrier_completion != ExitStatus(code=0)
            or result.carrier_failure is not None
            or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
            or observed is None
            or observed.state is not WorkloadShellObservationState.RESOLVED
            or observed.shell is None
            or custody.helper_requires_owner_retention
        ):
            raise ManagedJobShellRefusal(
                ManagedJobShellFact(
                    result,
                    custody.pending_remote_effects,
                    custody.coordination_uncertain,
                    custody.requires_owner_retention,
                )
            )
        return observed.shell

    @property
    def active_inline_calls(self) -> tuple[_ActiveHelperCall, ...]:
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
            self._require_no_inline_preparation()
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

    @property
    def scratch_helper_pending(self) -> bool:
        return any(cleanup.helper_pending for cleanup in self._prepared_stages) or any(
            capture.helper_pending for capture in self._captures
        )

    @property
    def closing_scratch_helper_pending(self) -> bool:
        return any(cleanup.closing_helper_pending for cleanup in self._prepared_stages) or any(
            capture.closing_helper_pending for capture in self._captures
        )

    def finish_scratch_cleanup(self, deadline: Deadline) -> None:
        """Drain each exact helper before advancing independent scratch cleanup."""
        for cleanup in tuple(self._prepared_stages):
            if cleanup.finish(deadline):
                self._prepared_stages.remove(cleanup)
            if cleanup.helper_pending:
                raise StateError("Prepared input cleanup retains helper custody")
        for capture in tuple(self._captures):
            if capture.finish(deadline):
                self._captures.remove(capture)
            if capture.helper_pending:
                raise StateError("Capture cleanup retains helper custody")

    def retry_inline_bookkeeping(self) -> None:
        """Relinquish a failed call using exact bookkeeping, without dispatch.

        The retained call supplies its original borrow and candidate evidence.
        Unknown helper attempts remain recovery custody and cannot be settled.
        """
        with self._admission_guard:
            self._retry_bookkeeping(finishing=self._finishing)

    def observe_inline_cleanup(self, deadline: Deadline) -> None:
        """Observe only retained exact helpers, without resuming application output."""
        with self._admission_guard:
            for active in tuple(self._active_inline_calls.values()):
                delivery = active.closure_delivery
                if delivery is None:
                    continue
                if not active.bookkeeping_retained:
                    raise StateError("Inline helper is still active")
                attempt = active.operation.outstanding_attempt
                if attempt is None:
                    continue
                if (active.not_sent or delivery.closure_proven) and attempt.local_delivery.settled:
                    continue
                if deadline.expires_at is None or deadline.expired:
                    raise ValidationError("Inline cleanup observation requires a fresh finite deadline")
                self._validate_closure_call(active)
                if not attempt.local_delivery.close(deadline):
                    raise StateError("Inline helper local delivery remains unsettled")
                self._validate_closure_call(active)
                if not active.not_sent and not delivery.closure_proven:
                    if self._route_check is not None:
                        self._route_check(deadline)
                    self._validate_closure_call(active)
                    if not delivery.observe_closure(deadline=deadline, custody=attempt.local_delivery):
                        raise StateError("Inline helper closure remains unknown")
                    self._validate_closure_call(active)
                note_input_activation_closed(active)
                if active.capture is not None:
                    active.capture.note_application(active)
                active.operation.settle_helper_closure()
                self._capture(active, OwnedInlineOutcome())

    def _validate_closure_call(self, active: _ActiveHelperCall) -> None:
        """Fence held continuation to the exact local borrow and durable generation."""
        owner = self._owner
        with owner._guard:  # noqa: SLF001
            if owner._active_borrow is not active.borrow or active.borrow._closed:  # noqa: SLF001
                raise StateError("Inline closure no longer owns its borrow")
            outstanding = owner._outstanding_attempt  # noqa: SLF001
            if outstanding is not None and outstanding is not active.operation.outstanding_attempt:
                raise StateError("Inline closure no longer owns its attempt")
        identity = active.start.obligation_id if active.start is not None else self._dispatch_id
        row = owner.inspect_lifecycle_obligation(identity) if identity is not None else None
        if row is None:
            raise StateError("Inline closure lifetime obligation is unavailable")
        if active.start is not None:
            _, kind, version, payload = active.start.registration
            if (row.obligation_kind, row.payload_version, row.payload, row.payload_revision) != (
                kind,
                version,
                payload,
                0,
            ):
                raise StateError("Managed start closure obligation binding changed")
        expectation = active.closure_expectation
        bootstrap = self._bootstrap
        binding = self._native_binding
        if binding is None or binding.carrier is not active.carrier:
            raise StateError("Inline closure native binding changed")
        if (
            expectation is None
            or expectation.guest != (bootstrap.guest if bootstrap is not None else None)
            or (
                expectation.guest is not None
                and (
                    self._managed_target is None
                    or self._managed_target.kind is not ManagedTargetKind.VM
                    or vm_guest_boot_id(expectation.guest) != self._managed_target.boot_id
                )
            )
        ):
            raise StateError("Inline closure guest binding changed")

    def _retry_bookkeeping(self, *, finishing: bool) -> None:
        for active in tuple(self._active_inline_calls.values()):
            self._settle_retained_helper(active)
            if active.start is not None:
                self._capture(active, OwnedInlineOutcome())
                if active.tracking_key in self._active_inline_calls:
                    raise StateError("Managed start retains unresolved launch custody")
                continue
            if active.disposal is not None:
                if active.candidate is None:
                    settle_unused_disposal_call(self, active)
                else:
                    self._capture(active, OwnedInlineOutcome())
                if active.tracking_key in self._active_inline_calls:
                    raise StateError("Managed disposal retains unresolved action custody")
                continue
            operation = active.operation
            if not active.armed:
                if finishing:
                    if not active.installed and not self._borrow_closed(active):
                        self._recover_registration(active)
                    if not self._borrow_closed(active):
                        active.borrow.close()
                    self._active_inline_calls.pop(active.tracking_key)
                    continue
                self._admit(active)
            outcome = OwnedInlineOutcome(
                pending_remote_effects=operation.pending_remote_effects,
                coordination_uncertain=operation.coordination_uncertain,
                helper_requires_owner_retention=operation.requires_owner_retention,
            )
            self._capture(active, outcome)

    def _settle_retained_helper(self, active: _ActiveHelperCall) -> None:
        """Reconcile this actual helper's local custody without any delivery."""
        if not active.bookkeeping_retained:
            raise StateError("Inline execution still has an active call")
        note_input_activation_closed(active)
        if active.capture is not None:
            active.capture.note_application(active)
        operation = active.operation
        candidate = active.candidate
        if active.closure_delivery is not None and operation.outstanding_attempt is not None:
            if not active.not_sent and not active.closure_delivery.closure_proven:
                raise StateError("Inline execution retains unknown helper closure")
            self._validate_closure_call(active)
            operation.settle_helper_closure()
            return
        if candidate is not None and self._known_termination(candidate):
            if not active.borrow.has_outstanding_attempt:
                operation.outstanding_attempt = None
            else:
                operation.settle(candidate.dispatch, candidate.carrier_completion)
            operation.coordination_uncertain = False
        elif not operation.pending_remote_effects and operation.outstanding_attempt is None:
            # A lost begin reply precedes entry into the actual carrier.
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

    def _recover_registration(self, active: _ActiveHelperCall) -> None:
        """Read an interrupted, never-dispatched registration during finish."""
        owner = self._owner
        borrow = active.borrow
        with owner._guard:  # noqa: SLF001
            owner._reconcile_transition_locked()  # noqa: SLF001
            if owner._active_borrow is not borrow or borrow._attempt_started:  # noqa: SLF001
                raise StateError("Inline registration no longer owns its unused borrow")
            identity, kind, version, payload = self._helper_registration(active)
            row = owner._repository.inspect_lifecycle_obligation(owner.ownership, identity)  # noqa: SLF001
            if row is None:
                if active.installed or (
                    active.disposal is None and active.start is None and self._dispatch_obligation is not None
                ):
                    raise StateError("Inline lifetime obligation disappeared")
                return
            if (row.obligation_kind, row.payload_version, row.payload) != (kind, version, payload) or (
                (active.disposal is not None or active.start is not None) and row.payload_revision != 0
            ):
                raise StateError("Inline lifetime obligation identity conflicts")
            borrow._dispatch_obligation = row  # noqa: SLF001
            if active.disposal is None and active.start is None:
                self._dispatch_obligation = LifecycleObligation(owner, row)
            active.installed = True

    @staticmethod
    def _known_termination(
        candidate: InlineCandidateResult
        | ManagedObservationCandidate
        | ManagedLiveCandidate
        | ManagedStopCandidate
        | DisposalCandidate
        | ManagedStartCandidate
        | WorkloadShellObservationResult
        | FileSnapshotCandidateResult,
    ) -> bool:
        return candidate.dispatch is Dispatch.NOT_SENT or (
            candidate.dispatch is Dispatch.SENT and candidate.carrier_completion == ExitStatus(code=0)
        )

    def _admit(self, active: _ActiveHelperCall) -> None:
        dispatch_id, kind, version, payload = self._helper_registration(active)
        if not active.installed:
            obligation = active.borrow.install_dispatch_obligation(
                dispatch_id, kind, payload_version=version, payload=payload
            )
            if active.disposal is None and active.start is None:
                self._dispatch_obligation = obligation
            active.installed = True
        if not active.armed:
            active.borrow.arm_dispatch_obligation()
            active.armed = True

    def _helper_registration(self, active: _ActiveHelperCall) -> tuple[str, str, int, bytes]:
        if active.start is not None:
            return active.start.registration
        if active.disposal is not None:
            return active.disposal.registration
        assert self._dispatch_id is not None
        return self._dispatch_id, "carrier-dispatch", 1, b""

    def _borrow_helper_call(
        self,
        carrier: Carrier,
        prepared: PreparedInlineCandidate | PreparedManagedRead | PreparedManagedLive | None,
        *,
        inline: bool = False,
        disposal: ManagedDisposalBinding | None = None,
        start: ManagedResourceStartBinding | None = None,
        delivery: HelperClosureDelivery | None = None,
        replacing: _ActiveHelperCall | None = None,
        preparation: object | None = None,
        input_stage: PreparedHelperStage | None = None,
        capture: CaptureCleanup | None = None,
    ) -> _ActiveHelperCall:
        """Borrow and publish one helper call while its admission guard is held."""
        self._require_no_inline_preparation(preparation)
        borrow = self._owner.borrow()
        active = None
        try:
            operation = (
                BorrowedFixedHelperCarrier(carrier, borrow)
                if delivery is None
                else BorrowedFixedHelperCarrier(carrier, borrow, delivery=delivery)
            )
            active = _ActiveHelperCall(
                carrier,
                borrow,
                prepared,
                operation,
                inline=inline,
                disposal=disposal,
                start=start,
                closure_delivery=delivery,
                input_stage=input_stage,
                capture=capture,
            )
            if replacing is not None:
                active.tracking_key = replacing.tracking_key
            if disposal is None and start is None and self._dispatch_id is None:
                self._dispatch_id = uuid4().hex
            self._active_inline_calls[active.tracking_key] = active
            return active
        except BaseException as control:
            # A mapping publication may commit before its reply is interrupted.
            # Keep the actual replacement visible rather than restoring its predecessor.
            if active is not None and self._active_inline_calls.get(active.tracking_key) is active:
                active.bookkeeping_retained = True
            try:
                borrow.close()
            except BaseException:
                raise control from control.__cause__
            raise

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
        source: ByteSource | None = None,
        stdout: ByteSink | None = None,
        stderr: ByteSink | None = None,
    ) -> OwnedInlineOutcome:
        """Choose delivery before exactly one ordinary helper attempt."""
        if capture_limit is not None and type(capture_limit) is int and capture_limit > 4096:
            from agentworks.execution._capture_operation import run_capture

            return run_capture(
                self,
                carrier,
                request,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                stdin=stdin,
                env=env,
                cwd=cwd,
                capture_limit=capture_limit,
                sensitive=sensitive,
                source=source,
                stdout=stdout,
                stderr=stderr,
            )
        return run_direct_input(
            self,
            carrier,
            request,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            stdin=stdin,
            env=env,
            cwd=cwd,
            capture_limit=capture_limit,
            sensitive=sensitive,
            source=source,
            stdout=stdout,
            stderr=stderr,
        )

    def _run_inline_prepared(
        self,
        carrier: Carrier,
        prepared: PreparedInlineCandidate,
        deadline: Deadline,
        reservation: object,
        stage: PreparedHelperStage | None,
        capture: CaptureCleanup | None = None,
    ) -> OwnedInlineOutcome:
        """Transfer preparation admission into the actual tracked helper borrow."""
        if deadline.expired:
            raise ValidationError("Inline execution deadline expired during preparation")
        if self._finishing or self._finished:
            raise StateError("Inline execution cannot admit new work")
        if self._route_check is not None:
            self._route_check(deadline)
        with self._admission_guard:
            self._require_no_inline_preparation(reservation)
            if self._finishing or self._finished or self._active_inline_calls or self._unfinished_inline_executions:
                raise StateError("Inline execution cannot admit new work")
            if deadline.expired:
                raise ValidationError("Inline execution deadline expired during route admission")
            active = self._borrow_helper_call(
                carrier, prepared, inline=True, preparation=reservation, input_stage=stage, capture=capture
            )
            self._inline_preparation = None
            operation = active.operation
        try:
            try:
                binding = self._native_binding
                if binding is not None and binding.carrier is carrier and binding._new_helper_delivery is not None:
                    active.closure_expectation = prepared.closure_expectation
                    active.closure_delivery = binding._new_helper_delivery(prepared.closure_expectation)
                    active.operation = BorrowedFixedHelperCarrier(active.closure_delivery, active.borrow)
                    operation = active.operation
                self._admit(active)
                candidate = execute_inline_candidate(operation, prepared, deadline=deadline)
                active.candidate = candidate
                note_input_activation_closed(active)
                if active.capture is not None:
                    active.capture.note_application(active)
                completion = candidate.carrier_completion
                if active.closure_delivery is not None and not active.closure_delivery.closure_proven:
                    completion = None
                operation.settle(candidate.dispatch, completion)
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
        finally:
            self._detach_helper_payload(active)

    @staticmethod
    def _detach_helper_payload(active: _ActiveHelperCall) -> None:
        if isinstance(active.prepared, PreparedInlineCandidate):
            active.prepared.release()
            active.prepared = None
        if isinstance(active.prepared, PreparedManagedLive):
            active.prepared.clear()
            active.prepared = None
        if isinstance(active.prepared, PreparedManagedRead) and isinstance(
            active.candidate, ManagedObservationCandidate
        ):
            # Plain carriers still need their original termination evidence.
            active.candidate = replace(active.candidate, observation=None)
        if active.closure_delivery is not None:
            # Positive nondispatch survives; caller observations and input do not.
            active.not_sent = active.candidate is not None and active.candidate.dispatch is Dispatch.NOT_SENT
            active.prepared = None
            active.candidate = None
            if active.outcome is not None:
                active.outcome = replace(active.outcome, candidate=None)

    def _outcome(
        self,
        active: _ActiveHelperCall,
        operation: BorrowedFixedHelperCarrier,
        deadline: Deadline,
        *,
        include_candidate: bool,
    ) -> OwnedInlineOutcome:
        return OwnedInlineOutcome(
            candidate=active.candidate
            if include_candidate and isinstance(active.candidate, InlineCandidateResult)
            else None,
            deadline_exceeded=deadline.expired,
            pending_remote_effects=operation.pending_remote_effects,
            coordination_uncertain=operation.coordination_uncertain,
            helper_requires_owner_retention=operation.requires_owner_retention,
            scratch_cleanup_pending=self._scratch_cleanup_pending,
        )

    def _raise_control(
        self,
        control: BaseException,
        active: _ActiveHelperCall,
        operation: BorrowedFixedHelperCarrier,
        deadline: Deadline,
    ) -> None:
        with self._admission_guard:
            active.bookkeeping_retained = True
            try:
                note_input_activation_closed(active)
                if active.capture is not None:
                    active.capture.note_application(active)
                outcome = self._outcome(active, operation, deadline, include_candidate=False)
            except BaseException:
                raise control from control.__cause__
            try:
                retryable = (active.candidate is not None and self._known_termination(active.candidate)) or (
                    operation.coordination_uncertain and not operation.pending_remote_effects
                )
                if active.disposal is not None or active.start is not None:
                    self._capture(active, outcome)
                    assert active.outcome is not None
                    outcome = active.outcome
                elif _is_pre_registration_refusal(control) and not active.installed and not active.armed:
                    active.borrow.close()
                    self._active_inline_calls.pop(active.tracking_key)
                elif active.armed and not retryable:
                    self._capture(active, outcome)
                else:
                    outcome = replace(outcome, coordination_uncertain=True, helper_requires_owner_retention=True)
                    active.outcome = outcome
            except BaseException:
                try:
                    outcome = replace(outcome, coordination_uncertain=True, helper_requires_owner_retention=True)
                    active.outcome = outcome
                except BaseException:
                    raise control from control.__cause__
            try:
                fact: Exception = (
                    ManagedStartControlFact(
                        ManagedStartOutcome(
                            deadline_exceeded=outcome.deadline_exceeded,
                            pending_remote_effects=outcome.pending_remote_effects,
                            coordination_uncertain=outcome.coordination_uncertain,
                            requires_owner_retention=outcome.requires_owner_retention,
                        )
                    )
                    if active.start is not None
                    else ManagedDisposalControlFact(
                        ManagedDisposalOutcome(
                            pending_remote_effects=outcome.pending_remote_effects,
                            coordination_uncertain=outcome.coordination_uncertain,
                            requires_owner_retention=outcome.requires_owner_retention,
                        )
                    )
                    if active.disposal is not None
                    else ManagedObserveControlFact(
                        ManagedObserveOutcome(
                            pending_remote_effects=outcome.pending_remote_effects,
                            coordination_uncertain=outcome.coordination_uncertain,
                            requires_owner_retention=outcome.requires_owner_retention,
                        )
                    )
                    if not active.inline
                    else InlineExecutionControlFact(outcome)
                )
                fact.__cause__ = control.__cause__
            except BaseException:
                raise control from control.__cause__
        raise control from fact

    def _capture(self, active: _ActiveHelperCall, outcome: OwnedInlineOutcome) -> None:
        note_input_activation_closed(active)
        if active.capture is not None:
            active.capture.note_application(active)
        if active.start is not None:
            capture_resource_start(self, active, outcome)
            return
        if active.disposal is not None:
            capture_disposal_call(self, active, outcome)
            return
        active.outcome = outcome
        if active.closure_delivery is not None and outcome.helper_requires_owner_retention:
            # The original attempt still needs its OPEN borrow for settlement.
            active.bookkeeping_retained = True
            return
        # Both handoffs are local core transitions. A reply can be lost after
        # the borrow relinquishes authority, so observe that exact local fact
        # before repeating a handoff. Never default-close the lifetime row.
        if not self._borrow_closed(active):
            release_borrow_after_custody(active.borrow, retain_effect=True)
        if outcome.helper_requires_owner_retention:
            self._unfinished_inline_executions.append(UnfinishedInlineExecution(replace(outcome, candidate=None)))
        self._active_inline_calls.pop(active.tracking_key)

    def _borrow_closed(self, active: _ActiveHelperCall) -> bool:
        with self._owner._guard:  # noqa: SLF001
            return active.borrow._closed  # noqa: SLF001
