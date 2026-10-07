"""One-shot RESOURCE launch through the existing operation's helper custody."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.operations import LifecycleObligation

from ._helper_launcher import _validate_plan
from ._managed_job_access import _explicit_managed_shell
from ._managed_job_protocol import encode_managed_job_fact
from ._managed_request_adapter import compose_managed_body
from ._managed_runs import (
    ManagedLaunchObservation,
    ManagedLaunchState,
    ManagedOutputPolicy,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunReceipt,
    ManagedRunSpec,
    ManagedShellIdentity,
)
from ._managed_start_exchange import ManagedStartCandidate, ManagedStartState, prepare_managed_start
from ._managed_start_operation import (
    MANAGED_START_OBLIGATION_KIND,
    MANAGED_START_PAYLOAD_VERSION,
    encode_managed_start_obligation,
    start_borrowed_managed_run,
)
from .binding import _IndependentJobAvailability
from .carrier import Dispatch, ExitStatus
from .models import JobRef, Script, Shell

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ._execution_operation import ExecutionOperation, OwnedInlineOutcome, _ActiveHelperCall
    from ._helper_launcher import IdentityPlan
    from ._managed_request_adapter import _ManagedBody
    from ._runtime_prerequisite import RuntimeSelection
    from .carrier import Carrier, Deadline
    from .models import Command, Input, Output


@dataclass(frozen=True, slots=True)
class ResourceStartAcknowledgement:
    """Same-invocation acknowledgement, not continuing job or cleanup ownership."""

    reference: JobRef
    output_policy: ManagedOutputPolicy


@dataclass(frozen=True, slots=True, repr=False)
class ManagedResourceStartBinding:
    """Non-secret planned launch and its returned reservation evidence."""

    obligation_id: str
    receipt: ManagedRunReceipt
    output_policy: ManagedOutputPolicy

    @property
    def registration(self) -> tuple[str, str, int, bytes]:
        return (
            self.obligation_id,
            MANAGED_START_OBLIGATION_KIND,
            MANAGED_START_PAYLOAD_VERSION,
            encode_managed_start_obligation(self.receipt.identity.run_id),
        )


def capture_resource_start(
    operation: ExecutionOperation, active: _ActiveHelperCall, outcome: OwnedInlineOutcome
) -> None:
    """Resolve exact launch evidence only together with actual helper closure."""
    start = active.start
    assert start is not None
    repository = operation._managed_repository
    assert repository is not None
    record = repository.inspect(start.receipt.identity)
    if record is not None and (
        record.identity != start.receipt.identity
        or record.spec != start.receipt.spec
        or record.output_policy != start.output_policy
    ):
        raise StateError("Managed resource reservation binding changed")
    identity, kind, version, payload = start.registration
    row = operation._owner.inspect_lifecycle_obligation(identity)
    if row is None:
        if active.installed or active.armed or active.borrow.has_installed_dispatch_obligation:
            raise StateError("Managed start obligation disappeared")
    elif (row.obligation_kind, row.payload_version, row.payload, row.payload_revision) != (kind, version, payload, 0):
        raise StateError("Managed start obligation binding changed")
    candidate = active.candidate
    matching_ack = (
        isinstance(candidate, ManagedStartCandidate)
        and candidate.dispatch is Dispatch.SENT
        and candidate.carrier_completion == ExitStatus(0)
        and candidate.observation is not None
        and candidate.observation.state is ManagedStartState.ACKNOWLEDGED
        and candidate.observation.launch_fact == encode_managed_job_fact(start.receipt)
    )
    settled = not active.operation.requires_owner_retention
    if matching_ack and settled and record is not None and record.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH:
        record = repository.reconcile(record, ManagedLaunchObservation(Dispatch.SENT, start.receipt))
    acknowledged = matching_ack and record is not None and record.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    # This is the actual wrapper's pre-entry control path, not receipt absence.
    # A lost begin reply must first reconcile its exact real owner/attempt.
    # A carrier return lost before candidate publication still has its actual
    # outstanding attempt and cannot satisfy this proof. Keep POSSIBLE_DISPATCH
    # unchanged: resolving local START custody is not launch reconciliation.
    never_entered = (
        candidate is None
        and settled
        and active.operation.outstanding_attempt is None
        and not active.borrow.has_outstanding_attempt
        and not active.operation.pending_remote_effects
        and not active.operation.coordination_uncertain
        and (
            record is None or record.launch_state in {ManagedLaunchState.RESERVED, ManagedLaunchState.POSSIBLE_DISPATCH}
        )
    )
    proved = settled and (acknowledged or never_entered)
    active.outcome = replace(outcome, requires_owner_retention=outcome.requires_owner_retention or not proved)
    active.bookkeeping_retained = True
    if not proved:
        return
    if not operation._borrow_closed(active):
        if not active.installed:
            operation._recover_registration(active)
        active.borrow.close()
    if row is not None and row.state is not LifecycleObligationState.RESOLVED:
        LifecycleObligation(operation._owner, row).resolve()
    operation._active_inline_calls.pop(active.tracking_key, None)


def start_resource_job(
    operation: ExecutionOperation,
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
    """Freeze, retain actual custody, reserve and launch once without adopting the job."""
    operation.require_job_binding(carrier, runtime_selection)
    binding, repository, bootstrap = operation._native_binding, operation._managed_repository, operation._bootstrap
    if (
        binding is None
        or repository is None
        or bootstrap is None
        or operation._resource_owner is None
        or binding._independent_availability is not _IndependentJobAvailability.NO_IDLE_STOP
    ):
        raise StateError("Independent managed execution availability is not established")
    operation._require_managed_context(
        repository=repository,
        owner=operation._owner,
        target=operation._target,
        guest=bootstrap.guest,
        root_plan=bootstrap.root_entry,
        runtime_selection=runtime_selection,
    )
    identity = ManagedRunIdentity(uuid4().hex)
    reference = JobRef(identity.run_id)
    try:
        user_default = isinstance(invocation, Script) and invocation.shell is Shell.USER_DEFAULT
        shell = (
            ManagedShellIdentity(Shell.USER_DEFAULT, "/bin/sh", invocation.login, invocation.interactive)
            if user_default and isinstance(invocation, Script)
            else _explicit_managed_shell(invocation)
        )
        spec = ManagedRunSpec(
            operation._target, _validate_plan(plan), shell, operation._resource_owner, ManagedRunLifetime.INDEPENDENT
        )
        body: _ManagedBody | None = compose_managed_body(
            invocation, input=input, output=output, env=env, cwd=cwd, sensitive=sensitive, identity=identity, spec=spec
        )
        if user_default:
            resolved = operation._observe_workload_shell(carrier, plan, runtime_selection, deadline)
            spec = replace(spec, shell=replace(shell, resolved_executable=resolved))
            receipt = ManagedRunReceipt(identity, identity.unit_name, spec)
            assert body is not None
            body = replace(body, request=replace(body.request, launch=encode_managed_job_fact(receipt)))
        operation.require_job_binding(carrier, runtime_selection)
        assert body is not None
        action = ManagedResourceStartBinding(
            uuid4().hex, ManagedRunReceipt(identity, identity.unit_name, spec), body.output_policy
        )
        with operation._admission_guard:
            if (
                operation._finishing
                or operation._finished
                or operation._active_inline_calls
                or operation._unfinished_inline_executions
            ):
                raise StateError("Execution operation cannot admit a managed resource start")
            active = operation._borrow_helper_call(carrier, None, start=action)
        prepared = None
        try:
            assert body is not None
            prepared = prepare_managed_start(
                carrier,
                identity,
                spec,
                body.output_policy,
                body.request,
                plan=bootstrap.root_entry,
                deadline=deadline,
                runtime_selection=runtime_selection,
                guest=bootstrap.guest,
            )
            reserved = repository.reserve(spec, identity=identity, output_policy=body.output_policy)
            body = None
            prepared.claim(reserved, carrier, deadline)
            operation._admit(active)

            def require_delivery_admission() -> None:
                operation.require_job_binding(carrier, runtime_selection)
                if operation._wsl2_route is not None:
                    operation._wsl2_route.require_selected_route(deadline)

            def publish_candidate(candidate: ManagedStartCandidate) -> None:
                active.candidate = candidate

            start_borrowed_managed_run(
                repository,
                reserved,
                active.operation,
                prepared=prepared,
                deadline=deadline,
                before_possible_dispatch=require_delivery_admission,
                before_delivery=require_delivery_admission,
                publish_candidate=publish_candidate,
            )
            with operation._admission_guard:
                operation._capture(
                    active, operation._outcome(active, active.operation, deadline, include_candidate=False)
                )
            if active.outcome is None or active.outcome.requires_owner_retention:
                raise StateError("Independent managed start was not acknowledged")
            return ResourceStartAcknowledgement(reference, action.output_policy)
        except BaseException as control:
            operation._raise_control(control, active, active.operation, deadline)
            raise AssertionError("Unreachable managed resource start control return") from None
        finally:
            body = None
            if prepared is not None:
                prepared.discard()
    except BaseException as control:
        from ._execution_operation import ManagedExecutionControlFact

        try:
            fact = ManagedExecutionControlFact(reference)
            fact.__cause__ = control.__cause__
        except BaseException:
            raise control from control.__cause__
        raise control from fact
