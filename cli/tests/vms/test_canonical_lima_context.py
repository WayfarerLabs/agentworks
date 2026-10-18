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
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext, TargetAbsence
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.execution import _lima_activation, _lima_selection, _lima_wsl_hold
from agentworks.execution._lima_selection import LimaDirectoryCustody, LimaLocalSelection
from agentworks.execution._wsl2_registration import WSLRegistration
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline, SinkOutput
from agentworks.execution.carriers.lima import LimaCarrier
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionRoute
from agentworks.operations import LifecycleObligation
from agentworks.vms._lima_native_access import LimaOwnedNativePlatformAccess
from agentworks.vms._native_operation import vm_execution_context
from tests.execution.carriers.test_lima import process_result
from tests.execution.test_wsl2_platform_hold import FakeNative, FakeObserver
from tests.vms.canonical_support import ADDRESS, canonical_config, install_canonical_delivery
from tests.vms.test_lima_native_access import LocalLima
from tests.vms.test_native_operation import _root, _RouteCarrier, _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="packed Linux helpers and held directory proof")


def _wsl_host_path(path: Path) -> Path:
    value = str(path)
    if os.name == "nt" or not value.startswith("/") or value.startswith("//"):
        return path
    return Path(f"/{value}")


@pytest.fixture
def lima(database, tmp_path, monkeypatch, request):
    driver, status = getattr(request, "param", ("qemu", "Running"))
    home = tmp_path / "lima-home"
    if driver == "wsl2":
        home = _wsl_host_path(home)
    home.mkdir()
    (home / "selected").mkdir()
    (home / "selected/lima.yaml").write_text(f"vmType: {driver}\n")
    held = LimaDirectoryCustody.open(str(home / "selected")) if driver == "wsl2" else None
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
        host_calls=[],
        initial_power=VMStatus.RUNNING if status == "Running" else VMStatus.STOPPED,
        original=None,
        deadline=Deadline.after(30),
        root=_root(tmp_path),
        native=FakeNative([]),
        observer=FakeObserver([]),
    )
    original_delivery = route.execute

    def delivery(*args, **kwargs):
        if driver == "wsl2":
            assert state.access.wsl_hold is not None
            state.access.wsl_hold.require_ready()
        return original_delivery(*args, **kwargs)

    monkeypatch.setattr(route, "execute", delivery)
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
        if driver == "wsl2":
            access.route = LimaLocalSelection(platform.site_name, "selected", "box", str(home), "/literal/limactl")
            access.route.directory = held
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
        if argv == ["wsl", "--list", "--verbose"]:
            state.host_calls.append(("power", deadline))
            assert isinstance(io.output, SinkOutput)
            io.output.stdout.try_write(memoryview(f"lima-selected {state.fake.status} 2\n".encode("utf-16-le")))
            return process_result()
        return state.fake.run(argv, io=io, deadline=deadline, custody=custody, env=env)

    def registration(name, vm_name, deadline, custody):
        assert (name, vm_name, deadline, custody) == ("lima-selected", "box", state.deadline, state.access.custody)
        state.host_calls.append(("registry", deadline))
        return WSLRegistration(
            "2f1b1ed8-a629-4fc5-93ef-504e9a780a37",
            "S-1-5-21-1001",
            None if state.fake.status == "Uninitialized" else "613ea898-8445-4e6e-82c7-f6e9ae8d7235",
            name,
            None if state.fake.status == "Uninitialized" else str(home / "selected"),
        )

    if driver == "wsl2":
        monkeypatch.setattr(_lima_selection, "sys", SimpleNamespace(platform="win32"))
        monkeypatch.setattr(_lima_selection, "observe_registration", registration)
        monkeypatch.setattr(_lima_wsl_hold, "WindowsWSL2HostClient", lambda: state.native)
        monkeypatch.setattr(_lima_wsl_hold, "WSL2GuestObserver", lambda connection: state.observer)

    monkeypatch.setattr(platform, "build_native_execution_access", build)
    monkeypatch.setattr(platform, "resolve_native_execution_binding", deny)
    monkeypatch.setattr(LimaOwnedNativePlatformAccess, "native_configuration_check", property(lambda _: deny))
    monkeypatch.setattr(LimaOwnedNativePlatformAccess, "prepare_native_route", deny)
    monkeypatch.setattr(LimaCarrier, "execute", deny)
    monkeypatch.setattr(_lima_selection, "run_process", run)
    monkeypatch.setattr(_lima_activation, "run_process", run)
    return state


@pytest.mark.parametrize(
    "lima",
    [("qemu", "Running"), ("vz", "Stopped"), ("wsl2", "Running"), ("wsl2", "Stopped"), ("wsl2", "Uninitialized")],
    indirect=True,
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
        assert access.selected.power is lima.initial_power
        assert len(fake.calls) + len(lima.host_calls) > 3
        if fake.vm_type == "wsl2":
            assert all(argv[1] == "start" for argv, _, _ in fake.calls)
            assert access.wsl_hold is not None
            access.wsl_hold.require_ready()
        else:
            assert access.wsl_hold is None and lima.native.events == []
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
    assert sum(argv[1] == "start" for argv, _, _ in fake.calls) == (lima.initial_power is VMStatus.STOPPED)
    before = len(fake.calls), len(lima.connections)
    with pytest.raises(StateError):
        execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    with pytest.raises(StateError):
        files.stat(lima.root / "missing")
    assert before == (len(fake.calls), len(lima.connections))


@pytest.mark.parametrize("lima", [("wsl2", "Running")], indirect=True)
def test_canonical_guest_preparation_requires_durable_wsl_ready(database, tmp_path, lima, monkeypatch):
    monkeypatch.setattr(LifecycleObligation, "publish_payload", lambda self, **kwargs: self._persisted_obligation)
    with (
        pytest.raises(StateError),
        vm_execution_context(
            database,
            "box",
            lima.platform,
            RunContext(config=canonical_config(tmp_path)),
            route=ExecutionRoute.CANONICAL,
            deadline=lima.deadline,
            trusted_root=lima.root,
        ),
    ):
        pytest.fail("non-durable WSL hold admitted guest preparation")
    assert lima.route.guest.calls == 0 and lima.connections == [] and lima.fake.calls == []
    assert lima.native.events.count("dispatch") == 1


@pytest.mark.parametrize("lima", [("wsl2", "Running")], indirect=True)
def test_canonical_wsl_restarted_distribution_refuses_before_account_preparation(database, tmp_path, lima):
    lima.route.guest.init_ticks += 1
    with (
        pytest.raises(StateError),
        vm_execution_context(
            database,
            "box",
            lima.platform,
            RunContext(config=canonical_config(tmp_path)),
            route=ExecutionRoute.CANONICAL,
            deadline=lima.deadline,
            trusted_root=lima.root,
        ),
    ):
        pytest.fail("restarted WSL distribution admitted a body")
    assert lima.route.guest.calls == 1 and lima.route.accounts.calls == []
    assert lima.access.route.directory.settled and database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("lima", [("unknown", "Running")], indirect=True)
def test_canonical_lima_unproved_driver_refuses_before_inspection(database, tmp_path, lima):
    with (
        pytest.raises(StateError),
        vm_execution_context(
            database,
            "box",
            lima.platform,
            RunContext(config=canonical_config(tmp_path)),
            route=ExecutionRoute.CANONICAL,
            deadline=lima.deadline,
            trusted_root=lima.root,
        ),
    ):
        pytest.fail("unproved external driver admitted a context")
    assert lima.fake.calls == [] and not lima.host_calls and not lima.connections
    assert database.operations.inspect(_scope()) is None


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
