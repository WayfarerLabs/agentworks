"""Private WSL2 hold and independent managed-start composition."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.execution._managed_job_access import start_bound_managed_job
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.vms.target_preparation import VMTargetPreparationStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._managed_runs import ManagedRunIdentity, ManagedRunOwner, ManagedRunRepository
    from agentworks.execution._managed_start_operation import ManagedStartOutcome
    from agentworks.execution.carrier import Deadline
    from agentworks.execution.models import Command, Input, Output, Script


class WSL2ManagedStartStatus(StrEnum):
    ATTEMPTED = "attempted"
    REFUSED = "refused"
    RETAINED = "retained"


class WSL2OwnedManagedJob(WSL2OwnedOperation):
    """Hold one selected VM route through a caller-supplied managed start.

    The independent run does not extend the WSL hold. A caller explicitly
    releases the hold and owner after checking start custody and settling all
    obligations; successful start does not release either resource here.
    """

    _purpose = "managed-job"

    start_outcome: ManagedStartOutcome | None = None
    _start_invoked = False
    _start_control_uncertain = False

    def start_job(
        self,
        repository: ManagedRunRepository,
        invocation: Command | Script,
        *,
        workload_plan: IdentityPlan,
        root_plan: IdentityPlan,
        run_owner: ManagedRunOwner,
        input: Input,
        output: Output,
        env: Mapping[str, str] | None,
        cwd: str | None,
        sensitive: bool,
        deadline: Deadline,
        obligation_id: str,
        identity: ManagedRunIdentity,
    ) -> WSL2ManagedStartStatus:
        """Prepare one VM and invoke the bound start with the caller's run ID.

        Route revalidation immediately precedes the bound-start invocation,
        but cannot close a post-preparation, pre-dispatch route-change race.
        Escaping control flow retains this operation for caller reconciliation.
        """
        guest = self.start_and_prepare(deadline)
        preparation = self.preparation
        if preparation is None:
            return WSL2ManagedStartStatus.RETAINED
        if preparation.status is not VMTargetPreparationStatus.PREPARED:
            return self._refuse_or_retain(deadline, safe=not preparation.requires_owner_retention)
        if guest is None:
            return self._refuse_or_retain(deadline, safe=True)
        target = preparation.target
        assert target is not None
        if not self.selected_route_still_current(deadline):
            return self._refuse_or_retain(deadline, safe=True)
        self._start_invoked = True
        try:
            self.start_outcome = start_bound_managed_job(
                repository,
                invocation,
                target=target,
                workload_plan=workload_plan,
                root_plan=root_plan,
                run_owner=run_owner,
                input=input,
                output=output,
                env=env,
                cwd=cwd,
                sensitive=sensitive,
                carrier=self._carrier,
                runtime_selection=self._runtime,
                deadline=deadline,
                owner=self.owner,
                obligation_id=obligation_id,
                identity=identity,
                guest=guest,
            )
        except BaseException:
            self._start_control_uncertain = True
            raise
        return WSL2ManagedStartStatus.ATTEMPTED

    def release_if_settled(self, deadline: Deadline, *, safe: bool) -> bool:
        """Settle the hold independently; close only when start custody permits."""
        if not self._release_exact_hold(deadline, safe=safe):
            return False
        outcome = self.start_outcome
        start_safe = not self._start_control_uncertain and (
            not self._start_invoked or outcome is not None and not outcome.requires_owner_retention
        )
        return start_safe and self._close_settled_owner()

    def _refuse_or_retain(self, deadline: Deadline, *, safe: bool) -> WSL2ManagedStartStatus:
        return (
            WSL2ManagedStartStatus.REFUSED
            if self.release_if_settled(deadline, safe=safe)
            else WSL2ManagedStartStatus.RETAINED
        )
