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


class FakeProxmoxAPI:
    """Recording ``ProxmoxAPI`` stand-in for the platform's create and
    delete paths: happy-path returns, a shared call-order log, and
    per-method error injection. Errors are typed ``BaseException``
    because the interrupt tests inject ``KeyboardInterrupt`` mid-call;
    ``wait_errors`` and ``status_errors`` queue raises consumed
    front-first, so a test can make one call raise and still choose
    what a later call on the same method sees (the rollback's settle
    wait, the escalation re-entry, the orphan-backstop probe); a
    ``None`` entry in ``status_errors`` means "this call succeeds",
    letting a test target a later status call (e.g. the probe, the
    second status call in a teardown flow) without failing the first.
    A deleted VM is modeled: ``delete_vm`` marks it gone and
    ``vm_status`` then raises the API error a real PVE returns, which
    is what keeps the post-teardown existence probe quiet on the happy
    paths."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.running = False
        self.vm_deleted = False
        self.next_id_error: BaseException | None = None
        self.configure_error: BaseException | None = None
        self.stop_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        self.stop_task_error: BaseException | None = None
        self.status_errors: list[BaseException | None] = []
        self.wait_errors: dict[str, list[BaseException]] = {}
        # The VM behind QGA, the root channel: bootstrap, join and address read.
        self.guest = FakeGuest()
        self._exited: dict[int, RawRun] = {}

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]

    # -- cluster / VM ops ------------------------------------------------------

    def next_id(self) -> int:
        if self.next_id_error is not None:
            raise self.next_id_error
        self.calls.append(("next_id",))
        return NEWID

    def list_vms(self, node: str) -> list[dict[str, Any]]:
        return []

    def clone_vm(self, node: str, template_vmid: int, newid: int, name: str, **kw: Any) -> str:
        self.calls.append(("clone_vm", node, template_vmid, newid, name))
        return CLONE_UPID

    def configure_vm(self, node: str, vmid: int, **params: Any) -> None:
        if self.configure_error is not None:
            raise self.configure_error
        self.calls.append(("configure_vm", node, vmid))

    def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> None:
        self.calls.append(("resize_disk", node, vmid, disk, size))

    def start_vm(self, node: str, vmid: int) -> str:
        self.calls.append(("start_vm", node, vmid))
        self.running = True
        return START_UPID

    def stop_vm(self, node: str, vmid: int) -> str:
        if self.stop_error is not None:
            raise self.stop_error
        self.calls.append(("stop_vm", node, vmid))
        self.running = False
        return "UPID:pve1:stop"

    def delete_vm(self, node: str, vmid: int) -> str:
        if self.delete_error is not None:
            raise self.delete_error
        self.calls.append(("delete_vm", node, vmid))
        self.vm_deleted = True
        return "UPID:pve1:delete"

    def vm_status(self, node: str, vmid: int) -> dict[str, Any]:
        if self.status_errors:
            err = self.status_errors.pop(0)
            if err is not None:
                raise err
        self.calls.append(("vm_status", node, vmid))
        if self.vm_deleted:
            raise ProxmoxAPIError(f"Configuration file 'qemu-server/{vmid}.conf' does not exist")
        return {"status": "running" if self.running else "stopped"}

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

    def guest_agent_network(self, node: str, vmid: int) -> list[dict[str, Any]]:
        return [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
            {"name": "eth0", "ip-addresses": [{"ip-address": "10.0.0.5", "ip-address-type": "ipv4"}]},
        ]

    def guest_agent_exec_wait(
        self, node: str, vmid: int, command: str, args: list[str] | None = None, *, timeout: float = 60
    ) -> dict[str, Any] | None:
        argv = tuple(args or ())
        self.calls.append(("guest_agent_exec_wait", node, vmid, command, argv, timeout))
        return {"exitcode": 0, "out-data": ""}

    def guest_agent_exec(self, node: str, vmid: int, *, command: list[str], input_data: str, timeout: float) -> int:
        self.calls.append(("guest_agent_exec", node, vmid, tuple(command)))
        pid = len(self.calls)
        self._exited[pid] = self.guest.run_as_root(input_data.encode("utf-8"), timeout)
        return pid

    def guest_agent_exec_status(self, node: str, vmid: int, *, pid: int, timeout: float) -> dict[str, Any]:
        raw = self._exited.pop(pid)
        return {"exited": True, "out-data": raw.stdout.decode("utf-8"), "err-data": raw.stderr.decode("utf-8")}
