"""A recording Proxmox API for the platform's create and delete paths, with QGA answered by a fake guest."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.plugins.proxmox.api import ProxmoxAPIError
from tests._root_channel_fakes import FakeGuest

if TYPE_CHECKING:
    from agentworks.execution.root_script import RawRun

PROXMOX_CONFIG = {
    "api_url": "https://pve.example.com:8006",
    "node": "pve1",
    "token_id": "agw@pam!agw",
    "template_vmids": {"trixie": 9001},
}

NEWID = 100
TEMPLATE_VMID = 9001
CLONE_UPID = "UPID:pve1:clone"
START_UPID = "UPID:pve1:start"
RESIZE_UPID = "UPID:pve1:resize"


def refused(method: str, path: str) -> ProxmoxAPIError:
    """The 403 a real PVE answers when the token's ACLs do not cover the VMID's path."""
    error = ProxmoxAPIError(f"Proxmox API {method} {path} failed (403): Permission check failed")
    error.code = 403
    return error


class FakeProxmoxAPI:
    """Recording ``ProxmoxAPI`` stand-in for the platform's create and
    delete paths: happy-path returns, a shared call-order log, and
    per-method error injection. Errors are typed ``BaseException``
    because the interrupt tests inject ``KeyboardInterrupt`` mid-call;
    ``wait_errors`` and ``status_errors`` queue raises consumed
    front-first, so a test can make one call raise and still choose
    what a later call on the same method sees (the rollback's settle
    wait, the escalation re-entry); a ``None`` entry in
    ``status_errors`` means "this call succeeds", letting a test target
    a later status call without failing the first.

    The guest at the VMID is modeled as the setup script's pool-scoped
    token sees it. ``holder`` names the guest holding ``NEWID``, or is
    ``None`` when nothing does: ``clone_vm`` sets it to the clone's name
    and ``delete_vm`` clears it (a test sets it to model a guest that is
    already there, ours or another's). ``auditable`` is whether the token
    may audit that guest; one out of the pool is not. A VMID the token
    cannot audit, which includes one nothing holds, answers status,
    stop and DELETE with a 403, while ``vmid_in_use`` (``/cluster/nextid``)
    answers for any token, as on PVE 8 and 9."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.running = False
        self.holder: str | None = None
        self.auditable = True
        self.list_error: BaseException | None = None
        self.next_id_error: BaseException | None = None
        self.in_use_error: BaseException | None = None
        self.clone_error: BaseException | None = None
        self.configure_error: BaseException | None = None
        self.stop_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        self.stop_task_error: BaseException | None = None
        self.status_errors: list[BaseException | None] = []
        # The timeout each identity read (status and VMID-in-use) was given, in call order.
        self.identity_timeouts: list[float | None] = []
        self.wait_errors: dict[str, list[BaseException]] = {}
        # The VM behind QGA, the root channel: bootstrap, join and address read.
        self.guest = FakeGuest()
        self._exited: dict[int, RawRun] = {}

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]

    def _audited(self, method: str, path: str) -> None:
        """Refuse as PVE does unless the token may audit the guest at the VMID."""
        if self.holder is None or not self.auditable:
            raise refused(method, path)

    # -- cluster / VM ops ------------------------------------------------------

    def next_id(self) -> int:
        if self.next_id_error is not None:
            raise self.next_id_error
        self.calls.append(("next_id",))
        return NEWID

    def vmid_in_use(self, vmid: int, *, timeout: float | None = None) -> bool:
        self.identity_timeouts.append(timeout)
        if self.in_use_error is not None:
            raise self.in_use_error
        self.calls.append(("vmid_in_use", vmid))
        return vmid == NEWID and self.holder is not None

    def list_vms(self, node: str) -> list[dict[str, Any]]:
        if self.list_error is not None:
            raise self.list_error
        self.calls.append(("list_vms", node))
        return [{"vmid": NEWID, "name": self.holder}] if self.holder is not None and self.auditable else []

    def clone_vm(self, node: str, template_vmid: int, newid: int, name: str, **kw: Any) -> str:
        self.calls.append(("clone_vm", node, template_vmid, newid, name))
        if self.clone_error is not None:
            raise self.clone_error
        self.holder = name
        return CLONE_UPID

    def configure_vm(self, node: str, vmid: int, **params: Any) -> None:
        if self.configure_error is not None:
            raise self.configure_error
        self.calls.append(("configure_vm", node, vmid))

    def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> str:
        self.calls.append(("resize_disk", node, vmid, disk, size))
        return RESIZE_UPID

    def start_vm(self, node: str, vmid: int) -> str:
        self.calls.append(("start_vm", node, vmid))
        self.running = True
        return START_UPID

    def stop_vm(self, node: str, vmid: int) -> str:
        if self.stop_error is not None:
            raise self.stop_error
        self._audited("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")
        self.calls.append(("stop_vm", node, vmid))
        self.running = False
        return "UPID:pve1:stop"

    def delete_vm(self, node: str, vmid: int) -> str:
        if self.delete_error is not None:
            raise self.delete_error
        self._audited("DELETE", f"/nodes/{node}/qemu/{vmid}")
        self.calls.append(("delete_vm", node, vmid))
        self.holder = None
        return "UPID:pve1:delete"

    def vm_status(self, node: str, vmid: int, *, timeout: float | None = None) -> dict[str, Any]:
        self.identity_timeouts.append(timeout)
        if self.status_errors:
            err = self.status_errors.pop(0)
            if err is not None:
                raise err
        self._audited("GET", f"/nodes/{node}/qemu/{vmid}/status/current")
        self.calls.append(("vm_status", node, vmid))
        return {"status": "running" if self.running else "stopped", "name": self.holder}

    # -- tasks -----------------------------------------------------------------

    def stop_task(self, node: str, upid: str) -> None:
        if self.stop_task_error is not None:
            raise self.stop_task_error
        self.calls.append(("stop_task", node, upid))

    def wait_for_task(self, node: str, upid: str, *, timeout: int = 300, poll_interval: float = 2.0) -> None:
        queue = self.wait_errors.get(upid)
        if queue:
            raise queue.pop(0)
        self.calls.append(("wait_for_task", node, upid))

    # -- guest agent (immediate readiness: no polling sleeps) ------------------

    def guest_agent_ping(self, node: str, vmid: int, *, timeout: float) -> None:
        return None

    def guest_agent_exec(self, node: str, vmid: int, *, command: list[str], input_data: str, timeout: float) -> int:
        self.calls.append(("guest_agent_exec", node, vmid, tuple(command)))
        pid = len(self.calls)
        self._exited[pid] = self.guest.run_as_root(input_data.encode("utf-8"), timeout)
        return pid

    def guest_agent_exec_status(self, node: str, vmid: int, *, pid: int, timeout: float) -> dict[str, Any]:
        raw = self._exited.pop(pid)
        return {"exited": True, "out-data": raw.stdout.decode("utf-8"), "err-data": raw.stderr.decode("utf-8")}
