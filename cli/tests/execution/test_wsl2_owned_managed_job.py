"""Private selected WSL2 managed-start composition and custody."""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution import _wsl2_owned_managed_job as managed
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import (
    ManagedLaunchState,
    ManagedRunIdentity,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunRepository,
)
from agentworks.execution._managed_start_operation import ManagedStartOutcome
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._wsl2_owned_managed_job import WSL2ManagedStartStatus, WSL2OwnedManagedJob
from agentworks.execution._wsl2_owned_operation import WSL2RouteRefusal, WSL2RouteStatus
from agentworks.execution._wsl2_platform_hold import OBLIGATION_KIND
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, PreparedInvocation
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from agentworks.execution.models import Command, Input, Output
from agentworks.operations import LifecycleObligation, OperationOwner
from tests.execution.test_wsl2_owned_download import GuestThenFileCarrier, _acquire_owner, _close_owner
from tests.execution.test_wsl2_platform_hold import FakeNative, FakeObserver
from tests.vms.test_target_preparation import _vm

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private WSL fixture requires Linux")

RUN = ManagedRunIdentity("e" * 32)
OWNER = ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7")
ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)
WORKLOAD = IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE)
CONNECTION = WSL2Connection("Ubuntu", "admin", "wsl.exe")


def _subject(
    database: Database,
    owner: OperationOwner,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bindings: list[NativeExecutionBinding] | None = None,
    locators: list[object] | None = None,
) -> tuple[WSL2OwnedManagedJob, Mock, GuestThenFileCarrier]:
    local_delivery = LocalDeliveryCustody()
    guest_carrier = GuestThenFileCarrier(database)

    def execute(
        selected: WSL2Carrier,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        assert selected.connection == WSL2Connection(CONNECTION.distribution, "root", CONNECTION.wsl_executable)
        return guest_carrier.execute(invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(WSL2Carrier, "execute", execute)
    platform = Mock(spec=WSL2Platform)
    platform.site_name = "local"
    platform.observe_provider_locator.side_effect = locators or [ProviderLocator("wsl2:registration")] * 5
    first = NativeExecutionBinding(WSL2Carrier(CONNECTION), CONNECTION.user, RuntimeSelection(RuntimeTargetOS.LINUX))
    platform.resolve_native_execution_binding.side_effect = bindings or [first, first]
    subject = WSL2OwnedManagedJob.from_platform(
        _vm(),
        platform,
        cast(RunContext, object()),
        owner=owner,
        deadline=Deadline.after(30),
        native=FakeNative([]),
        observer=FakeObserver([]),
        provider_custody=local_delivery,
    )
    assert subject is not None
    assert subject.owner is owner
    assert subject.hold._owner is owner
    return subject, platform, guest_carrier


def _start(
    subject: WSL2OwnedManagedJob, database: Database, *, deadline: Deadline | None = None
) -> WSL2ManagedStartStatus:
    return subject.start_job(
        ManagedRunRepository(database),
        Command(["/usr/bin/true"]),
        workload_plan=WORKLOAD,
        root_plan=ROOT,
        run_owner=OWNER,
        input=Input.eof(),
        output=Output.capture(128),
        env=None,
        cwd=None,
        sensitive=False,
        deadline=deadline if deadline is not None else Deadline.after(30),
        obligation_id="b" * 32,
        identity=RUN,
    )


def test_managed_start_passes_selected_route_guest_and_caller_identity_without_auto_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        subject, platform, carrier = _subject(database, owner, monkeypatch)
        outcome = ManagedStartOutcome(launch_state=ManagedLaunchState.RECEIPT_CONFIRMED)
        start = Mock(return_value=outcome)
        monkeypatch.setattr(managed, "start_bound_managed_job", start)

        assert _start(subject, database) is WSL2ManagedStartStatus.ATTEMPTED
        with pytest.raises(ValidationError):
            _start(subject, database)
        assert start.call_count == 1
        assert subject.start_outcome is outcome
        assert carrier.calls == 1
        assert platform.observe_provider_locator.call_count == 5
        assert platform.resolve_native_execution_binding.call_count == 2
        kwargs = start.call_args.kwargs
        assert kwargs["identity"] is RUN
        assert kwargs["run_owner"] is OWNER
        assert kwargs["workload_plan"] is WORKLOAD
        assert kwargs["root_plan"] is ROOT
        assert kwargs["carrier"] is subject._carrier
        assert kwargs["owner"] is subject.owner
        assert subject.preparation is not None
        assert kwargs["target"] is subject.preparation.target
        assert subject.ready is not None and subject.ready.identity is not None
        assert kwargs["guest"].init_start_ticks == subject.ready.identity.init_start_ticks
        assert callable(kwargs["before_dispatch"])
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        assert subject.release_hold_if_settled(Deadline.after(30), safe=True)
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        next_step = owner.register_lifecycle_obligation("caller-next-step", payload_version=1, payload=b"next")
        next_step.resolve()
        _close_owner(owner)
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_changed_connection_refuses_before_reservation_and_releases_settled_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        first = NativeExecutionBinding(
            WSL2Carrier(CONNECTION), CONNECTION.user, RuntimeSelection(RuntimeTargetOS.LINUX)
        )
        changed = WSL2Connection("Debian", "admin", "wsl.exe")
        second = NativeExecutionBinding(WSL2Carrier(changed), changed.user, RuntimeSelection(RuntimeTargetOS.LINUX))
        subject, _, carrier = _subject(database, owner, monkeypatch, bindings=[first, second])
        start = Mock(side_effect=AssertionError("managed start after route change"))
        monkeypatch.setattr(managed, "start_bound_managed_job", start)

        assert _start(subject, database) is WSL2ManagedStartStatus.REFUSED
        assert carrier.calls == 1
        start.assert_not_called()
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is not None
        _close_owner(owner)
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is None


@pytest.mark.parametrize("observation", [ProviderLocatorUnavailable(), object()])
def test_unconfirmed_prestart_locator_retains_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation: object
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        stable = ProviderLocator("wsl2:registration")
        subject, _, carrier = _subject(database, owner, monkeypatch, locators=[stable] * 3 + [observation])
        start = Mock(side_effect=AssertionError("managed start after unconfirmed route"))
        monkeypatch.setattr(managed, "start_bound_managed_job", start)

        assert _start(subject, database) is WSL2ManagedStartStatus.RETAINED
        assert carrier.calls == 1
        start.assert_not_called()
        assert database.operations.inspect(subject.owner.ownership.scope) is not None


def test_exceptional_prestart_locator_preserves_original_and_retains_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        subject, platform, carrier = _subject(database, owner, monkeypatch)
        original = RuntimeError("locator observation failed")
        platform.observe_provider_locator.side_effect = [ProviderLocator("wsl2:registration")] * 2 + [original]

        with pytest.raises(RuntimeError) as caught:
            _start(subject, database)
        assert caught.value is original
        assert carrier.calls == 1
        assert subject.release_hold_if_settled(Deadline.after(30), safe=True)
        assert database.operations.inspect(subject.owner.ownership.scope) is not None


@pytest.mark.parametrize("change", ["locator", "connection", "runtime", "unavailable", "invalid", "late", "exception"])
def test_dispatch_route_callback_classifies_and_retains_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        stable = ProviderLocator("wsl2:registration")
        replacement = ProviderLocator("wsl2:replacement")
        locators: list[object] = [stable for _ in range(5)]
        locators.append(replacement if change == "locator" else stable)
        if change == "unavailable":
            locators[-1] = ProviderLocatorUnavailable()
        elif change == "invalid":
            locators[-1] = object()
        else:
            locators.append(stable)
        first = NativeExecutionBinding(
            WSL2Carrier(CONNECTION), CONNECTION.user, RuntimeSelection(RuntimeTargetOS.LINUX)
        )
        changed_connection = WSL2Connection("Debian", "admin", "wsl.exe")
        second = NativeExecutionBinding(
            WSL2Carrier(changed_connection if change == "connection" else CONNECTION),
            CONNECTION.user,
            RuntimeSelection(RuntimeTargetOS.LINUX, "/different/python3")
            if change == "runtime"
            else first.runtime_selection,
        )
        subject, platform, carrier = _subject(
            database, owner, monkeypatch, locators=locators, bindings=[first, first, second]
        )
        original = RuntimeError("locator observation failed")

        deadline = Deadline.after(30)

        def observe_late(*args: object, **kwargs: object) -> ProviderLocator:
            object.__setattr__(deadline, "expires_at", 0.0)
            return stable

        def start(*args: object, **kwargs: object) -> ManagedStartOutcome:
            callback = kwargs["before_dispatch"]
            assert callable(callback)
            if change == "exception":
                platform.observe_provider_locator.side_effect = original
            elif change == "late":
                platform.observe_provider_locator.side_effect = observe_late
            callback()
            raise AssertionError("route callback admitted changed route")

        monkeypatch.setattr(managed, "start_bound_managed_job", start)
        with pytest.raises(RuntimeError if change == "exception" else WSL2RouteRefusal) as caught:
            _start(subject, database, deadline=deadline)
        if change == "exception":
            assert caught.value is original
        else:
            expected = (
                WSL2RouteStatus.CHANGED
                if change in {"locator", "connection", "runtime"}
                else WSL2RouteStatus.UNCONFIRMED
            )
            assert isinstance(caught.value, WSL2RouteRefusal)
            assert caught.value.status is expected
        assert carrier.calls == 1
        assert subject.release_hold_if_settled(Deadline.after(30), safe=True)
        assert database.operations.inspect(subject.owner.ownership.scope) is not None


@pytest.mark.parametrize("change", ["runtime", "late-locator"])
def test_changed_runtime_or_late_locator_refuses_before_managed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        first = NativeExecutionBinding(
            WSL2Carrier(CONNECTION), CONNECTION.user, RuntimeSelection(RuntimeTargetOS.LINUX)
        )
        second = NativeExecutionBinding(
            WSL2Carrier(CONNECTION),
            CONNECTION.user,
            RuntimeSelection(RuntimeTargetOS.LINUX, "/different/python3")
            if change == "runtime"
            else first.runtime_selection,
        )
        stable = ProviderLocator("wsl2:registration")
        locators = [stable] * 4 + [ProviderLocator("wsl2:replacement") if change == "late-locator" else stable]
        subject, _, carrier = _subject(database, owner, monkeypatch, bindings=[first, second], locators=locators)
        start = Mock(side_effect=AssertionError("managed start after selected route changed"))
        monkeypatch.setattr(managed, "start_bound_managed_job", start)

        assert _start(subject, database) is WSL2ManagedStartStatus.REFUSED
        assert carrier.calls == 1
        start.assert_not_called()
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is not None
        _close_owner(owner)
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is None


def test_exact_hold_settles_before_other_obligation_and_owner_closes_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        subject, _, _ = _subject(database, owner, monkeypatch)
        other: LifecycleObligation | None = None

        def start(*args: object, **kwargs: object) -> ManagedStartOutcome:
            nonlocal other
            other = subject.owner.register_lifecycle_obligation("managed-start", payload_version=1, payload=b"pending")
            other.mark_possible_effect()
            return ManagedStartOutcome(launch_state=ManagedLaunchState.RECEIPT_CONFIRMED)

        monkeypatch.setattr(managed, "start_bound_managed_job", start)
        assert _start(subject, database) is WSL2ManagedStartStatus.ATTEMPTED
        assert subject.release_hold_if_settled(Deadline.after(30), safe=True)
        rows = database.operations.list_lifecycle_obligations(subject.owner.ownership)
        assert any(
            row.obligation_kind == OBLIGATION_KIND and row.state is LifecycleObligationState.RESOLVED for row in rows
        )
        assert any(
            row.obligation_kind == "managed-start" and row.state is LifecycleObligationState.POSSIBLE_EFFECT
            for row in rows
        )
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        assert other is not None
        other.resolve()
        assert subject.release_hold_if_settled(Deadline.after(30), safe=True)
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        _close_owner(owner)
        assert database.operations.inspect(subject.owner.ownership.scope) is None


@pytest.mark.parametrize("raises", [False, True])
def test_uncertain_start_or_escaping_control_retains_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        subject, _, _ = _subject(database, owner, monkeypatch)
        if raises:
            monkeypatch.setattr(managed, "start_bound_managed_job", Mock(side_effect=RuntimeError("uncertain")))
            with pytest.raises(RuntimeError, match="uncertain"):
                _start(subject, database)
        else:
            outcome = ManagedStartOutcome(
                launch_state=ManagedLaunchState.POSSIBLE_DISPATCH,
                pending_remote_effects=True,
                coordination_uncertain=True,
                requires_owner_retention=True,
            )
            monkeypatch.setattr(managed, "start_bound_managed_job", Mock(return_value=outcome))
            assert _start(subject, database) is WSL2ManagedStartStatus.ATTEMPTED
        assert subject.release_hold_if_settled(Deadline.after(30), safe=True)
        rows = database.operations.list_lifecycle_obligations(subject.owner.ownership)
        assert any(row.state is LifecycleObligationState.RESOLVED for row in rows)
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
