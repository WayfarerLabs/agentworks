"""Every Proxmox operation that addresses a guest by VMID first confirms the guest there is the row's.

A freed VMID is reissued to the next create, so a stale row can come to
address another guest (material 4 of #866's round two). Delete's handling
lives with the other delete tests in ``test_proxmox_create_interrupt.py``.
"""

from __future__ import annotations

import contextlib
import math
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.execution import root_script
from agentworks.execution.root_script import RawRun
from agentworks.plugins.proxmox import guest_agent
from agentworks.plugins.proxmox import platform as proxmox_platform
from agentworks.plugins.proxmox.api import ProxmoxAPIError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from tests._proxmox_fakes import NEWID, PROXMOX_CONFIG, FakeProxmoxAPI

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import VMRow

ROW = cast("VMRow", SimpleNamespace(name="vm1", hostname="vm1", platform_metadata={"vmid": str(NEWID), "node": "pve1"}))

OPERATIONS: dict[str, Callable[[ProxmoxPlatform], object]] = {
    "start": lambda platform: platform.start(ROW, RunContext()),
    "stop": lambda platform: platform.stop(ROW, RunContext()),
    "status": lambda platform: platform.status(ROW, RunContext()),
    "run_as_root": lambda platform: _run_script(platform),
}


def _run_script(platform: ProxmoxPlatform) -> object:
    """A root script, run as core runs one: through ``root_script.run`` over the platform's channel."""
    return root_script.run(partial(platform.run_as_root, ROW, RunContext()), "true", deadline=30)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProxmoxAPI:
    api = FakeProxmoxAPI()
    monkeypatch.setattr(ProxmoxPlatform, "_api", lambda self, ctx: api)
    return api


def _platform() -> ProxmoxPlatform:
    return ProxmoxPlatform("pve-site", dict(PROXMOX_CONFIG))


_ADDRESSING = ("start_vm", "stop_vm", "delete_vm", "guest_agent_exec")


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_reissued_vmid_is_refused_naming_the_guest_that_holds_it(fake: FakeProxmoxAPI, operation: str) -> None:
    fake.holder = "vm2"
    fake.running = True

    with pytest.raises(StateError) as caught:
        OPERATIONS[operation](_platform())

    assert "'vm2'" in str(caught.value)
    assert "agw vm delete vm1" in (caught.value.hint or "")
    assert not set(_ADDRESSING) & set(fake.ops())


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_gone_guest_is_refused_naming_delete(fake: FakeProxmoxAPI, operation: str) -> None:
    with pytest.raises(StateError) as caught:
        OPERATIONS[operation](_platform())

    assert "agw vm delete vm1" in (caught.value.hint or "")
    assert not set(_ADDRESSING) & set(fake.ops())


@pytest.mark.parametrize("operation", OPERATIONS)
def test_a_guest_the_token_cannot_audit_is_refused(fake: FakeProxmoxAPI, operation: str) -> None:
    fake.holder = "outside-the-pool"
    fake.auditable = False

    with pytest.raises(StateError) as caught:
        OPERATIONS[operation](_platform())

    assert caught.value.hint
    assert not set(_ADDRESSING) & set(fake.ops())


@pytest.mark.parametrize("operation", [*OPERATIONS, "delete"])
def test_every_identity_read_has_a_finite_timeout(fake: FakeProxmoxAPI, operation: str) -> None:
    """An API that stops answering fails the confirmation; it cannot hang the operation behind it."""
    # Nothing holds the VMID, so the status read is refused and the VMID-in-use read follows it.
    platform = _platform()
    with contextlib.suppress(StateError):
        if operation == "delete":
            platform.delete(ROW, RunContext())
        else:
            OPERATIONS[operation](platform)

    assert len(fake.identity_timeouts) == 2
    assert all(timeout is not None and 0 < timeout < math.inf for timeout in fake.identity_timeouts)


def test_our_guest_is_operated_on(fake: FakeProxmoxAPI) -> None:
    fake.holder = "vm1"
    platform = _platform()

    assert platform.status(ROW, RunContext()) is VMStatus.STOPPED
    platform.start(ROW, RunContext())
    assert platform.status(ROW, RunContext()) is VMStatus.RUNNING
    _run_script(platform)
    platform.stop(ROW, RunContext())

    assert [op for op in fake.ops() if op in _ADDRESSING] == ["start_vm", "guest_agent_exec", "stop_vm"]


def test_an_api_failure_reads_as_unknown_status_but_refuses_a_start(fake: FakeProxmoxAPI) -> None:
    """Status degrades on an outage; an operation that would address the VMID cannot confirm it, so it raises."""
    fake.holder = "vm1"
    outage = ProxmoxAPIError("Proxmox API failed: connection refused")
    fake.status_errors = [outage, outage]
    fake.in_use_error = outage
    platform = _platform()

    assert platform.status(ROW, RunContext()) is VMStatus.UNKNOWN
    with pytest.raises(ProxmoxAPIError):
        platform.start(ROW, RunContext())

    assert "start_vm" not in fake.ops()


def test_the_identity_check_spends_the_root_scripts_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    passed: list[float] = []

    def slow_check(self: ProxmoxPlatform, vm: VMRow, ctx: RunContext) -> dict[str, object]:
        now[0] += 20
        return {}

    monkeypatch.setattr(proxmox_platform, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(ProxmoxPlatform, "_guest", slow_check)
    monkeypatch.setattr(ProxmoxPlatform, "_api", lambda self, ctx: None)
    monkeypatch.setattr(
        guest_agent,
        "run_as_root",
        lambda api, node, vmid, script, deadline: passed.append(deadline) or RawRun(b"", b""),
    )

    _platform().run_as_root(ROW, RunContext(), b"true", 30)

    assert passed == [10]
