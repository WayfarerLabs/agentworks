"""Private selected WSL2 managed-start composition and custody."""

from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution import _wsl2_owned_managed_job as managed
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
from agentworks.execution._wsl2_platform_hold import OBLIGATION_KIND
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, PreparedInvocation
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from agentworks.execution.models import Command, Input, Output
from agentworks.operations import LifecycleObligation
from tests.execution.test_wsl2_owned_download import GuestThenFileCarrier
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
    monkeypatch: pytest.MonkeyPatch,
    *,
    bindings: list[NativeExecutionBinding] | None = None,
    locators: list[ProviderLocator] | None = None,
) -> tuple[WSL2OwnedManagedJob, Mock, GuestThenFileCarrier]:
    guest_carrier = GuestThenFileCarrier(database)

    def execute(
        selected: WSL2Carrier, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline
    ) -> CarrierReport:
        assert selected.connection == CONNECTION
        return guest_carrier.execute(invocation, io=io, deadline=deadline)

    monkeypatch.setattr(WSL2Carrier, "execute", execute)
    platform = Mock(spec=WSL2Platform)
    platform.site_name = "local"
    platform.observe_provider_locator.side_effect = locators or [ProviderLocator("wsl2:registration")] * 5
    first = NativeExecutionBinding(
        WSL2Carrier(CONNECTION), CONNECTION.user, RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)
    )
    platform.resolve_native_execution_binding.side_effect = bindings or [first, first]
    subject = WSL2OwnedManagedJob.from_platform(
        database.operations,
        _vm(),
        platform,
        cast(RunContext, object()),
        deadline=Deadline.after(30),
        native=FakeNative([]),
        observer=FakeObserver([]),
    )
    assert subject is not None
    return subject, platform, guest_carrier


def _start(subject: WSL2OwnedManagedJob, database: Database) -> WSL2ManagedStartStatus:
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
        deadline=Deadline.after(30),
        obligation_id="b" * 32,
        identity=RUN,
    )


def test_managed_start_passes_selected_route_guest_and_caller_identity_without_auto_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        subject, platform, carrier = _subject(database, monkeypatch)
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
        assert kwargs["target"] is subject.preparation.target
        assert kwargs["guest"].init_start_ticks == subject.ready.identity.init_start_ticks
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        assert subject.release_if_settled(Deadline.after(30), safe=True)
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_changed_connection_refuses_before_reservation_and_releases_settled_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        first = NativeExecutionBinding(
            WSL2Carrier(CONNECTION), CONNECTION.user, RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)
        )
        changed = WSL2Connection("Debian", "admin", "wsl.exe")
        second = NativeExecutionBinding(
            WSL2Carrier(changed), changed.user, RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)
        )
        subject, _, carrier = _subject(database, monkeypatch, bindings=[first, second])
        start = Mock(side_effect=AssertionError("managed start after route change"))
        monkeypatch.setattr(managed, "start_bound_managed_job", start)

        assert _start(subject, database) is WSL2ManagedStartStatus.REFUSED
        assert carrier.calls == 1
        start.assert_not_called()
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is None


@pytest.mark.parametrize("change", ["runtime", "late-locator"])
def test_changed_runtime_or_late_locator_refuses_before_managed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        first = NativeExecutionBinding(
            WSL2Carrier(CONNECTION), CONNECTION.user, RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)
        )
        second = NativeExecutionBinding(
            WSL2Carrier(CONNECTION),
            CONNECTION.user,
            RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
            if change == "runtime"
            else first.runtime_selection,
        )
        stable = ProviderLocator("wsl2:registration")
        locators = [stable] * 4 + [ProviderLocator("wsl2:replacement") if change == "late-locator" else stable]
        subject, _, carrier = _subject(database, monkeypatch, bindings=[first, second], locators=locators)
        start = Mock(side_effect=AssertionError("managed start after selected route changed"))
        monkeypatch.setattr(managed, "start_bound_managed_job", start)

        assert _start(subject, database) is WSL2ManagedStartStatus.REFUSED
        assert carrier.calls == 1
        start.assert_not_called()
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is None


def test_exact_hold_settles_before_other_obligation_and_owner_closes_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        subject, _, _ = _subject(database, monkeypatch)
        other: LifecycleObligation | None = None

        def start(*args: object, **kwargs: object) -> ManagedStartOutcome:
            nonlocal other
            other = subject.owner.register_lifecycle_obligation("managed-start", payload_version=1, payload=b"pending")
            other.mark_possible_effect()
            return ManagedStartOutcome(launch_state=ManagedLaunchState.RECEIPT_CONFIRMED)

        monkeypatch.setattr(managed, "start_bound_managed_job", start)
        assert _start(subject, database) is WSL2ManagedStartStatus.ATTEMPTED
        assert not subject.release_if_settled(Deadline.after(30), safe=True)
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
        assert subject.release_if_settled(Deadline.after(30), safe=True)
        assert database.operations.inspect(subject.owner.ownership.scope) is None


@pytest.mark.parametrize("raises", [False, True])
def test_uncertain_start_or_escaping_control_retains_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        subject, _, _ = _subject(database, monkeypatch)
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
        assert not subject.release_if_settled(Deadline.after(30), safe=True)
        rows = database.operations.list_lifecycle_obligations(subject.owner.ownership)
        assert any(row.state is LifecycleObligationState.RESOLVED for row in rows)
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
