"""Native Proxmox pure admission before authenticated provider observation.

Host and guest delivery boundaries are synthetic. The actual binding factories,
SQLite claim, shared context and account preparation remain intact.
"""

from __future__ import annotations

import sys
from pathlib import PurePosixPath

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.execution.models import Command, Input
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionRoute
from agentworks.vms._native_operation import vm_execution_context
from tests.vms.test_proxmox_native_operation import _scope
from tests.vms.test_proxmox_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="packed Linux helper proof")


def test_proxmox_managed_live_input_refuses_before_native_provider_check(database, tmp_path, monkeypatch):
    from agentworks.plugins.proxmox._native_binding import proxmox_native_binding
    from tests.vms.test_proxmox_native_operation import _install

    platform, route = _install(database, monkeypatch)
    monkeypatch.setattr("agentworks.plugins.proxmox._native_access.proxmox_native_binding", proxmox_native_binding)

    class Source:
        def try_read(self, limit):
            pytest.fail("unsupported live input was consumed")

    with vm_execution_context(
        database,
        "box",
        platform,
        RunContext(),
        route=ExecutionRoute.NATIVE,
        deadline=Deadline.after(30),
        trusted_root=PurePosixPath(tmp_path),
    ) as ctx:
        target = ctx.admin_execution_target()
        assert target is not None
        execution = target.execution()
        assert execution is not None
        before = list(route.deadlines)

        def deny(*args, **kwargs):
            pytest.fail("unsupported live request reached authenticated provider observation")

        monkeypatch.setattr(platform, "observe_provider_locator", deny)
        with pytest.raises(ValidationError):
            execution.run(Command(["/bin/true"]), profile=Protection.MANAGED, stdin=Input.live(Source()))
        assert route.deadlines == before and not execution._operation.managed_runs
        assert database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
    assert database.operations.inspect(_scope()) is None
