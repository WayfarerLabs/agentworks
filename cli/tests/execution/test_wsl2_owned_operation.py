"""Private WSL composition consumes core custody before selecting any route."""

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
from agentworks.execution import _wsl2_owned_operation
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._wsl2_owned_download import WSL2OwnedDownload
from agentworks.execution._wsl2_owned_managed_job import WSL2OwnedManagedJob
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, PreparedInvocation
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from agentworks.operations import OperationOwner
from tests.execution.test_wsl2_owned_download import GuestThenFileCarrier, _acquire_owner, _close_owner
from tests.execution.test_wsl2_platform_hold import FakeNative, FakeObserver
from tests.vms.test_target_preparation import _vm


def test_durable_ready_requires_the_full_selected_anchor_payload(tmp_path: Path) -> None:
    local_delivery = LocalDeliveryCustody()
    from dataclasses import replace

    from agentworks.execution._wsl2_platform_hold import decode_hold_payload, encode_hold_payload

    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        selected = WSL2OwnedOperation(
            _vm(),
            platform,
            RunContext(),
            ProviderLocator("wsl2:registration"),
            WSL2Connection("Ubuntu", "admin", "wsl.exe"),
            RuntimeSelection(RuntimeTargetOS.LINUX),
            owner=owner,
            native=FakeNative([]),
            observer=FakeObserver([]),
            provider_custody=local_delivery,
        )
        ready = selected.hold.start(Deadline.after(10))
        assert selected._ready_is_durable(ready)  # noqa: SLF001
        row = owner.list_lifecycle_obligations()[0]
        substitute = replace(decode_hold_payload(row.payload), nonce="f" * 32)
        database.operations.publish_lifecycle_obligation_payload(
            owner.ownership,
            row.obligation_id,
            expected_revision=row.payload_revision,
            payload_version=row.payload_version,
            payload=encode_hold_payload(substitute),
        )
        assert not selected._ready_is_durable(ready)  # noqa: SLF001


@pytest.mark.parametrize("composition", [WSL2OwnedDownload, WSL2OwnedManagedJob])
@pytest.mark.parametrize("entry", ["constructor", "factory"])
@pytest.mark.parametrize("invalid", ["object", "subclass", "resource", "name"])
def test_invalid_owner_refuses_before_route_selection_or_native_effects(
    tmp_path: Path, composition: type[WSL2OwnedOperation], entry: str, invalid: str
) -> None:
    local_delivery = LocalDeliveryCustody()

    class SubclassOwner(OperationOwner):
        pass

    with closing(Database(tmp_path / "state.db")) as database:
        scope = OperationScope(
            OperationResourceKind.PLATFORM_HOST if invalid == "resource" else OperationResourceKind.VM,
            "other" if invalid == "name" else "box",
        )
        owner_type = SubclassOwner if invalid == "subclass" else OperationOwner
        retained = owner_type.acquire(database.operations, scope, "caller-operation")
        supplied = cast(OperationOwner, object()) if invalid == "object" else retained
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        native = FakeNative([])
        observer = FakeObserver([])
        with pytest.raises(ValidationError):
            if entry == "factory":
                composition.from_platform(
                    _vm(),
                    platform,
                    cast(RunContext, object()),
                    owner=supplied,
                    deadline=Deadline.after(30),
                    native=native,
                    observer=observer,
                    provider_custody=local_delivery,
                )
            else:
                composition(
                    _vm(),
                    platform,
                    cast(RunContext, object()),
                    ProviderLocator("wsl2:registration"),
                    WSL2Connection("Ubuntu", "admin", "wsl.exe"),
                    RuntimeSelection(RuntimeTargetOS.LINUX),
                    owner=supplied,
                    native=native,
                    observer=observer,
                    provider_custody=local_delivery,
                )
        platform.observe_provider_locator.assert_not_called()
        platform.resolve_native_execution_binding.assert_not_called()
        assert native.events == [] and observer.events == []
        assert not database.operations.list_lifecycle_obligations(retained.ownership)
        claim = database.operations.inspect(scope)
        assert claim is not None and claim.ownership == retained.ownership
        next_step = retained.register_lifecycle_obligation("caller-next-step", payload_version=1, payload=b"next")
        next_step.resolve()
        _close_owner(retained)
        assert database.operations.inspect(scope) is None


def test_factory_exception_preserves_unsealed_caller_claim(tmp_path: Path) -> None:
    local_delivery = LocalDeliveryCustody()
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        original = RuntimeError("locator unavailable")
        platform.observe_provider_locator.side_effect = original
        with pytest.raises(RuntimeError) as caught:
            WSL2OwnedDownload.from_platform(
                _vm(),
                platform,
                cast(RunContext, object()),
                owner=owner,
                deadline=Deadline.after(30),
                provider_custody=local_delivery,
            )
        assert caught.value is original
        platform.resolve_native_execution_binding.assert_not_called()
        obligation = owner.register_lifecycle_obligation("caller-next-step", payload_version=1, payload=b"next")
        obligation.resolve()
        _close_owner(owner)


@pytest.mark.skipif(sys.platform != "linux", reason="the private WSL fixture requires Linux")
@pytest.mark.parametrize("composition", [WSL2OwnedDownload, WSL2OwnedManagedJob])
@pytest.mark.parametrize("entry", ["constructor", "factory"])
@pytest.mark.parametrize("runtime_path", [None, "/usr/bin/python3"])
def test_hold_reads_and_settles_only_supplied_owners_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    composition: type[WSL2OwnedOperation],
    entry: str,
    runtime_path: str | None,
) -> None:
    local_delivery = LocalDeliveryCustody()
    with (
        closing(Database(tmp_path / "owner.db")) as database,
        closing(Database(tmp_path / "other.db")) as other_database,
    ):
        owner = _acquire_owner(database)
        other_owner = _acquire_owner(other_database)
        other_claim = other_database.operations.inspect(other_owner.ownership.scope)
        carrier = GuestThenFileCarrier(database)

        def execute(
            selected: WSL2Carrier,
            invocation: PreparedInvocation,
            *,
            io: CarrierIO,
            deadline: Deadline,
            custody: LocalDeliveryCustody | None = None,
        ) -> CarrierReport:
            assert selected.connection == WSL2Connection("Ubuntu", "root", "wsl.exe")
            return carrier.execute(invocation, io=io, deadline=deadline, custody=custody)

        monkeypatch.setattr(WSL2Carrier, "execute", execute)
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        locator = ProviderLocator("wsl2:registration")
        connection = WSL2Connection("Ubuntu", "admin", "wsl.exe")
        runtime = RuntimeSelection(RuntimeTargetOS.LINUX, runtime_path)
        platform.observe_provider_locator.return_value = locator
        platform.resolve_native_execution_binding.return_value = NativeExecutionBinding(
            WSL2Carrier(connection), connection.user, runtime
        )
        native = FakeNative([])
        observer = FakeObserver([])
        if entry == "factory":
            subject = composition.from_platform(
                _vm(),
                platform,
                cast(RunContext, object()),
                owner=owner,
                deadline=Deadline.after(30),
                native=native,
                observer=observer,
                provider_custody=local_delivery,
            )
            assert subject is not None
        else:
            subject = composition(
                _vm(),
                platform,
                cast(RunContext, object()),
                locator,
                connection,
                runtime,
                owner=owner,
                native=native,
                observer=observer,
                provider_custody=local_delivery,
            )

        selected_binding = subject.binding
        assert isinstance(selected_binding.carrier, WSL2Carrier)
        assert selected_binding.carrier.connection == connection
        assert selected_binding.delivery_account == connection.user
        assert selected_binding.runtime_selection == runtime
        early = selected_binding._early_guest_facts_route
        assert early is not None and isinstance(early.carrier, WSL2Carrier)
        assert early.carrier.connection == WSL2Connection(connection.distribution, "root", connection.wsl_executable)
        assert early.account == connection.user
        assert subject.start_and_prepare(Deadline.after(30)) is not None
        assert carrier.owner_id == owner.ownership.operation_id
        rows = owner.list_lifecycle_obligations()
        assert rows == database.operations.list_lifecycle_obligations(owner.ownership)
        assert any(row.state is LifecycleObligationState.POSSIBLE_EFFECT for row in rows)
        assert subject.release_hold_if_settled(Deadline.after(30), safe=True)
        assert all(row.state is LifecycleObligationState.RESOLVED for row in owner.list_lifecycle_obligations())
        assert other_database.operations.inspect(other_owner.ownership.scope) == other_claim
        assert not other_owner.list_lifecycle_obligations()
        _close_owner(owner)
        _close_owner(other_owner)


@pytest.mark.parametrize("composition", [WSL2OwnedDownload, WSL2OwnedManagedJob])
@pytest.mark.parametrize("entry", ["constructor", "factory"])
@pytest.mark.parametrize(
    "runtime",
    [RuntimeSelection(RuntimeTargetOS.LINUX, "/custom/python3"), RuntimeSelection(RuntimeTargetOS.DARWIN)],
)
def test_unsupported_runtime_refuses_before_hold_construction_or_native_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    composition: type[WSL2OwnedOperation],
    entry: str,
    runtime: RuntimeSelection,
) -> None:
    local_delivery = LocalDeliveryCustody()
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        locator = ProviderLocator("wsl2:registration")
        connection = WSL2Connection("Ubuntu", "admin", "wsl.exe")
        platform.observe_provider_locator.return_value = locator
        platform.resolve_native_execution_binding.return_value = NativeExecutionBinding(
            WSL2Carrier(connection), connection.user, runtime
        )
        native = FakeNative([])
        observer = FakeObserver([])
        hold = Mock(side_effect=AssertionError("hold constructed for unsupported runtime"))
        monkeypatch.setattr(_wsl2_owned_operation, "WSL2PlatformHold", hold)

        with pytest.raises(ValidationError):
            if entry == "constructor":
                composition(
                    _vm(),
                    platform,
                    RunContext(),
                    locator,
                    connection,
                    runtime,
                    owner=owner,
                    native=native,
                    observer=observer,
                    provider_custody=local_delivery,
                )
            else:
                composition.from_platform(
                    _vm(),
                    platform,
                    RunContext(),
                    owner=owner,
                    deadline=Deadline.after(30),
                    native=native,
                    observer=observer,
                    provider_custody=local_delivery,
                )

        hold.assert_not_called()
        assert native.events == observer.events == []
        assert not owner.list_lifecycle_obligations()
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.ownership == owner.ownership
        follow_up = owner.register_lifecycle_obligation("caller-next-step", payload_version=1, payload=b"next")
        follow_up.resolve()
        _close_owner(owner)
