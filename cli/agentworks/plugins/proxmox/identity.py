"""Which guest holds a Proxmox VMID, and whether it is the one a row's create cloned.

VMIDs are unique across a cluster, and Proxmox hands a freed one to the next
create, so a row's VMID can come to address another guest. Every operation
that addresses a guest by VMID (delete, start, stop, status, the guest-agent
root channel, and create's rollback) asks :func:`find_guest` first, which
compares the name ``status/current`` reports with the name the clone set.

``status/current`` cannot answer alone: a token that may not audit a VMID
gets a 403 whether or not anything holds it, and a token with rights gets an
error for a VMID nothing holds. ``/cluster/nextid`` tells those apart for any
token (:meth:`~agentworks.plugins.proxmox.api.ProxmoxAPI.vmid_in_use`). So a
guest is ours only when its name matches; it is provably gone only when
nothing holds the VMID or a guest of another name does; and anything else is
unknown and refused, never read as gone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentworks.errors import StateError
from agentworks.plugins.proxmox.api import REQUEST_TIMEOUT_SECONDS, ProxmoxAPIError

if TYPE_CHECKING:
    from collections.abc import Collection

    from agentworks.plugins.proxmox.api import ProxmoxAPI


@dataclass(frozen=True)
class Gone:
    """The row's guest is provably gone from the cluster."""

    vm_name: str
    node: str
    vmid: int
    holder: str | None
    """The name of the guest that holds the VMID now, or ``None`` when nothing does."""

    def describe(self) -> str:
        if self.holder is None:
            return f"VM '{self.vm_name}' is gone: nothing holds its Proxmox VMID {self.vmid}"
        return f"VM '{self.vm_name}' is gone: its Proxmox VMID {self.vmid} on node '{self.node}' is now '{self.holder}'"

    def refusal(self) -> StateError:
        """The refusal of an operation that needs the guest, which only ``agw vm delete`` resolves."""
        untouched = "" if self.holder is None else f", leaving '{self.holder}' untouched"
        return StateError(
            self.describe(),
            entity_kind="vm",
            entity_name=self.vm_name,
            hint=f"Run 'agw vm delete {self.vm_name}' to remove its row{untouched}.",
        )


def find_guest(
    api: ProxmoxAPI,
    node: str,
    vmid: int,
    *,
    vm_name: str,
    names: Collection[str],
    expires: float | None = None,
) -> dict[str, Any] | Gone:
    """Our guest's ``status/current`` answer, or :class:`Gone` when the guest is provably gone.

    ``names`` are the names the row's clone may carry. A guest the token
    cannot audit holding the VMID raises ``StateError``, and a VMID neither
    call can place raises the status read's ``ProxmoxAPIError``: both mean
    the guest's identity is unknown, which no caller may treat as gone.

    ``expires`` is when the caller's budget ends, on ``time.monotonic()``'s
    clock: each request gets what remains of it, at most
    ``REQUEST_TIMEOUT_SECONDS``, and a budget spent before a request leaves
    the VMID unplaced. With none, each request gets the whole request timeout.
    """
    timeout = _request_timeout(expires)
    if timeout <= 0:
        raise ProxmoxAPIError(f"no time remained to read the status of Proxmox VMID {vmid} on node '{node}'")
    try:
        current = api.vm_status(node, vmid, timeout=timeout)
    except ProxmoxAPIError as status_error:
        timeout = _request_timeout(expires)
        if timeout <= 0:
            raise
        try:
            held = api.vmid_in_use(vmid, timeout=timeout)
        except ProxmoxAPIError:
            raise status_error from None
        if not held:
            return Gone(vm_name, node, vmid, None)
        if status_error.code == 403:
            raise StateError(
                f"cannot tell whether Proxmox VMID {vmid} on node '{node}' is still VM '{vm_name}': "
                "the API token cannot audit the guest that holds it",
                entity_kind="vm",
                entity_name=vm_name,
                hint=(
                    f"Check VM {vmid} in the Proxmox web UI. If it is this VM, delete it there and rerun; "
                    f"if not, grant the token VM.Audit on /vms/{vmid} so agw can tell, then rerun."
                ),
            ) from None
        raise
    holder = current.get("name")
    if not isinstance(holder, str):
        raise ProxmoxAPIError(f"Proxmox API status of VMID {vmid} on node '{node}' carries no name")
    if holder in names:
        return current
    return Gone(vm_name, node, vmid, holder)


def _request_timeout(expires: float | None) -> float:
    """The timeout of the lookup's next request: what remains before ``expires``, if that is the shorter."""
    if expires is None:
        return REQUEST_TIMEOUT_SECONDS
    return min(REQUEST_TIMEOUT_SECONDS, expires - time.monotonic())
