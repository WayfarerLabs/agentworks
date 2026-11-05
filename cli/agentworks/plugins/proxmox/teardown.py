"""Stop and delete the exact Proxmox node/VMID, shared by deletion and rollback.

Direct deletion returns only after provider-confirmed absence. Failed or
unobservable deletion raises, so core retains its row for retry. Create rollback
contains those failures and warns without replacing its original error or
interrupt. No other VMID or node is discovered or removed by this path.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.plugins.proxmox.api import ProxmoxAPIError, ProxmoxVMNotFoundError

if TYPE_CHECKING:
    from agentworks.plugins.proxmox.api import ProxmoxAPI

# How long the rollback waits for a cancelled in-flight task to settle.
# Deliberately shorter than wait_for_task's 300s default: the task has
# been told to stop, so this only covers PVE tearing the worker down,
# not a full clone finishing.
_SETTLE_TIMEOUT = 60


def _vm_is_absent(api: ProxmoxAPI, node: str, vmid: int) -> bool:
    try:
        api.vm_status(node, vmid)
    except ProxmoxVMNotFoundError:
        return True
    return False


def stop_and_delete_vm(api: ProxmoxAPI, node: str, vmid: int) -> None:
    """Delete the bound VM or raise when its removal cannot be confirmed.

    Only the API's exact missing-configuration response establishes absence.
    Permission, lock, transport and task failures remain failures even when an
    HTTP status happens to match the missing-configuration response.
    """
    try:
        status = api.vm_status(node, vmid)
    except ProxmoxVMNotFoundError:
        return
    if status["status"] == "running":
        upid = api.stop_vm(node, vmid)
        api.wait_for_task(node, upid)
    upid = api.delete_vm(node, vmid)
    api.wait_for_task(node, upid)
    if not _vm_is_absent(api, node, vmid):
        raise ProxmoxAPIError(
            f"Proxmox VM {vmid} still exists on node '{node}' after deletion",
            hint="Inspect this VM in Proxmox, then retry deletion; its Agentworks record is retained.",
        )


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


def rollback_partial_create(api: ProxmoxAPI, node: str, vmid: int, *, pending_upid: str | None = None) -> None:
    """Tear down whatever a failed ``create`` made: the cloned VM under
    ``vmid``, and nothing else.

    The failure arm's wrapper: the caller is already unwinding on the
    original error, so a rollback failure of any kind is contained here
    (warned with the manual-cleanup pointer) rather than raised, which
    would mask that original error. A ``KeyboardInterrupt`` during this
    rollback is NOT contained: it escapes to ``create``'s outer
    interrupt arm, which restarts the cleanup under the full two-Ctrl-C
    interrupt protocol (:func:`rollback_create_on_interrupt`). A
    teardown that returns has confirmed absence; an unknown result warns with
    the exact node and VMID for manual recovery.
    """
    try:
        _settle_and_teardown(api, node, vmid, pending_upid)
    except Exception:
        output.warn(f"Rollback incomplete: Proxmox VM {vmid} may remain on node '{node}'; delete it there manually.")


def rollback_create_on_interrupt(api: ProxmoxAPI, node: str, vmid: int, *, pending_upid: str | None = None) -> None:
    """Roll back the partially created VM after an operator interrupt
    inside ``ProxmoxPlatform.create``.

    The VM may be anywhere from mid-clone (the task still running) to
    fully up and mid-bootstrap, so this settles the pending task and
    runs the shared stop-then-delete. The cleanup never wedges and never
    replaces the original interrupt: a SECOND interrupt during it is
    absorbed once and abandons the remaining cleanup, and a non-interrupt
    failure (e.g. the host became unreachable) is absorbed the same way;
    both warn naming the node and VMID so the operator can finish the
    removal in the Proxmox web UI. A cleanup that returns has confirmed absence.
    Either way the caller re-raises the
    ORIGINAL interrupt, which propagates to ``create_vm``, whose unwind
    then deletes the DB row it no longer needs.
    """
    output.warn(
        f"Interrupted: cleaning up the partial Proxmox VM {vmid} on node "
        f"'{node}', please wait (Ctrl-C again to abandon it)..."
    )
    try:
        _settle_and_teardown(api, node, vmid, pending_upid)
    except (KeyboardInterrupt, Exception):
        # Both types on purpose (azure's absorber catches only the
        # interrupt): _settle_and_teardown lets transport-level errors
        # escape, and they must be absorbed HERE too, or they would
        # replace the original interrupt mid-propagation.
        output.warn(f"Cleanup abandoned: Proxmox VM {vmid} may remain on node '{node}'; delete it there manually.")
