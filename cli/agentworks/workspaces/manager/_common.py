"""Shared helpers for the workspace lifecycle commands.

VM resolution, the VM-status guard, and the WORKSPACE-level operation scope
are used across create/reinit/rehome/delete/copy, so they live here rather
than in any one of those submodules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import InitStatus
from agentworks.errors import NotFoundError, StateError

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope
    from agentworks.db import Database, VMRow


def _workspace_scope(db: Database, vm: VMRow, ws_name: str) -> OperationScope:
    """The workspace commands' shared WORKSPACE-level operation scope:
    the operation is about the workspace (on this VM), even when the
    composed graph is the live VM alone. The WORKSPACE level's field
    rules (required vm + workspace; forbidden agent, session) are
    enforced by the scope's own constructor."""
    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.db import SYSTEM_SLUG_KEY

    return OperationScope(
        level=ScopeLevel.WORKSPACE,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm.name,
        workspace=ws_name,
    )


def _guard_vm_status(vm: VMRow) -> None:
    """Block operations on VMs that are not usable (failed or in-progress)."""
    usable = {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}
    if vm.init_status not in usable:
        if vm.init_status == InitStatus.FAILED.value:
            raise StateError(
                f"VM '{vm.name}' is in 'failed' state.",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Run 'vm delete' and recreate.",
            )
        else:
            raise StateError(
                f"VM '{vm.name}' initialization is not complete (status: {vm.init_status}).",
                entity_kind="vm",
                entity_name=vm.name,
            )


def _resolve_vm(db: Database, vm_name: str | None) -> VMRow:
    """Resolve which VM to use: explicit, auto-select if 1, or error."""
    if vm_name is not None:
        vm = db.get_vm(vm_name)
        if vm is None:
            raise NotFoundError(
                f"VM '{vm_name}' not found",
                entity_kind="vm",
                entity_name=vm_name,
            )
        return vm

    vms = db.list_vms()
    usable_statuses = {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}
    usable_vms = [v for v in vms if v.init_status in usable_statuses]

    if len(usable_vms) == 0:
        raise NotFoundError(
            "no VMs available.",
            entity_kind="vm",
            hint="Create one with 'agw vm create'.",
        )

    if len(usable_vms) == 1:
        output.info(f"Using VM '{usable_vms[0].name}'")
        return usable_vms[0]

    options = [f"{v.name}  ({v.site})" for v in usable_vms]
    idx = output.choose("Select a VM:", options)
    return usable_vms[idx]
