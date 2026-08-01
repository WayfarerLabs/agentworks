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

Error containment: ``ProxmoxAPI`` types only HTTP failures as
``ProxmoxAPIError``; a transport-level failure (``URLError``/``OSError``
/SSL) raises through the narrow per-step suppressions. The two rollback
wrappers therefore contain ALL non-interrupt exceptions, each with an
operator warning naming the node and VMID: a rollback failure must
never mask the original error (the Exception arm) or replace the
original interrupt (the interrupt arm). ``stop_and_delete_vm`` itself
keeps the delete op's original narrow suppression unchanged.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.plugins.proxmox.api import ProxmoxAPIError

if TYPE_CHECKING:
    from agentworks.plugins.proxmox.api import ProxmoxAPI

# How long the rollback waits for a cancelled in-flight task to settle.
# Deliberately shorter than wait_for_task's 300s default: the task has
# been told to stop, so this only covers PVE tearing the worker down,
# not a full clone finishing.
_SETTLE_TIMEOUT = 60


def stop_and_delete_vm(api: ProxmoxAPI, node: str, vmid: int) -> None:
    """Stop the VM if it is running, then delete the VMID, each step
    best-effort (the VM may already be stopped, mid-teardown, or gone,
    and the delete op is retried against partial prior failures).
    Shared by the delete op and the create rollback wrappers, so the
    stop-then-delete ordering lives in exactly one place. Suppression
    is deliberately narrow (``ProxmoxAPIError`` only, the delete op's
    original behavior); the rollback wrappers contain transport-level
    failures themselves."""
    with contextlib.suppress(ProxmoxAPIError):
        status = api.vm_status(node, vmid)
        if status.get("status") == "running":
            upid = api.stop_vm(node, vmid)
            api.wait_for_task(node, upid)
    with contextlib.suppress(ProxmoxAPIError):
        upid = api.delete_vm(node, vmid)
        api.wait_for_task(node, upid)


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
    interrupt protocol (:func:`rollback_create_on_interrupt`).
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
    removal in the Proxmox web UI. Either way the caller re-raises the
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
        output.warn(f"Cleanup abandoned: Proxmox VM {vmid} may remain on node '{node}'; delete it there manually.")
