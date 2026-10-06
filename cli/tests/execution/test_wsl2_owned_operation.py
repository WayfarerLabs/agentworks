"""Private WSL composition consumes core custody before selecting any route."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._wsl2_owned_download import WSL2OwnedDownload
from agentworks.execution._wsl2_owned_managed_job import WSL2OwnedManagedJob
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection
from agentworks.operations import OperationOwner
from tests.execution.test_wsl2_owned_download import _acquire_owner, _close_owner
from tests.execution.test_wsl2_platform_hold import FakeNative, FakeObserver
from tests.vms.test_target_preparation import _vm


@pytest.mark.parametrize("composition", [WSL2OwnedDownload, WSL2OwnedManagedJob])
@pytest.mark.parametrize("entry", ["constructor", "factory"])
@pytest.mark.parametrize("invalid", ["object", "subclass", "resource", "name"])
def test_invalid_owner_refuses_before_route_selection_or_native_effects(
    tmp_path: Path, composition: type[WSL2OwnedOperation], entry: str, invalid: str
) -> None:
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
                    database.operations,
                    _vm(),
                    platform,
                    cast(RunContext, object()),
                    owner=supplied,
                    deadline=Deadline.after(30),
                    native=native,
                    observer=observer,
                )
            else:
                composition(
                    database.operations,
                    _vm(),
                    platform,
                    cast(RunContext, object()),
                    ProviderLocator("wsl2:registration"),
                    WSL2Connection("Ubuntu", "admin", "wsl.exe"),
                    RuntimeSelection(RuntimeTargetOS.LINUX),
                    owner=supplied,
                    native=native,
                    observer=observer,
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
    with closing(Database(tmp_path / "state.db")) as database:
        owner = _acquire_owner(database)
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        original = RuntimeError("locator unavailable")
        platform.observe_provider_locator.side_effect = original
        with pytest.raises(RuntimeError) as caught:
            WSL2OwnedDownload.from_platform(
                database.operations,
                _vm(),
                platform,
                cast(RunContext, object()),
                owner=owner,
                deadline=Deadline.after(30),
            )
        assert caught.value is original
        platform.resolve_native_execution_binding.assert_not_called()
        obligation = owner.register_lifecycle_obligation("caller-next-step", payload_version=1, payload=b"next")
        obligation.resolve()
        _close_owner(owner)
