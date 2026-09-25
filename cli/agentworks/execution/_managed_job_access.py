"""Private composition of one independent managed start under an owned VM.

The caller supplies an already composed target identity. This module does not
recheck its marker or boot fence; a production caller must hold the route and
revalidate both at the dispatch boundary before using this seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.db import OperationResourceKind
from agentworks.errors import ValidationError
from agentworks.operations import OperationOwner, release_borrow_after_custody

from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._helper_identity import IdentityExpectation
from ._helper_launcher import IdentityPlan, _validate_plan
from ._managed_request_adapter import compose_managed_request
from ._managed_runs import (
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunRepository,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from ._managed_start_exchange import prepare_managed_start
from ._managed_start_operation import ManagedStartOutcome, start_owned_managed_run
from ._runtime_prerequisite import RuntimePrerequisiteState, RuntimeSelection, RuntimeTargetOS
from ._workload_shell import WorkloadShellObservationResult, WorkloadShellObservationState, observe_workload_shell
from .carrier import Carrier, Deadline, Dispatch, ExitStatus
from .models import Command, Input, Output, Script, Shell

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True, repr=False)
class ManagedJobShellFact:
    """Safe pre-reservation shell evidence and retained operation custody."""

    observation: WorkloadShellObservationResult | None = field(default=None, repr=False)
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class ManagedJobShellRefusal(Exception):
    """The destination account shell was not proved before reservation."""

    def __init__(self, fact: ManagedJobShellFact) -> None:
        self.fact = fact
        super().__init__("managed job shell was not resolved")


def start_bound_managed_job(
    repository: ManagedRunRepository,
    invocation: Command | Script,
    *,
    target: ManagedTargetIdentity,
    workload: IdentityExpectation,
    workload_plan: IdentityPlan,
    root_plan: IdentityPlan,
    run_owner: ManagedRunOwner,
    input: Input,
    output: Output,
    env: Mapping[str, str] | None,
    cwd: str | None,
    sensitive: bool,
    carrier: Carrier,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
    obligation_id: str,
    identity: ManagedRunIdentity,
) -> ManagedStartOutcome:
    """Preflight, reserve and attempt one start under an already owned exact VM.

    This is a private composition seam, not target activation or public execution.
    The caller must supply current target facts and retain custody on uncertain
    outcomes. A failed shell observation raises safe typed facts before reserve.
    """
    if (
        type(target) is not ManagedTargetIdentity
        or target.kind is not ManagedTargetKind.VM
        or type(workload) is not IdentityExpectation
        or type(run_owner) is not ManagedRunOwner
        or run_owner.kind is not ManagedRunOwnerKind.RESOURCE
        or type(identity) is not ManagedRunIdentity
        or type(obligation_id) is not str
        or len(obligation_id) != 32
        or any(character not in "0123456789abcdef" for character in obligation_id)
        or type(deadline) is not Deadline
        or deadline.expires_at is None
        or deadline.expired
        or type(runtime_selection) is not RuntimeSelection
        or runtime_selection.target_os is not RuntimeTargetOS.LINUX
        or not isinstance(owner, OperationOwner)
        or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
        or owner.ownership.scope.resource_name != target.name
    ):
        raise ValidationError("Managed job requires an owned exact Linux VM and finite deadline")
    if _validate_plan(workload_plan) != workload or _validate_plan(root_plan).euid != 0:
        raise ValidationError("Managed job identity plans do not match workload and root")

    if not isinstance(invocation, Command | Script):
        raise ValidationError("Managed execution requires a command or script")
    if isinstance(invocation, Script):
        if invocation.shell is Shell.USER_DEFAULT:
            provisional_spec = ManagedRunSpec(
                target,
                workload,
                ManagedShellIdentity(Shell.USER_DEFAULT, "/bin/sh", invocation.login, invocation.interactive),
                run_owner,
                ManagedRunLifetime.INDEPENDENT,
            )
            compose_managed_request(
                invocation,
                input=input,
                output=output,
                env=env,
                cwd=cwd,
                sensitive=sensitive,
                identity=identity,
                spec=provisional_spec,
            )
            borrow = owner.borrow()
            operation = BorrowedFixedHelperCarrier(carrier, borrow)
            result: WorkloadShellObservationResult | None = None
            try:
                result = observe_workload_shell(
                    operation, plan=workload_plan, runtime_selection=runtime_selection, deadline=deadline
                )
                operation.settle(result.dispatch, result.carrier_completion)
            except BaseException as control:
                retained = operation.requires_owner_retention
                release_failed = False
                try:
                    release_borrow_after_custody(borrow, retain_effect=retained)
                except BaseException:
                    release_failed = True
                fact = ManagedJobShellFact(
                    result,
                    operation.pending_remote_effects,
                    operation.coordination_uncertain or operation.has_outstanding_attempt or release_failed,
                    retained or release_failed,
                )
                raise control from ManagedJobShellRefusal(fact)
            retained = operation.requires_owner_retention
            release_failed = False
            try:
                release_borrow_after_custody(borrow, retain_effect=retained)
            except BaseException:
                release_failed = True
            fact = ManagedJobShellFact(
                result,
                operation.pending_remote_effects,
                operation.coordination_uncertain or release_failed,
                retained or release_failed,
            )
            observed = result.observation
            if (
                result.dispatch is not Dispatch.SENT
                or result.carrier_completion != ExitStatus(code=0)
                or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
                or observed is None
                or observed.state is not WorkloadShellObservationState.RESOLVED
                or observed.shell is None
                or fact.requires_owner_retention
            ):
                raise ManagedJobShellRefusal(fact)
            resolved = observed.shell
        else:
            resolved = {Shell.SH: "/bin/sh", Shell.BASH: "/bin/bash"}[invocation.shell]
        shell = ManagedShellIdentity(invocation.shell, resolved, invocation.login, invocation.interactive)
    else:
        shell = ManagedShellIdentity(None, None)

    spec = ManagedRunSpec(target, workload, shell, run_owner, ManagedRunLifetime.INDEPENDENT)
    request, policy = compose_managed_request(
        invocation, input=input, output=output, env=env, cwd=cwd, sensitive=sensitive, identity=identity, spec=spec
    )
    prepared = prepare_managed_start(carrier, identity, spec, policy, request, root_plan, deadline, runtime_selection)
    try:
        reserved = repository.reserve(spec, output_policy=policy, identity=identity)
    except BaseException:
        prepared.discard()
        raise
    return start_owned_managed_run(
        repository,
        reserved,
        carrier,
        prepared=prepared,
        deadline=deadline,
        owner=owner,
        obligation_id=obligation_id,
    )
