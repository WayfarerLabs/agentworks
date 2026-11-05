"""``vm start``'s and the gate's one wait for the VM to answer over SSH (``_await_tailnet``).

An already-running VM is only verified; a started one waits to reconnect. A
VM that does not answer, or has no address, fails with :class:`VMNotAnswering`,
whose guidance is ``agw vm recover``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.db import ProvisioningStatus, VMStatus
from agentworks.execution import reachability
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.vms.manager.tailscale import VMNotAnswering, _await_tailnet
from tests.conftest import seed_vm_trust

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db import Database, VMRow
    from tests.vm_access_support import AccessRecorder


@pytest.fixture(autouse=True)
def _on_tailscale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workstation is on Tailscale with a supported client, whatever the test host is: start checks both first."""
    from agentworks.execution.client import SSHClient

    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: SSHClient("/usr/bin/ssh", (9, 2)))


def _seed_vm(db: Database, *, address: str | None = "100.64.0.9") -> VMRow:
    db.insert_vm("box", site="proxmox", hostname="box")
    if address:
        db.update_vm_tailscale("box", address)
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    seed_vm_trust("box")
    vm = db.get_vm("box")
    assert vm is not None
    return vm


def _record_waits(monkeypatch: pytest.MonkeyPatch, *, answers: bool = True) -> list[reachability.Wording]:
    waits: list[reachability.Wording] = []

    def _wait(access: object, *, wording: reachability.Wording) -> bool:
        waits.append(wording)
        return answers

    monkeypatch.setattr(reachability, "wait", _wait)
    return waits


@pytest.mark.parametrize(
    ("already_running", "wording"),
    [(True, reachability.Wording.VERIFY), (False, reachability.Wording.RECONNECT)],
)
def test_the_wait_verifies_a_running_vm_and_waits_out_a_start(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    vm_access_recorder: AccessRecorder,
    already_running: bool,
    wording: reachability.Wording,
) -> None:
    vm = _seed_vm(db)
    waits = _record_waits(monkeypatch)

    _await_tailnet(db, make_config(), vm, already_running=already_running)

    assert waits == [wording]


def test_a_vm_that_does_not_answer_raises(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    vm_access_recorder: AccessRecorder,
) -> None:
    vm = _seed_vm(db)
    _record_waits(monkeypatch, answers=False)

    with pytest.raises(VMNotAnswering):
        _await_tailnet(db, make_config(), vm, already_running=False)


def test_a_vm_with_no_address_raises_without_waiting(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vm = _seed_vm(db, address=None)
    waits = _record_waits(monkeypatch)

    with pytest.raises(VMNotAnswering):
        _await_tailnet(db, make_config(), vm, already_running=False)

    assert waits == []


def test_start_vm_through_a_hold_that_anchors_waits_exactly_once(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    resolve_counter: list[list[str]],  # noqa: ARG001
    vm_access_recorder: AccessRecorder,
) -> None:
    """Through a WSL2-shaped hold (one that anchors, not the no-op default),
    the start waits once, inside it. The guard that the real WSL2 hold runs
    no wait of its own is ``test_wsl2_keepalive.py``'s, since WSL2's site is
    unavailable on a non-Windows test host."""
    config = make_config()
    _seed_vm(db)
    events: list[str] = []

    @contextlib.contextmanager
    def _anchoring_hold(self: object, vm: VMRow, *, config: object = None) -> Iterator[None]:
        events.append("anchor")
        try:
            yield
        finally:
            events.append("release")

    def _wait(access: object, *, wording: reachability.Wording) -> bool:
        events.append(f"wait:{wording.name}")
        return True

    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: None)
    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _anchoring_hold)
    monkeypatch.setattr(reachability, "wait", _wait)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["anchor", "wait:VERIFY", "release"]
