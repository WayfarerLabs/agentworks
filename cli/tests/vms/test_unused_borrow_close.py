"""Native aggregate close revokes a pristine borrow whose reply was lost."""

from __future__ import annotations

import sys

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.execution.carrier import Deadline
from agentworks.execution.target import ExecutionRoute
from agentworks.operations import OperationBorrow
from agentworks.vms._native_operation import _vm_operation

from .test_native_operation import _install_route, _root, _scope
from .test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux guest identity")


def test_native_close_retires_lost_pristine_borrow_without_erasing_other_cleanup(database, tmp_path, monkeypatch):
    platform = WSL2Platform("wsl2", {})
    _, native, observer = _install_route(database, platform, monkeypatch)
    orphan: list[OperationBorrow] = []
    control = KeyboardInterrupt()
    with _vm_operation(
        database,
        "box",
        platform,
        RunContext(),
        route=ExecutionRoute.NATIVE,
        deadline=Deadline.after(30),
        trusted_root=_root(tmp_path),
    ) as views:
        original = views.owner.borrow

        def lost_reply():
            orphan.append(original())
            raise control

        monkeypatch.setattr(views.owner, "borrow", lost_reply)
        pending = views.owner.list_pending_lifecycle_obligations()
        assert pending
        with pytest.raises(KeyboardInterrupt) as caught:
            views.owner.borrow()
        assert caught.value is control and not orphan[0].closed
        assert views.owner.list_pending_lifecycle_obligations() == pending
    assert orphan[0].closed
    assert database.operations.inspect(_scope()) is None
    assert native.events and observer.events == ["observe"]
