"""Owning-lifecycle composition of native setup inputs and invocations.

Prepare before the command's single secret-resolution boundary. Execute only
with those prepared inputs and values, under the core mutation's existing guard.
Install-command transports remain independent of this setup environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.harness_integration.setup import (
    SetupInvocation,
    UserSetupInvocation,
    VMSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.env import ResourceContext
from agentworks.errors import StateError
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.runner import SetupRunner
from agentworks.harness_setup.state import read_native_setup, write_native_setup
from agentworks.secrets.orchestration import SecretTarget
from agentworks.vms.sites import site_platform_name

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.db.instance_state import InstanceKind
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.harness_setup.model import NativeSetupState, SetupComponent
    from agentworks.resources.registry import Registry
    from agentworks.schema import CapabilityBlock
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.templates import ResolvedVMTemplate
    from agentworks.workspaces.templates import ResolvedTemplate


def _needed(
    db: Database, kind: InstanceKind, name: str, component: SetupComponent, attachments: Sequence[CapabilityBlock]
) -> bool:
    return bool(attachments) or any(item.component == component for item in read_native_setup(db, kind, name).records)


def require_prepared_setup(
    db: Database,
    kind: InstanceKind,
    name: str,
    components: tuple[SetupComponent, ...],
    inputs: tuple[SetupInputs, ...],
) -> None:
    """Under the mutation guard, refuse receipts added after an unused plan.

    Active plans can read newer prior claims in dispatch. A previously skipped
    component has no prepared env, so it must retry at the eager boundary.
    """
    prepared = {item.component for item in inputs}
    if any(
        record.component in components and record.component not in prepared
        for record in read_native_setup(db, kind, name).records
    ):
        raise StateError(
            "native setup changed while this operation was preparing",
            entity_kind=kind,
            entity_name=name,
            hint="Retry the operation to prepare its current setup inputs.",
        )


def prepare_vm_setup(
    db: Database, registry: Registry, *, name: str, template: ResolvedVMTemplate, admin: AdminConfig
) -> tuple[SetupInputs, ...]:
    """Prepare VM and actual-admin inputs independently, including retirement."""
    result = []
    if _needed(db, "vm", name, "vm", template.harness_integrations):
        result.append(
            SetupInputs("vm", name, "vm", tuple(template.harness_integrations), SecretTarget(vm=template.env))
        )
    if _needed(db, "vm", name, "admin", admin.harness_integrations):
        result.append(
            SetupInputs(
                "vm", name, "admin", tuple(admin.harness_integrations), SecretTarget(vm=template.env, admin=admin.env)
            )
        )
    return tuple(result)


def prepare_agent_setup(
    db: Database, registry: Registry, *, vm: VMRow, name: str, template: ResolvedAgentTemplate
) -> SetupInputs | None:
    """Prepare this agent's VM + user chain, without admin or workspace env."""
    if not _needed(db, "agent", name, "agent", template.harness_integrations):
        return None
    from agentworks.vms.templates import resolve_live_template

    ancestor = resolve_live_template(db, registry, vm.name, vm.template)
    return SetupInputs(
        "agent", name, "agent", tuple(template.harness_integrations), SecretTarget(vm=ancestor.env, agent=template.env)
    )


def prepare_workspace_setup(
    db: Database, registry: Registry, *, vm: VMRow, name: str, template: ResolvedTemplate
) -> SetupInputs | None:
    """Prepare this project's VM + workspace chain, without either user env."""
    if not _needed(db, "workspace", name, "workspace", template.harness_integrations):
        return None
    from agentworks.vms.templates import resolve_live_template

    ancestor = resolve_live_template(db, registry, vm.name, vm.template)
    return SetupInputs(
        "workspace",
        name,
        "workspace",
        tuple(template.harness_integrations),
        SecretTarget(vm=ancestor.env, workspace=template.env),
    )


def apply_vm_setup(
    db: Database,
    registry: Registry,
    *,
    inputs: tuple[SetupInputs, ...],
    vm: VMRow,
    target: Transport,
    values: Mapping[str, str],
    held: NativeMutationGuard,
    operation: str,
) -> None:
    """Apply VM then admin setup on the already-established admin transport."""
    for item in inputs:
        context = ResourceContext(
            vm_name=vm.name, platform=site_platform_name(vm.site, registry), site=vm.site, user=vm.admin_username
        )
        environment = item.environment(values, context)
        runner = SetupRunner(target, environment)
        if item.component == "vm":
            invocation: SetupInvocation = VMSetupInvocation(
                vm=vm,
                runner=runner,
                environment=environment,
                secrets=values,
                prior=None,
                checkpoint=lambda claims: None,
            )
        else:
            invocation = UserSetupInvocation(
                vm=vm,
                runner=runner,
                environment=environment,
                secrets=values,
                prior=None,
                checkpoint=lambda claims: None,
                username=vm.admin_username,
                home=f"/home/{vm.admin_username}",
            )
        run_setup(db, registry, item, invocation, operation=operation, held=held)


def apply_agent_setup(
    db: Database,
    config: Config,
    registry: Registry,
    *,
    inputs: SetupInputs,
    vm: VMRow,
    username: str,
    values: Mapping[str, str],
    logger: SSHLogger,
    held: NativeMutationGuard,
    operation: str,
    buffered: bool = False,
) -> NativeSetupState:
    """Execute as the actual agent, after core establishes its SSH access."""
    from agentworks.transports import transport_for_user

    context = ResourceContext(
        vm_name=vm.name,
        platform=site_platform_name(vm.site, registry),
        site=vm.site,
        user=username,
        agent_name=inputs.name,
    )
    environment = inputs.environment(values, context)
    runner = SetupRunner(transport_for_user(vm, config, user=username, logger=logger), environment)
    invocation = UserSetupInvocation(
        vm=vm,
        runner=runner,
        environment=environment,
        secrets=values,
        prior=None,
        checkpoint=lambda claims: None,
        username=username,
        home=f"/home/{username}",
    )
    return run_setup(db, registry, inputs, invocation, operation=operation, buffered=buffered, held=held)


def apply_workspace_setup(
    db: Database,
    config: Config,
    registry: Registry,
    *,
    inputs: SetupInputs,
    vm: VMRow,
    root: str,
    linux_group: str,
    values: Mapping[str, str],
    logger: SSHLogger,
    held: NativeMutationGuard,
    operation: str,
    buffered: bool = True,
) -> NativeSetupState:
    """Execute project setup as admin before publishing the new workspace row."""
    from agentworks.transports import transport

    context = ResourceContext(
        vm_name=vm.name,
        platform=site_platform_name(vm.site, registry),
        site=vm.site,
        user=vm.admin_username,
        workspace_name=inputs.name,
        workspace_dir=root,
    )
    environment = inputs.environment(values, context)
    runner = SetupRunner(transport(vm, config, logger=logger), environment)
    invocation = WorkspaceSetupInvocation(
        vm=vm,
        runner=runner,
        environment=environment,
        secrets=values,
        prior=None,
        checkpoint=lambda claims: None,
        workspace_name=inputs.name,
        root=root,
        linux_group=linux_group,
    )
    return run_setup(db, registry, inputs, invocation, operation=operation, buffered=buffered, held=held)


def mark_cleanup_pending(db: Database, kind: InstanceKind, name: str, *, operation: str) -> None:
    """Retain the latest confirmed prefix when owner deletion cannot complete."""
    from agentworks.harness_setup.model import NativeSetupState

    try:
        state = read_native_setup(db, kind, name)
    except StateError:
        # Unknown or malformed evidence remains untouched. The failed deletion
        # still preserves its owner, and a later compatible reader can recover it.
        return
    if state.records:
        pending = NativeSetupState(
            records=tuple(
                record.model_copy(update={"complete": False, "pending_cleanup": True}) for record in state.records
            )
        )
        write_native_setup(db, kind, name, pending, operation=operation)


def retire_owner_setup(
    db: Database,
    config: Config,
    *,
    kind: InstanceKind,
    name: str,
    vm: VMRow | None,
    logger: SSHLogger,
    held: NativeMutationGuard,
    registry: Registry | None = None,
    username: str | None = None,
    root: str | None = None,
    linux_group: str | None = None,
) -> None:
    """Retire owned user/project claims using absent desired config, never emit it.

    Deletion does not consume current config or environment. Retained settings
    mappings relinquish their claims through integration retirement. Other
    remaining claims prevent owner destruction and preserve recovery evidence.
    """
    state = read_native_setup(db, kind, name)
    if not state.records:
        return
    operation = f"{kind}-delete"
    mark_cleanup_pending(db, kind, name, operation=operation)
    if vm is None:
        raise StateError(
            "native setup cleanup needs its owning VM",
            entity_kind=kind,
            entity_name=name,
            hint="Restore access to the owning VM before retrying deletion. Ownership records were retained.",
        )
    if not any(record.claims for record in state.records):
        return
    if registry is None:
        from agentworks.bootstrap import load_request_registry

        registry = load_request_registry(config, live_database=db)
    if kind == "agent":
        assert username is not None
        inputs = SetupInputs("agent", name, "agent", (), SecretTarget(vm={}))
        result = apply_agent_setup(
            db,
            config,
            registry,
            inputs=inputs,
            vm=vm,
            username=username,
            values={},
            logger=logger,
            held=held,
            operation=operation,
        )
    elif kind == "workspace":
        assert root is not None and linux_group is not None
        inputs = SetupInputs("workspace", name, "workspace", (), SecretTarget(vm={}))
        result = apply_workspace_setup(
            db,
            config,
            registry,
            inputs=inputs,
            vm=vm,
            root=root,
            linux_group=linux_group,
            values={},
            logger=logger,
            held=held,
            operation=operation,
            buffered=False,
        )
    else:
        raise TypeError("owner retirement requires an agent or workspace")
    if any(record.claims for record in result.records):
        raise StateError(
            "native setup cleanup remains incomplete",
            entity_kind=kind,
            entity_name=name,
            hint="Restore the integration and native destination, then retry deletion. Records were retained.",
        )


def vm_family_setup_owners(db: Database, vm_name: str) -> tuple[tuple[InstanceKind, str], ...]:
    """Find recorded guest-native domains, preserving unknown payload versions."""
    owners: list[tuple[InstanceKind, str]] = [("vm", vm_name)]
    owners.extend(("agent", agent.name) for agent in db.list_agents(vm_name=vm_name))
    owners.extend(("workspace", workspace.name) for workspace in db.list_workspaces(vm_name=vm_name))
    recorded = []
    for kind, name in owners:
        try:
            state = read_native_setup(db, kind, name)
        except ValueError:
            # Invalid legacy names cannot carry supported instance-state ownership.
            continue
        except StateError:
            recorded.append((kind, name))
        else:
            if state.records:
                recorded.append((kind, name))
    return tuple(recorded)


def require_workspace_rehome_supported(db: Database, name: str) -> None:
    """Native ownership has no relocation contract; preserve its current root."""
    try:
        recorded = bool(read_native_setup(db, "workspace", name).records)
    except StateError:
        recorded = True
    if recorded:
        raise StateError(
            "cannot rehome a workspace with native setup evidence",
            entity_kind="workspace",
            entity_name=name,
            hint="Preserve the workspace contents, delete the old workspace with its setup cleanup, "
            "then create a workspace at the new location. Native receipts cannot be relocated.",
        )
