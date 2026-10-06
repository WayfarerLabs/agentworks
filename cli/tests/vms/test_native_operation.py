"""Real-claim checks for the private existing-VM native operation."""

from __future__ import annotations

import gc
import json
import os
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import (
    Database,
    OperationClaimState,
    OperationOwnership,
    OperationResourceKind,
    OperationScope,
    VMStatus,
)
from agentworks.db.operations import OperationRepository
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _wsl2_owned_operation
from agentworks.execution._file_operation import _ActiveFileUpload
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, EndOfInput, FiniteInput, PreparedInvocation
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.operations import OperationOwner
from agentworks.vms._native_operation import NativeVMOperationControlFact, native_vm_operation
from agentworks.vms.target_preparation import (
    VMTargetPreparation,
    VMTargetPreparationControlFact,
    VMTargetPreparationFailure,
    VMTargetPreparationStatus,
)
from tests.execution.test_target_identity import LocalCarrier, SyntheticCarrier
from tests.execution.test_wsl2_owned_download import GuestThenFileCarrier, _install_file_fixtures
from tests.execution.test_wsl2_platform_hold import FakeNative, FakeObserver
from tests.vms.test_target_preparation import _MARKER

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db import VMRow

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux helper proof")


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    with closing(Database(tmp_path / "state.db")) as db:
        db.insert_vm("box", "wsl2", "box", admin_username="admin", instance_marker=_MARKER)
        db.update_vm_platform_metadata("box", {"distro_name": "Ubuntu"})
        yield db


def _scope() -> OperationScope:
    return OperationScope(OperationResourceKind.VM, "box")


def _root(tmp_path: Path) -> PurePosixPath:
    return PurePosixPath(str(tmp_path / "files"))


def test_conflicting_claim_prevents_passive_provider_work(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = OperationOwner.acquire(database.operations, _scope(), "other")
    platform = WSL2Platform("wsl2", {})
    observed = False

    def observe(vm: VMRow, ctx: RunContext, *, deadline: Deadline) -> VMStatus:
        nonlocal observed
        observed = True
        return VMStatus.RUNNING

    monkeypatch.setattr(platform, "observe_execution_power", observe)
    with (
        pytest.raises(StateError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=_root(tmp_path)
        ),
    ):
        pass
    assert not observed
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_site_mismatch_refuses_before_power_observation(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("other-site", {})
    observed = False

    def observe(vm: VMRow, ctx: RunContext, *, deadline: Deadline) -> VMStatus:
        nonlocal observed
        observed = True
        return VMStatus.RUNNING

    monkeypatch.setattr(platform, "observe_execution_power", observe)
    with (
        pytest.raises(StateError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=_root(tmp_path)
        ),
    ):
        pass
    assert not observed
    assert database.operations.inspect(_scope()) is None


def test_late_power_result_refuses_before_route(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    deadline = Deadline.after(10)
    routed = False

    def observe(vm: VMRow, ctx: RunContext, *, deadline: Deadline) -> VMStatus:
        assert database.operations.inspect(_scope()) is not None
        object.__setattr__(deadline, "expires_at", 0.0)
        return VMStatus.RUNNING

    def locator(vm: VMRow, ctx: RunContext, *, deadline: Deadline) -> ProviderLocator:
        nonlocal routed
        routed = True
        return ProviderLocator("wsl2:registration")

    monkeypatch.setattr(platform, "observe_execution_power", observe)
    monkeypatch.setattr(platform, "observe_provider_locator", locator)
    with (
        pytest.raises(StateError),
        native_vm_operation(database, "box", platform, RunContext(), deadline=deadline, trusted_root=_root(tmp_path)),
    ):
        pass
    assert not routed
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize(
    ("power", "stopped", "permits_route"),
    [
        (VMStatus.STOPPED, True, False),
        (VMStatus.UNKNOWN, False, False),
        (VMStatus.RUNNING, True, True),
    ],
)
def test_fresh_intent_and_power_gate_precedes_route_and_wake(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    power: VMStatus,
    stopped: bool,
    permits_route: bool,
) -> None:
    platform = WSL2Platform("wsl2", {})
    database.set_operator_stopped("box", stopped)
    events: list[str] = []

    def observe(vm: VMRow, ctx: RunContext, *, deadline: Deadline) -> VMStatus:
        assert database.operations.inspect(_scope()) is not None
        assert vm.operator_stopped is stopped
        events.append("power")
        return power

    def locator(vm: VMRow, ctx: RunContext, *, deadline: Deadline) -> ProviderLocator:
        events.append("route")
        raise StateError("test route stop")

    monkeypatch.setattr(platform, "observe_execution_power", observe)
    monkeypatch.setattr(platform, "observe_provider_locator", locator)
    with (
        pytest.raises(StateError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=_root(tmp_path)
        ),
    ):
        pass
    assert events == (["power", "route"] if permits_route else ["power"])
    assert database.operations.inspect(_scope()) is None


class _RouteCarrier:
    def __init__(self, database: Database) -> None:
        self.guest = GuestThenFileCarrier(database)
        gid = os.getegid()
        identity = IdentityExpectation(os.geteuid(), gid, tuple(sorted(set(os.getgroups()) | {gid})))
        self.accounts = SyntheticCarrier({"admin": identity, "root": IdentityExpectation(0, 0, (0,))})
        self.local = LocalCarrier()
        self.local_deadlines: list[Deadline] = []
        self.routes: list[WSL2Connection] = []
        self.ownership: OperationOwnership | None = None

    def execute(
        self, carrier: WSL2Carrier, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline
    ) -> CarrierReport:
        claim = self.guest.database.operations.inspect(_scope())
        assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
        if self.ownership is None:
            self.ownership = claim.ownership
        assert claim.ownership == self.ownership
        self.routes.append(carrier.connection)
        assert carrier.connection.distribution == "Ubuntu" and carrier.connection.wsl_executable == "wsl.exe"
        if carrier.connection.user == "root":
            assert self.guest.calls == 0 and isinstance(io.input, EndOfInput)
            return self.guest.execute(invocation, io=io, deadline=deadline)
        assert carrier.connection.user == "admin" and self.guest.calls == 1
        if isinstance(io.input, FiniteInput):
            try:
                request = json.loads(io.input.data)
            except (TypeError, ValueError):
                request = None
            if isinstance(request, dict) and "account" in request:
                return self.accounts.execute(invocation, io=io, deadline=deadline)
        self.local_deadlines.append(deadline)
        return self.local.execute(invocation, io=io, deadline=deadline)


def _install_route(
    database: Database, platform: WSL2Platform, monkeypatch: pytest.MonkeyPatch
) -> tuple[_RouteCarrier, FakeNative, FakeObserver]:
    route = _RouteCarrier(database)
    native = FakeNative([])
    observer = FakeObserver([])
    connection = WSL2Connection("Ubuntu", "admin", "wsl.exe")
    binding = NativeExecutionBinding(WSL2Carrier(connection), "admin", RuntimeSelection(RuntimeTargetOS.LINUX))
    monkeypatch.setattr(platform, "observe_execution_power", lambda vm, ctx, *, deadline: VMStatus.RUNNING)
    monkeypatch.setattr(
        platform, "observe_provider_locator", lambda vm, ctx, *, deadline: ProviderLocator("wsl2:registration")
    )
    monkeypatch.setattr(platform, "resolve_native_execution_binding", lambda vm, ctx, *, deadline, config: binding)
    monkeypatch.setattr(_wsl2_owned_operation, "WindowsWSL2HostClient", lambda: native)
    monkeypatch.setattr(_wsl2_owned_operation, "WSL2GuestObserver", lambda connection: observer)
    monkeypatch.setattr(
        WSL2Carrier,
        "execute",
        lambda selected, invocation, *, io, deadline: route.execute(selected, invocation, io=io, deadline=deadline),
    )
    return route, native, observer


def test_prepared_views_share_claim_and_clean_teardown(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    root = Path(_root(tmp_path))
    root.mkdir()
    root.joinpath("source").write_bytes(b"native-file")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    _install_file_fixtures(monkeypatch, tmp_path, scratch)
    views = None
    body_deadline = Deadline.after(30)
    with native_vm_operation(
        database, "box", platform, RunContext(), deadline=body_deadline, trusted_root=PurePosixPath(root)
    ) as selected:
        views = selected
        claim = database.operations.inspect(_scope())
        assert claim is not None and claim.ownership == selected.owner.ownership
        assert selected.file_operation._owner is selected.owner
        assert selected.execution_operation._owner is selected.owner
        assert selected.files.stat(PurePosixPath(root / "source")) is not None
        read = selected.files.read_file(PurePosixPath(root / "source"), max_bytes=64)
        assert read is not None
        result = selected.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT, deadline=Deadline(None))
        assert result is not None
        assert route.local_deadlines[-1] is body_deadline
    assert views is not None
    assert native.events and "dispatch" in native.events
    assert route.guest.owner_id == views.owner.ownership.operation_id
    assert route.ownership == views.owner.ownership
    assert route.guest.calls == 1
    assert route.accounts.calls == ["admin", "root"]
    assert route.local.calls
    assert route.routes[0] == WSL2Connection("Ubuntu", "root", "wsl.exe")
    assert all(connection == WSL2Connection("Ubuntu", "admin", "wsl.exe") for connection in route.routes[1:])
    assert observer.events == ["observe"]
    assert database.operations.inspect(_scope()) is None
    with pytest.raises(StateError):
        views.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    destination = tmp_path / "not-staged"
    with pytest.raises(StateError):
        views.files.download(PurePosixPath(root / "source"), destination, max_bytes=64)
    assert not destination.exists()
    assert route.local.calls >= 3


def test_never_created_hold_releases_without_ready(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    native.never_created = True
    monkeypatch.setattr(native, "read_stdout_line", lambda limit, deadline: b"")
    with (
        pytest.raises(ValidationError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ),
    ):
        pass
    assert native.events and "dispatch" in native.events
    assert not observer.events
    assert route.guest.calls == 0
    assert database.operations.inspect(_scope()) is None


def test_native_view_cannot_escape_expired_body_budget_and_cleanup_uses_new_one(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, _, observer = _install_route(database, platform, monkeypatch)
    body_deadline = Deadline.after(30)
    with (
        pytest.raises(StateError) as teardown,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=body_deadline, trusted_root=_root(tmp_path)
        ) as views,
    ):
        object.__setattr__(body_deadline, "expires_at", 0.0)
        with pytest.raises(ValidationError):
            views.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT, deadline=Deadline(None))
        assert route.local.calls == 0
    fact = teardown.value.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    assert database.operations.inspect(_scope()) is not None
    fact.retry_cleanup(Deadline.after(10))
    assert observer.events == ["observe"]
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("committed", [False, True])
def test_claim_release_interrupt_retries_only_finalization(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    repository = database.operations
    original_release = OperationRepository.release_resolved
    release_calls = 0
    control = KeyboardInterrupt()

    def interrupted_release(selected_repository: OperationRepository, ownership: OperationOwnership) -> None:
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            if committed:
                original_release(selected_repository, ownership)
            raise control
        original_release(selected_repository, ownership)

    monkeypatch.setattr(OperationRepository, "release_resolved", interrupted_release)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ) as views,
    ):
        pass
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    assert (repository.inspect(_scope()) is None) is committed
    with pytest.raises(StateError):
        views.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    dispatches = native.events.count("dispatch")
    observations = list(observer.events)
    control.__traceback__ = None
    control.__context__ = None
    gc.collect()
    fact.retry_cleanup(Deadline.after(10))
    fact.retry_cleanup(Deadline.after(10))
    assert repository.inspect(_scope()) is None
    assert release_calls == (1 if committed else 2)
    assert native.events.count("dispatch") == dispatches
    assert observer.events == observations
    assert route.local.calls == 0


def test_uncertain_hold_keeps_claim_and_stops_new_body_admission(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, _, observer = _install_route(database, platform, monkeypatch)
    observer.presence = GuestAnchorPresence.UNKNOWN
    selected = None
    with (
        pytest.raises(StateError) as raised,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ) as views,
    ):
        selected = views
    assert selected is not None
    assert database.operations.inspect(_scope()) is not None
    assert observer.events == ["observe"]
    with pytest.raises(StateError):
        selected.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    assert route.local.calls == 0
    fact = raised.value.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    observer.presence = GuestAnchorPresence.ABSENT_CONFIRMED
    fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None


def test_interruption_keeps_original_control_and_uncertain_claim(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    _, _, observer = _install_route(database, platform, monkeypatch)
    observer.presence = GuestAnchorPresence.UNKNOWN
    control = KeyboardInterrupt()
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ),
    ):
        raise control
    assert caught.value is control
    assert database.operations.inspect(_scope()) is not None
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    observer.presence = GuestAnchorPresence.ABSENT_CONFIRMED
    fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None


def test_interrupted_pre_yield_hold_retains_exact_cleanup_without_traceback(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, native, _ = _install_route(database, platform, monkeypatch)
    control = KeyboardInterrupt()
    native.never_created = True
    native.fail_spawn = control
    original_spawn = native.spawn_owned

    def interrupted_spawn(argv: tuple[str, ...], deadline: Deadline) -> None:
        try:
            original_spawn(argv, deadline)
        finally:
            object.__setattr__(deadline, "expires_at", 0.0)

    monkeypatch.setattr(native, "spawn_owned", interrupted_spawn)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ),
    ):
        pytest.fail("startup interruption must not yield")
    assert caught.value is control
    assert database.operations.inspect(_scope()) is not None
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    control.__traceback__ = None
    control.__context__ = None
    gc.collect()
    with pytest.raises(ValidationError):
        fact.retry_cleanup(Deadline(None))
    with pytest.raises(StateError):
        fact._workflow.owner.borrow()
    assert native.events.count("dispatch") == 1
    fact.retry_cleanup(Deadline.after(10))
    fact.retry_cleanup(Deadline.after(10))
    assert native.events.count("dispatch") == 1
    assert route.guest.calls == 0
    assert database.operations.inspect(_scope()) is None


def test_preparation_control_fact_survives_failed_teardown_handoff(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    _, native, observer = _install_route(database, platform, monkeypatch)
    observer.presence = GuestAnchorPresence.UNKNOWN
    control = KeyboardInterrupt()
    preparation = VMTargetPreparation(
        VMTargetPreparationStatus.FAILED,
        None,
        None,
        failure=VMTargetPreparationFailure.DISPATCH,
    )
    original_fact = VMTargetPreparationControlFact(preparation)

    def interrupt_preparation(*args: object, **kwargs: object) -> VMTargetPreparation:
        raise control from original_fact

    monkeypatch.setattr(_wsl2_owned_operation, "prepare_managed_vm_target_from_platform", interrupt_preparation)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ),
    ):
        pytest.fail("preparation interruption must not yield")
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    assert fact.__cause__ is original_fact
    assert original_fact.preparation is preparation
    assert database.operations.inspect(_scope()) is not None
    control.__traceback__ = None
    gc.collect()
    observer.presence = GuestAnchorPresence.ABSENT_CONFIRMED
    fact.retry_cleanup(Deadline.after(10))
    assert native.events.count("dispatch") == 1
    assert database.operations.inspect(_scope()) is None


def test_local_download_call_custody_blocks_aggregate_release(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    _install_route(database, platform, monkeypatch)
    selected = None
    call = None
    with (
        pytest.raises(StateError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ) as views,
    ):
        selected = views
        call = views.file_operation.begin_local_download()
    assert selected is not None and call is not None
    assert database.operations.inspect(_scope()) is not None
    assert selected.file_operation.has_unfinished_local_download_call
    selected.file_operation.finish_local_download(call)
    with pytest.raises(StateError):
        selected.file_operation.begin_local_download()


def test_active_package_registry_blocks_aggregate_release(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = WSL2Platform("wsl2", {})
    _install_route(database, platform, monkeypatch)
    selected = None
    with (
        pytest.raises(StateError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ) as views,
    ):
        selected = views
        borrow = views.owner.borrow()
        # Model retained package registry custody after its borrow has settled.
        active = cast("_ActiveFileUpload", object())
        views.file_operation._active_package_uploads[id(active)] = active
        borrow.close()
    assert selected is not None
    assert database.operations.inspect(_scope()) is not None
    assert selected.file_operation.active_package_uploads
    with pytest.raises(StateError):
        selected.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
