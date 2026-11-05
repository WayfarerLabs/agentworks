"""Teardown plumbing for the Proxmox platform: the stop-then-delete
sequence shared by the delete op and ``create``'s rollback arms.

The platform's only backend artifact is the cloned VM (one VMID on one
node), so teardown is a single sequence: settle any in-flight task,
stop the VM if running, delete the VMID. This module is the one place
that sequence lives; ``ProxmoxPlatform.delete`` and both rollback arms
of ``ProxmoxPlatform.create`` compose it (the azure plugin's
``network.delete_vm_and_resources`` is the structural precedent:
platform.py keeps the capability surface, a sibling module holds the
teardown mechanics).

``stop_and_delete_vm`` returns only once the VMID is gone and raises
otherwise: core drops a VM's row only when its delete returns (incident
row I18), and Proxmox has no azure-style tag sweep to find an orphan
later. ``ProxmoxAPI`` wraps every HTTP and transport failure as
``ProxmoxAPIError``, so an unreachable API is a failed delete like any
other. The two rollback wrappers contain every non-interrupt failure of
that sequence with an operator warning naming the node and VMID, and
hand the VMID to core as :class:`RetainedProvisioningError`, so the row
survives for ``vm delete`` to retry instead of leaving a running VM
nothing targets. The original failure stays in that error's message.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.base import RetainedProvisioningError
from agentworks.plugins.proxmox.api import ProxmoxAPIError

if TYPE_CHECKING:
    from agentworks.plugins.proxmox.api import ProxmoxAPI

# How long the rollback waits for a cancelled in-flight task to settle.
# Deliberately shorter than wait_for_task's 300s default: the task has
# been told to stop, so this only covers PVE tearing the worker down,
# not a full clone finishing.
_SETTLE_TIMEOUT = 60


def stop_and_delete_vm(api: ProxmoxAPI, node: str, vmid: int) -> None:
    """Stop the VM if it is running, then delete the VMID; return only once it is gone.

    Each step tolerates a VM already stopped, mid-teardown or gone, since
    the delete op is retried against partial prior failures: a failed stop
    still leaves the delete to decide, and a failed delete counts as done
    when the node no longer lists the VMID. Otherwise the delete's error
    propagates, a listing that fails included, since it cannot show the
    VMID gone. Shared by the delete op and the create rollback wrappers, so
    the stop-then-delete ordering lives in exactly one place.
    """
    with contextlib.suppress(ProxmoxAPIError):
        status = api.vm_status(node, vmid)
        if status.get("status") == "running":
            upid = api.stop_vm(node, vmid)
            api.wait_for_task(node, upid)
    try:
        upid = api.delete_vm(node, vmid)
        api.wait_for_task(node, upid)
    except ProxmoxAPIError:
        if not _vm_absent(api, node, vmid):
            raise


def _vm_absent(api: ProxmoxAPI, node: str, vmid: int) -> bool:
    """Whether the node's VM listing, which shows every VM the token can audit, lacks ``vmid``.

    A listing that fails cannot show the VMID gone, so it answers no.
    """
    try:
        listed = api.list_vms(node)
    except ProxmoxAPIError:
        return False
    return all(str(entry.get("vmid")) != str(vmid) for entry in listed)


def _settle_and_teardown(api: ProxmoxAPI, node: str, vmid: int, pending_upid: str | None) -> None:
    """The raw rollback sequence; raises freely, callers contain.

    ``pending_upid`` is the task ``create`` was still waiting on when it
    unwound (likeliest: the minutes-long full clone). A clone task in
    flight holds a lock on the target VMID, so the delete cannot succeed
    until it settles; cancelling it first settles it in seconds instead
    of waiting out the clone. Both settle steps tolerate any failure
    (broadly: a cancelled task reports a non-OK exit, which
    ``wait_for_task`` raises as its "Task failed" error and is the
    expected outcome here, and a settle failure must not skip the delete
    attempt). Only the VMID this create allocated is ever touched; the
    template is a different VMID and is never named here.
    """
    if pending_upid is not None:
        with contextlib.suppress(Exception):
            api.stop_task(node, pending_upid)
        with contextlib.suppress(Exception):
            api.wait_for_task(node, pending_upid, timeout=_SETTLE_TIMEOUT)
    stop_and_delete_vm(api, node, vmid)


def rollback_partial_create(
    api: ProxmoxAPI,
    node: str,
    vmid: int,
    *,
    vm_name: str,
    failure: Exception,
    pending_upid: str | None = None,
) -> None:
    """Tear down whatever a failed ``create`` made: the cloned VM under
    ``vmid``, and nothing else.

    The failure arm's wrapper. A rollback that cannot confirm the VMID
    gone warns naming it and raises :class:`RetainedProvisioningError`,
    whose message carries the original ``failure`` and whose metadata
    addresses the VMID, so core keeps the row and ``vm delete`` can
    finish the removal. A ``KeyboardInterrupt`` during this rollback
    escapes to ``create``'s outer interrupt arm, which restarts the
    cleanup under the full two-Ctrl-C interrupt protocol
    (:func:`rollback_create_on_interrupt`).
    """
    try:
        _settle_and_teardown(api, node, vmid, pending_upid)
    except Exception as cleanup_error:
        output.warn(f"Rollback incomplete: Proxmox VM {vmid} may remain on node '{node}'.")
        raise _retained(
            vm_name, node, vmid, f"create failed ({failure}), and removing the VM failed: {cleanup_error}"
        ) from cleanup_error


def rollback_create_on_interrupt(
    api: ProxmoxAPI, node: str, vmid: int, *, vm_name: str, pending_upid: str | None = None
) -> None:
    """Roll back the partially created VM after an operator interrupt
    inside ``ProxmoxPlatform.create``.

    The VM may be anywhere from mid-clone (the task still running) to
    fully up and mid-bootstrap, so this settles the pending task and
    runs the shared stop-then-delete. The cleanup never wedges: a SECOND
    interrupt during it abandons the remaining cleanup, and a failure
    (the API became unreachable, the VMID stayed locked) ends it the same
    way. Either warns naming the node and VMID and raises
    :class:`RetainedProvisioningError` in place of the interrupt, as the
    EC2 platform does, so core keeps the row and ``vm delete`` can finish
    the removal. A cleanup that completes returns, and the caller
    re-raises the ORIGINAL interrupt, which propagates to ``create_vm``,
    whose unwind then deletes the DB row it no longer needs.
    """
    output.warn(
        f"Interrupted: cleaning up the partial Proxmox VM {vmid} on node "
        f"'{node}', please wait (Ctrl-C again retains it for explicit deletion)..."
    )
    try:
        _settle_and_teardown(api, node, vmid, pending_upid)
    except (KeyboardInterrupt, Exception) as cleanup_error:
        # Both types on purpose: a delete that could not be confirmed
        # raises out of _settle_and_teardown as surely as a second Ctrl-C
        # interrupts it, and either leaves a VMID only the row can target.
        detail = "the cleanup was interrupted" if isinstance(cleanup_error, KeyboardInterrupt) else str(cleanup_error)
        output.warn(f"Cleanup abandoned: Proxmox VM {vmid} may remain on node '{node}'.")
        raise _retained(vm_name, node, vmid, f"create was interrupted, and removing the VM failed: {detail}") from (
            cleanup_error
        )


def _retained(vm_name: str, node: str, vmid: int, detail: str) -> RetainedProvisioningError:
    """The retained-row handoff: the row keeps the VMID, so ``vm delete`` can retry its removal."""
    return RetainedProvisioningError(
        f"Proxmox VM {vmid} on node '{node}' may remain: {detail}",
        platform_metadata={"vmid": str(vmid), "node": node},
        entity_name=vm_name,
        hint=(
            f"Once the cause is fixed, run 'agw vm delete {vm_name}' to remove the VM, "
            f"or delete VM {vmid} on node '{node}' in the Proxmox web UI first."
        ),
    )
