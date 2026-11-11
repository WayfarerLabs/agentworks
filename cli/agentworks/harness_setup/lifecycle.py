"""Owning-lifecycle composition of native setup inputs and invocations.

Prepare before the command's single secret-resolution boundary. Execute only
with those prepared inputs and values, under the core mutation's existing guard.
Install-command transports remain independent of this setup environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import agentworks.execution as execution
from agentworks.artifacts.state import capture_owner, read_captures
from agentworks.capabilities.harness_integration.setup import (
    SetupInvocation,
    UserSetupInvocation,
    VMSetupInvocation,
    WorkspaceSetupInvocation,
)
from agentworks.env import ResourceContext
from agentworks.errors import StateError
from agentworks.execution.access import Admin, Agent
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.state import read_native_setup
from agentworks.linux_users import managed_home
from agentworks.package_sources import PackageCapture
from agentworks.secrets.orchestration import SecretTarget
from agentworks.vms.sites import site_platform_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.artifacts.declarations import ArtifactsConfig
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.db.instance_state import InstanceKind
    from agentworks.execution.files import Files
    from agentworks.execution.requests import RunRequest
    from agentworks.execution.results import Result
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.harness_setup.model import NativeSetupState, SetupComponent
    from agentworks.harness_setup.outcomes import SetupFailure
    from agentworks.resources.registry import Registry
    from agentworks.schema import CapabilityConfig
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.templates import ResolvedVMTemplate
    from agentworks.workspaces.templates import ResolvedTemplate


class SetupLog(Protocol):
    """What native setup reads of the owning operation's log (``SSHLogger`` satisfies it)."""

    @property
    def redactions(self) -> tuple[str, ...]: ...

    def warning(self, msg: str) -> None: ...

    def record(self, request: RunRequest, result: Result) -> None: ...


def _needed(
    db: Database,
    kind: InstanceKind,
    name: str,
    component: SetupComponent,
    activations: Mapping[str, CapabilityConfig],
    artifacts: ArtifactsConfig,
) -> bool:
    return (
        bool(activations)
        or bool(artifacts.bundles)
        or component in read_captures(db, kind, name)
        or any(item.component == component for item in read_native_setup(db, kind, name).records)
    )


def require_prepared_setup(
    db: Database,
    kind: InstanceKind,
    name: str,
    components: tuple[SetupComponent, ...],
    inputs: tuple[SetupInputs, ...],
) -> None:
    """Under the mutation guard, refuse applied-state records added after an unused plan.

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
    """Capture VM and actual-admin inputs at one revision, retaining separate owners."""
    result = []
    with PackageCapture() as operation:
        if _needed(db, "vm", name, "vm", template.harness_integrations, template.artifacts):
            result.append(
                SetupInputs(
                    "vm",
                    name,
                    "vm",
                    template.harness_integrations,
                    SecretTarget(vm=template.env),
                    template.artifacts,
                    capture_owner(registry, "vm", name, "vm", template.artifacts, operation=operation),
                )
            )
        if _needed(db, "vm", name, "admin", admin.active_harness_integrations, admin.artifacts):
            result.append(
                SetupInputs(
                    "vm",
                    name,
                    "admin",
                    admin.active_harness_integrations,
                    SecretTarget(vm=template.env, admin=admin.env),
                    admin.artifacts,
                    capture_owner(registry, "vm", name, "admin", admin.artifacts, operation=operation),
                )
            )
    from agentworks.interpolation import context_values

    row = db.get_vm(name)
    values = context_values(vm_name=name, user=row.admin_username if row else admin.username)
    for inputs in result:
        inputs.environment_target.validate_context(values)
    return tuple(result)


def prepare_agent_setup(
    db: Database, registry: Registry, *, vm: VMRow, name: str, template: ResolvedAgentTemplate
) -> SetupInputs | None:
    """Prepare this agent's VM + user chain, without admin or workspace env."""
    if not _needed(db, "agent", name, "agent", template.harness_integrations, template.artifacts):
        return None
    from agentworks.vms.templates import resolve_live_template

    ancestor = resolve_live_template(db, registry, vm.name, vm.template)
    inputs = SetupInputs(
        "agent",
        name,
        "agent",
        template.harness_integrations,
        SecretTarget(vm=ancestor.env, agent=template.env),
        template.artifacts,
        capture_owner(registry, "agent", name, "agent", template.artifacts),
    )
    from agentworks.agents.manager import derive_linux_user
    from agentworks.interpolation import context_values

    row = db.get_agent(name)
    inputs.environment_target.validate_context(
        context_values(
            vm_name=vm.name,
            user=row.linux_user if row else derive_linux_user(name),
            agent_name=name,
        )
    )
    return inputs


def prepare_workspace_setup(
    db: Database, registry: Registry, *, vm: VMRow, name: str, template: ResolvedTemplate, root: str | None = None
) -> SetupInputs | None:
    """Prepare this project's VM + workspace chain, without either user env."""
    if not _needed(db, "workspace", name, "workspace", template.harness_integrations, template.artifacts):
        return None
    from agentworks.vms.templates import resolve_live_template

    ancestor = resolve_live_template(db, registry, vm.name, vm.template)
    inputs = SetupInputs(
        "workspace",
        name,
        "workspace",
        template.harness_integrations,
        SecretTarget(vm=ancestor.env, workspace=template.env),
        template.artifacts,
        capture_owner(registry, "workspace", name, "workspace", template.artifacts),
    )
    from agentworks.interpolation import context_values

    inputs.environment_target.validate_context(
        context_values(
            vm_name=vm.name,
            user=vm.admin_username,
            workspace_name=name,
            workspace_dir=root,
        )
    )
    return inputs


def apply_vm_setup(
    db: Database,
    files: Files,
    registry: Registry,
    *,
    inputs: tuple[SetupInputs, ...],
    vm: VMRow,
    values: Mapping[str, str],
    logger: SetupLog,
    held: NativeMutationGuard,
    operation: str,
    failures: list[SetupFailure],
) -> None:
    """Apply VM then admin setup as the admin, on Phase B's ``files`` and its access.

    ``files`` carries Phase B's bound, so every setup request has the one
    create and reinit give each of their own.
    """
    access = files.access
    for item in inputs:
        context = ResourceContext(
            vm_name=vm.name, platform=site_platform_name(vm.site, registry), site=vm.site, user=vm.admin_username
        )
        environment = item.environment(values, context)
        if item.component == "vm":
            invocation: SetupInvocation = VMSetupInvocation(
                vm=vm,
                access=access,
                files=files,
                environment=environment,
                secrets=values,
                redactions=logger.redactions,
                prior=None,
                checkpoint=lambda claims: None,
            )
        else:
            invocation = UserSetupInvocation(
                vm=vm,
                access=access,
                files=files,
                environment=environment,
                secrets=values,
                redactions=logger.redactions,
                prior=None,
                checkpoint=lambda claims: None,
                username=vm.admin_username,
                home=managed_home(vm.admin_username),
            )
        run_setup(
            db,
            registry,
            item,
            invocation,
            operation=operation,
            failures=failures,
            held=held,
            log_warning=logger.warning,
        )


def apply_agent_setup(
    db: Database,
    config: Config,
    registry: Registry,
    *,
    inputs: SetupInputs,
    vm: VMRow,
    username: str,
    values: Mapping[str, str],
    logger: SetupLog,
    held: NativeMutationGuard,
    operation: str,
    failures: list[SetupFailure],
    buffered: bool = False,
) -> NativeSetupState:
    """Execute as the actual agent, after core establishes its SSH access."""
    context = ResourceContext(
        vm_name=vm.name,
        platform=site_platform_name(vm.site, registry),
        site=vm.site,
        user=username,
        agent_name=inputs.name,
    )
    environment = inputs.environment(values, context)
    access = execution.vm_access(vm, config, Agent(inputs.name, username), log=logger.record)
    invocation = UserSetupInvocation(
        vm=vm,
        access=access,
        files=access.files(),
        environment=environment,
        secrets=values,
        redactions=logger.redactions,
        prior=None,
        checkpoint=lambda claims: None,
        username=username,
        home=managed_home(username),
    )
    return run_setup(
        db,
        registry,
        inputs,
        invocation,
        operation=operation,
        failures=failures,
        buffered=buffered,
        held=held,
        log_warning=logger.warning,
    )


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
    logger: SetupLog,
    held: NativeMutationGuard,
    operation: str,
    failures: list[SetupFailure],
    buffered: bool = True,
) -> NativeSetupState:
    """Execute project setup as admin before publishing the new workspace row."""
    context = ResourceContext(
        vm_name=vm.name,
        platform=site_platform_name(vm.site, registry),
        site=vm.site,
        user=vm.admin_username,
        workspace_name=inputs.name,
        workspace_dir=root,
    )
    environment = inputs.environment(values, context)
    access = execution.vm_access(vm, config, Admin(), log=logger.record)
    invocation = WorkspaceSetupInvocation(
        vm=vm,
        access=access,
        files=access.files(),
        environment=environment,
        secrets=values,
        redactions=logger.redactions,
        prior=None,
        checkpoint=lambda claims: None,
        workspace_name=inputs.name,
        root=root,
        linux_group=linux_group,
    )
    return run_setup(
        db,
        registry,
        inputs,
        invocation,
        operation=operation,
        failures=failures,
        buffered=buffered,
        held=held,
        log_warning=logger.warning,
    )
