"""Canonical contexts over actual Lima access and held directory custody.

Host query output and SSH credential admission are synthetic. The shared kernel,
SQLite ownership, directory descriptors and packed command/file helpers are real.
This is not installed Lima or native Windows evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext, TargetAbsence
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.execution import _lima_activation, _lima_selection
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.lima import LimaCarrier
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionRoute
from agentworks.vms._lima_native_access import LimaOwnedNativePlatformAccess
from agentworks.vms._native_operation import vm_execution_context
from tests.vms.canonical_support import ADDRESS, canonical_config, install_canonical_delivery
from tests.vms.test_lima_native_access import LocalLima
from tests.vms.test_native_operation import _root, _RouteCarrier, _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="packed Linux helpers and held directory proof")


@pytest.fixture
def lima(database, tmp_path, monkeypatch, request):
    driver, status = getattr(request, "param", ("qemu", "Running"))
    home = tmp_path / "lima-home"
    home.mkdir()
    (home / "selected").mkdir()
    database.update_vm_platform_metadata("box", {"instance_name": "selected"})
    database.update_vm_tailscale("box", ADDRESS)
    platform = LimaPlatform("wsl2", {})
    route = _RouteCarrier(database)
    connections = install_canonical_delivery(monkeypatch, route)
    state = SimpleNamespace(
        platform=platform,
        route=route,
        connections=connections,
        access=None,
        fake=None,
        original=None,
        deadline=Deadline.after(30),
        root=_root(tmp_path),
    )
    original_build = platform.build_native_execution_access
    original_which = shutil.which
    monkeypatch.setenv("LIMA_HOME", str(home))
    monkeypatch.setattr(shutil, "which", lambda name: "/literal/limactl" if name == "limactl" else original_which(name))

    def deny(*args, **kwargs):
        pytest.fail("canonical Lima selected native configuration, route or guest delivery")

    def build(vm, ctx, *, owner, custody):
        assert state.access is None
        access = original_build(vm, ctx, owner=owner, custody=custody)
        assert isinstance(access, LimaOwnedNativePlatformAccess)
        assert access.route is None and access.selected is None and access.binding is None
        assert access.independent_availability is None
        state.access = access
        state.fake = LocalLima(home, owner, status=status, vm_type=driver)
        return access

    def run(argv, *, io, deadline, custody, env):
        access = state.access
        claim = database.operations.inspect(_scope())
        assert claim is not None and claim.ownership == access.owner.ownership
        assert deadline is state.deadline and custody is access.custody
        if access.selected is not None:
            if state.original is None:
                state.original = access.selected
            assert access.selected is state.original
        return state.fake.run(argv, io=io, deadline=deadline, custody=custody, env=env)

    monkeypatch.setattr(platform, "build_native_execution_access", build)
    monkeypatch.setattr(platform, "resolve_native_execution_binding", deny)
    monkeypatch.setattr(LimaOwnedNativePlatformAccess, "native_configuration_check", property(lambda _: deny))
    monkeypatch.setattr(LimaOwnedNativePlatformAccess, "prepare_native_route", deny)
    monkeypatch.setattr(LimaCarrier, "execute", deny)
    monkeypatch.setattr(_lima_selection, "run_process", run)
    monkeypatch.setattr(_lima_activation, "run_process", run)
    return state


@pytest.mark.parametrize(
    "lima", [("qemu", "Running"), ("vz", "Stopped"), ("wsl2", "Running"), ("unknown", "Running")], indirect=True
)
def test_canonical_lima_repeated_observations_share_context_owner_and_cleanup(database, tmp_path, lima):
    config = canonical_config(tmp_path)
    incoming = RunContext(config=config)
    with vm_execution_context(
        database,
        "box",
        lima.platform,
        incoming,
        route=ExecutionRoute.CANONICAL,
        deadline=lima.deadline,
        trusted_root=lima.root,
    ) as ctx:
        access, fake = lima.access, lima.fake
        assert access.selected is lima.original
        assert access.selected.power is (VMStatus.STOPPED if fake.vm_type == "vz" else VMStatus.RUNNING)
        assert len(fake.calls) > 3
        assert access.binding is access.route_check is None
        assert lima.route.guest.calls == 1 and lima.route.accounts.calls == ["admin", "root"]
        held = access.route.directory
        descriptor = held.descriptor
        assert descriptor is not None
        os.fstat(descriptor)
        target = ctx.admin_execution_target()
        assert target is not None and target.route is ExecutionRoute.CANONICAL
        before = len(fake.calls), len(lima.connections)
        for _ in range(2):
            assert ctx.config is config and ctx.admin_execution_target() is target
            assert ctx.agent_execution_target() is None
            assert ctx.agent_execution_target_absence() is TargetAbsence.UNAVAILABLE
            assert target.identity.account == "admin"
        assert before == (len(fake.calls), len(lima.connections))
        execution, files = target.execution(), target.files()
        assert execution is not None and files is not None
        binding = execution._operation._native_binding
        expected = _IndependentJobAvailability.NO_IDLE_STOP if fake.vm_type in {"qemu", "vz"} else None
        assert binding is not None and binding._independent_availability is access.independent_availability is expected
        assert execution.run(Command(["/bin/true"]), profile=Protection.DIRECT).ok
        assert files.stat(lima.root / "missing") is None
        assert lima.route.file_records and all(item is lima.deadline for item in lima.route.local_deadlines)
        assert lima.route.ownership == access.owner.ownership
        assert access.selected is lima.original
    assert held.settled and descriptor is not None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert access.custody.settled and access.independent_availability is None
    assert database.operations.inspect(_scope()) is None
    assert sum(argv[1] == "start" for argv, _, _ in fake.calls) == (fake.vm_type == "vz")
    before = len(fake.calls), len(lima.connections)
    with pytest.raises(StateError):
        execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    with pytest.raises(StateError):
        files.stat(lima.root / "missing")
    assert before == (len(fake.calls), len(lima.connections))


@pytest.mark.parametrize("drift", ["driver", "power", "locator", "directory"])
def test_canonical_lima_drift_refuses_before_ssh_dispatch_without_reactivation(database, tmp_path, lima, drift):
    with vm_execution_context(
        database,
        "box",
        lima.platform,
        RunContext(config=canonical_config(tmp_path)),
        route=ExecutionRoute.CANONICAL,
        deadline=lima.deadline,
        trusted_root=lima.root,
    ) as ctx:
        access, fake = lima.access, lima.fake
        target = ctx.admin_execution_target()
        assert target is not None
        execution, files = target.execution(), target.files()
        assert execution is not None and files is not None
        held = access.route.directory
        if drift == "driver":
            fake.vm_type = "wsl2"
        elif drift == "power":
            fake.status = "Stopped"
        elif drift == "locator":
            other = fake.home / "other"
            other.mkdir()
            fake.raw = json.dumps(
                {
                    "name": "selected",
                    "status": "Running",
                    "dir": str(other),
                    "vmType": "qemu",
                    "user": fake.user,
                    "hasErrors": False,
                }
            ).encode()
        else:
            fake.directory.rename(fake.home / "original")
            fake.directory.mkdir()
        dispatches = len(lima.connections)
        for _ in range(2):
            with pytest.raises(StateError):
                execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
            with pytest.raises(StateError):
                files.stat(lima.root / "missing")
        assert len(lima.connections) == dispatches
        assert access.selected is lima.original and access.activation is access.binding is None
        assert access.independent_availability is None
        assert lima.route.guest.calls == 1 and lima.route.accounts.calls == ["admin", "root"]
        assert all(argv[1] == "list" for argv, _, _ in fake.calls)
    assert held.settled and access.custody.settled
    assert database.operations.inspect(_scope()) is None
