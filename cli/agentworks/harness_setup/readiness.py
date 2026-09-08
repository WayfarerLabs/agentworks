"""Read-only prerequisite checks before a session can replace its runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.harness_integration.setup import (
    SetupEvidence,
    SetupGap,
    SetupReadiness,
)
from agentworks.errors import StateError
from agentworks.harness_setup.dispatch import destination_id
from agentworks.harness_setup.state import read_native_setup
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.capabilities.harness_integration import HarnessIntegration
    from agentworks.capabilities.harness_integration.setup import SetupStatus
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.model import SetupFacet, SetupRecord
    from agentworks.resources.registry import Registry
    from agentworks.transports import Transport


class RequiredSetupMissingError(StateError):
    """A session's integration requires upstream setup that is not current."""


def evaluate_setup(
    db: Database,
    inputs: SetupInputs,
    integration_name: str,
    *,
    vm: VMRow,
    runner: Transport,
    remediation: str,
    location: str | None = None,
    username: str | None = None,
) -> SetupEvidence:
    """Compare declarations and actual placement without acquiring source files."""

    def result(status: SetupStatus, record: SetupRecord | None = None) -> SetupEvidence:
        return SetupEvidence(status, inputs.kind, inputs.name, remediation, record)

    try:
        state = read_native_setup(db, inputs.kind, inputs.name)
    except StateError:
        return result("unavailable")
    record = next(
        (item for item in state.records if item.component == inputs.component and item.integration == integration_name),
        None,
    )
    if record is None:
        return result("absent")
    if not record.complete or record.pending_cleanup:
        return result("incomplete", record)
    block = next((item for item in inputs.attachments if item.name == integration_name), None)
    if block is None or inputs.declaration(block) != record.declaration:
        return result("stale", record)
    try:
        if destination_id(vm, runner, location=location, username=username) != record.destination_id:
            return result("stale", record)
    except (SSHError, StateError):
        return result("unavailable", record)
    return result("current", record)


def require_setup_ready(
    db: Database,
    registry: Registry,
    integration: HarnessIntegration,
    *,
    vm: VMRow,
    workspace: WorkspaceRow,
    agent_name: str | None,
    runner: Transport,
) -> None:
    """Give the integration the actual bound owners and enforce returned severity."""

    def lookup(facet: SetupFacet) -> SetupEvidence:
        return _applicable_evidence(
            db,
            registry,
            integration.name,
            facet,
            vm=vm,
            workspace=workspace,
            agent_name=agent_name,
            runner=runner,
        )

    enforce_setup_gaps(integration.check_setup(SetupReadiness(lookup, runner)))


def _applicable_evidence(
    db: Database,
    registry: Registry,
    integration_name: str,
    facet: SetupFacet,
    *,
    vm: VMRow,
    workspace: WorkspaceRow,
    agent_name: str | None,
    runner: Transport,
) -> SetupEvidence:
    from agentworks.agents.templates import resolve_live_template as resolve_agent
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.secrets.orchestration import SecretTarget
    from agentworks.vms.admin_templates import resolve_live_template as resolve_admin
    from agentworks.vms.templates import resolve_live_template as resolve_vm
    from agentworks.workspaces.templates import resolve_live_template as resolve_workspace

    vm_template = resolve_vm(db, registry, vm.name, vm.template)
    inputs: SetupInputs
    location = None
    username = None
    if facet == "vm":
        inputs = SetupInputs(
            kind="vm",
            name=vm.name,
            component="vm",
            attachments=tuple(vm_template.harness_integrations),
            target=SecretTarget(vm=vm_template.env),
        )
        remedy = f"Enable the attachment and run 'agw vm reinit {vm.name}'."
    elif facet == "user":
        if agent_name is None:
            admin = resolve_admin(db, registry, vm.name, vm.admin_template)
            inputs = SetupInputs(
                kind="vm",
                name=vm.name,
                component="admin",
                attachments=tuple(admin.harness_integrations),
                target=SecretTarget(vm=vm_template.env, admin=admin.env),
            )
            username = vm.admin_username
            remedy = f"Enable the attachment and run 'agw vm reinit {vm.name}'."
        else:
            agent = db.get_agent(agent_name)
            if agent is None or agent.vm_name != vm.name:
                raise StateError("setup readiness requires the session's actual agent on this VM")
            template = resolve_agent(db, registry, agent.name, agent.template)
            inputs = SetupInputs(
                kind="agent",
                name=agent.name,
                component="agent",
                attachments=tuple(template.harness_integrations),
                target=SecretTarget(vm=vm_template.env, agent=template.env),
            )
            username = agent.linux_user
            remedy = f"Enable the attachment and run 'agw agent reinit {agent.name}'."
        location = f"/home/{username}"
    else:
        if workspace.vm_name != vm.name:
            raise StateError("setup readiness requires the session's workspace on this VM")
        project = resolve_workspace(db, registry, workspace.name, workspace.template)
        inputs = SetupInputs(
            kind="workspace",
            name=workspace.name,
            component="workspace",
            attachments=tuple(project.harness_integrations),
            target=SecretTarget(vm=vm_template.env, workspace=project.env),
        )
        location = workspace.workspace_path
        remedy = f"Inspect {workspace.workspace_path} and explicitly recreate workspace '{workspace.name}' with setup."
    return evaluate_setup(
        db,
        inputs,
        integration_name,
        vm=vm,
        runner=runner,
        location=location,
        username=username,
        remediation=remedy,
    )


def enforce_setup_gaps(gaps: tuple[SetupGap, ...]) -> None:
    """Validate plugin hook results before enforcing their prerequisite severity.

    Third-party integrations need not run our type checker; dataclass annotations
    alone cannot prevent an invalid severity from silently permitting launch.
    """
    for gap in gaps:
        if not isinstance(gap, SetupGap) or gap.severity not in ("required", "recommended"):
            raise StateError("harness integration returned an invalid setup prerequisite")
        if gap.severity == "required":
            raise RequiredSetupMissingError(
                gap.reason,
                entity_kind=gap.evidence.owner_kind,
                entity_name=gap.evidence.owner_name,
                hint=gap.evidence.remediation,
            )
        output.warn(f"{gap.reason}. {gap.evidence.remediation}")
