"""``vm delete``'s Tailscale logout: the logout script over the platform's root channel, each outcome reported.

Nothing is checked over SSH afterwards, since the VM is going away. A cloud
VM whose host key was never recorded refuses inside ``run_as_root``, before
any route opens, and delete warns naming the node instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import ConnectivityError, ProvisioningError, UserAbort
from agentworks.execution.errors import TrustMissing
from agentworks.vms.manager.tailscale import _tailscale_logout
from tests._root_channel_fakes import Answer, FakeGuest

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.db import Database, VMRow
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import AccessRecorder


class _Platform:
    """A platform whose root channel is a fake guest."""

    recovery_needs_guest_sshd = True

    def __init__(self, guest: FakeGuest) -> None:
        self.guest = guest

    def run_as_root(self, vm: VMRow, ctx: RunContext, script: bytes, deadline: float) -> object:
        return self.guest.run_as_root(script, deadline)


def _vm(db: Database) -> VMRow:
    db.insert_vm("box", site="lima-local", hostname="box-node")
    db.update_vm_tailscale("box", "100.64.0.9")
    vm = db.get_vm("box")
    assert vm is not None
    return vm


def _log_out(db: Database, guest: FakeGuest) -> None:
    _tailscale_logout(_vm(db), cast("VMPlatform", _Platform(guest)), RunContext())


@pytest.mark.parametrize(
    ("answer", "warned"),
    [
        (Answer(status=0), False),
        (Answer(status=1), True),
        (Answer(status=None), True),
        (Answer(raises=ConnectivityError("channel failed")), True),
        (Answer(raises=TrustMissing("box")), True),
        (Answer(raises=ProvisioningError("provider said no")), True),
    ],
    ids=["logged-out", "failed", "unknown", "channel-raised", "never-enrolled", "provider-raised"],
)
def test_each_logout_outcome_is_reported_and_nothing_reaches_the_vm_after_it(
    db: Database,
    captured_output: CapturedOutput,
    vm_access_recorder: AccessRecorder,
    answer: Answer,
    warned: bool,
) -> None:
    guest = FakeGuest(respond=lambda payload, seen: answer)

    _log_out(db, guest)

    assert len(guest.scripts) == 1
    assert bool(captured_output.warnings) is warned
    assert vm_access_recorder.operations == []


def test_an_operator_abort_is_not_downgraded(db: Database) -> None:
    guest = FakeGuest(respond=lambda payload, seen: Answer(raises=UserAbort("stop")))

    with pytest.raises(UserAbort):
        _log_out(db, guest)


def test_a_never_enrolled_cloud_vm_is_not_logged_out_and_its_node_is_named(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """With no host-key record a cloud's logout could only run under Unverified trust, where an impostor could
    report a logout that never happened, so it opens no route and dispatches nothing."""
    from agentworks.plugins.aws.platform import EC2Platform

    platform = EC2Platform("aws-site", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    monkeypatch.setattr(
        EC2Platform, "transient_route", lambda *a, **k: pytest.fail("a route opened for a never-enrolled VM")
    )
    monkeypatch.setattr(
        "agentworks.execution.root_script.run_process", lambda *a, **k: pytest.fail("a script was dispatched")
    )

    _tailscale_logout(_vm(db), platform, RunContext())

    (warning,) = captured_output.warnings
    assert "box-node" in warning
