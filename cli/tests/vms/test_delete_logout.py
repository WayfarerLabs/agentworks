"""``vm delete``'s Tailscale logout: the logout script over the platform's root channel, each outcome returned.

Nothing is checked over SSH afterwards, since the VM is going away. A cloud
VM whose host key was never recorded refuses inside ``run_as_root``, before
any route opens. The helper returns why the node was not logged out, which
delete reports once the row goes, or ``None`` once it was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import ConnectivityError, ProvisioningError, StateError, UserAbort
from agentworks.execution.errors import TrustMissing
from agentworks.vms.manager.tailscale import _tailscale_logout
from tests._root_channel_fakes import Answer, FakeGuest

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.db import Database, VMRow
    from tests.vm_access_support import AccessRecorder


class _Platform:
    """A platform whose root channel is a fake guest."""

    recovery_needs_guest_sshd = True

    def __init__(self, guest: FakeGuest, status: VMStatus | BaseException = VMStatus.RUNNING) -> None:
        self.guest = guest
        self._status = status

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        if isinstance(self._status, BaseException):
            raise self._status
        return self._status

    def run_as_root(self, vm: VMRow, ctx: RunContext, script: bytes, deadline: float) -> object:
        return self.guest.run_as_root(script, deadline)


def _vm(db: Database) -> VMRow:
    db.insert_vm("box", site="lima-local", hostname="box-node")
    db.update_vm_tailscale("box", "100.64.0.9")
    vm = db.get_vm("box")
    assert vm is not None
    return vm


def _log_out(db: Database, guest: FakeGuest, status: VMStatus | BaseException = VMStatus.RUNNING) -> str | None:
    return _tailscale_logout(_vm(db), cast("VMPlatform", _Platform(guest, status)), RunContext())


@pytest.mark.parametrize(
    ("answer", "logged_out"),
    [
        (Answer(status=0), True),
        (Answer(status=1), False),
        (Answer(status=None), False),
        (Answer(raises=ConnectivityError("channel failed")), False),
        (Answer(raises=TrustMissing("box")), False),
        (Answer(raises=ProvisioningError("provider said no")), False),
    ],
    ids=["logged-out", "failed", "unknown", "channel-raised", "never-enrolled", "provider-raised"],
)
def test_each_logout_outcome_is_returned_and_nothing_reaches_the_vm_after_it(
    db: Database,
    vm_access_recorder: AccessRecorder,
    answer: Answer,
    logged_out: bool,
) -> None:
    guest = FakeGuest(respond=lambda payload, seen: answer)

    reason = _log_out(db, guest)

    assert len(guest.scripts) == 1
    assert (reason is None) is logged_out
    assert vm_access_recorder.operations == []


@pytest.mark.parametrize("status", [VMStatus.STOPPED, VMStatus.DEALLOCATED])
def test_a_stopped_vm_is_not_asked(db: Database, status: VMStatus) -> None:
    """No channel reaches a stopped VM; a cloud's would probe for two minutes and then blame the firewall."""
    guest = FakeGuest(respond=lambda payload, seen: pytest.fail("a stopped VM was asked to log out"))

    assert _log_out(db, guest, status) is not None
    assert guest.scripts == []


def test_a_guest_the_platform_cannot_confirm_is_not_asked(db: Database) -> None:
    """A Proxmox VMID another guest now holds refuses its status, so that guest is never logged out."""
    guest = FakeGuest(respond=lambda payload, seen: pytest.fail("an unconfirmed guest was asked to log out"))

    assert _log_out(db, guest, StateError("VM 'box' is gone")) is not None
    assert guest.scripts == []


def test_an_operator_abort_is_not_downgraded(db: Database) -> None:
    guest = FakeGuest(respond=lambda payload, seen: Answer(raises=UserAbort("stop")))

    with pytest.raises(UserAbort):
        _log_out(db, guest)


def test_a_never_enrolled_cloud_vm_is_not_logged_out(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no host-key record a cloud's logout could only run under Unverified trust, where an impostor could
    report a logout that never happened, so it opens no route and dispatches nothing."""
    from agentworks.plugins.aws.platform import EC2Platform

    platform = EC2Platform("aws-site", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    monkeypatch.setattr(EC2Platform, "status", lambda self, vm, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(
        EC2Platform, "_transient_route", lambda *a, **k: pytest.fail("a route opened for a never-enrolled VM")
    )
    monkeypatch.setattr(
        "agentworks.execution.root_script.run_process", lambda *a, **k: pytest.fail("a script was dispatched")
    )

    assert _tailscale_logout(_vm(db), platform, RunContext()) is not None
