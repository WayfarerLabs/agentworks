"""Owned preliminary power reads retain exact cleanup custody before admission."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import OperationScope as ContextScope
from agentworks.capabilities.base import RunContext, ScopeLevel
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from agentworks.plugins.proxmox._native_access import ProxmoxOwnedNativePlatformAccess
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.vms._native_operation import NativeVMOperationControlFact, native_vm_operation
from agentworks.vms._wsl2_native_access import WSL2OwnedNativePlatformAccess

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db import VMRow


_MARKER = "0123456789abcdef0123456789abcdef"
_CONTROLS = [OSError, KeyboardInterrupt, SystemExit]


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    with closing(Database(tmp_path / "state.db")) as db:
        db.insert_vm("box", "wsl2", "box", admin_username="admin", instance_marker=_MARKER)
        yield db


def _scope() -> OperationScope:
    return OperationScope(OperationResourceKind.VM, "box")


@dataclass
class _FailingAccess:
    database: Database
    owner: OperationOwner
    custody: LocalDeliveryCustody
    deadline: Deadline
    control: BaseException
    events: list[str]
    retirement: str = "settled"
    binding = None
    preparation = None
    route_check = None

    def observe_power(self, deadline: Deadline) -> VMStatus:
        assert deadline is self.deadline
        claim = self.database.operations.inspect(_scope())
        assert claim is not None and claim.ownership == self.owner.ownership
        self.events.append("power")
        raise self.control

    def prepare(self, power: VMStatus, deadline: Deadline) -> None:
        pytest.fail("failed initial observation prepared a target")

    def settle(self, deadline: Deadline) -> bool:
        assert self.custody.settled
        assert deadline.expires_at is not None and not deadline.expired
        claim = self.database.operations.inspect(_scope())
        assert claim is not None and claim.ownership == self.owner.ownership
        with pytest.raises(StateError):
            self.owner.borrow()
        self.events.append("settle")
        if self.retirement == "error":
            raise OSError("retirement observation failed")
        return self.retirement == "settled"


def _install_failure(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    deadline: Deadline,
    control: BaseException,
    events: list[str],
    *,
    retirement: str = "settled",
) -> tuple[WSL2Platform, list[_FailingAccess]]:
    platform = WSL2Platform("wsl2", {})
    retained: list[_FailingAccess] = []

    def build(vm: VMRow, ctx: RunContext, *, owner: OperationOwner, custody: LocalDeliveryCustody) -> _FailingAccess:
        assert vm.name == "box" and custody.settled
        assert not owner.list_pending_lifecycle_obligations()
        assert not retained
        access = _FailingAccess(database, owner, custody, deadline, control, events, retirement)
        retained.append(access)
        events.append("build")
        return access

    monkeypatch.setattr(platform, "build_native_execution_access", build)
    for method in ("observe_execution_power", "observe_provider_locator", "resolve_native_execution_binding", "start"):
        monkeypatch.setattr(platform, method, lambda *args, **kwargs: pytest.fail("unowned or active provider work"))
    return platform, retained


@pytest.mark.parametrize("control_type", _CONTROLS)
@pytest.mark.parametrize("retirement", ["settled", "refused", "error"])
def test_initial_failure_settles_or_retains_same_access(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
    retirement: str,
) -> None:
    control = control_type("initial power read")
    deadline = Deadline.after(10)
    events: list[str] = []
    platform, retained = _install_failure(database, monkeypatch, deadline, control, events, retirement=retirement)
    with (
        pytest.raises(control_type) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=deadline, trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("failed power read admitted a body")
    assert caught.value is control and events == ["build", "power", "settle"]
    (access,) = retained
    if retirement == "settled":
        assert control.__cause__ is None and database.operations.inspect(_scope()) is None
        return
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    assert fact._workflow.access is access
    assert fact._workflow.owner is access.owner and fact._workflow.local_delivery is access.custody
    with pytest.raises(StateError):
        access.owner.borrow()
    with pytest.raises(OSError if retirement == "error" else StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is not None
    access.retirement = "settled"
    cleanup_deadline = Deadline.after(10)
    assert cleanup_deadline is not deadline
    fact.retry_cleanup(cleanup_deadline)
    assert events == ["build", "power", "settle", "settle", "settle"]
    assert fact._workflow.access is access and database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("control_type", _CONTROLS)
@pytest.mark.parametrize("close_error", [False, True])
def test_local_retirement_defers_same_access_until_retry(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
    close_error: bool,
) -> None:
    control = control_type("initial power read")
    deadline = Deadline.after(10)
    events: list[str] = []
    platform, retained = _install_failure(database, monkeypatch, deadline, control, events)
    retired = False
    original_read = _FailingAccess.observe_power
    cleanup_budgets: list[float | None] = []

    def observe(access: _FailingAccess, budget: Deadline) -> VMStatus:
        # No process is launched. Script only terminal retirement of an inert owner.
        native_owner = access.custody.begin_process()
        monkeypatch.setattr(
            native_owner,
            "snapshot",
            lambda: SimpleNamespace(terminal=SimpleNamespace(cleaned=True) if retired else None),
        )

        def close(process_budget):
            cleanup_budgets.append(process_budget.expires_at)
            if close_error and not retired:
                raise OSError("local retirement unavailable")

        monkeypatch.setattr(native_owner, "close_bounded", close)
        return original_read(access, budget)

    monkeypatch.setattr(_FailingAccess, "observe_power", observe)
    with (
        pytest.raises(control_type) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=deadline, trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("unsettled read admitted a body")
    assert caught.value is control and events == ["build", "power"]
    (access,) = retained
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    assert fact._workflow.access is access and not access.custody.settled
    refused_cleanup = Deadline.after(10)
    with pytest.raises(OSError if close_error else StateError):
        fact.retry_cleanup(refused_cleanup)
    assert events == ["build", "power"] and database.operations.inspect(_scope()) is not None
    with pytest.raises(StateError):
        access.owner.borrow()
    retired = True
    cleanup = Deadline.after(10)
    fact.retry_cleanup(cleanup)
    assert cleanup_budgets == [deadline.expires_at, refused_cleanup.expires_at, cleanup.expires_at]
    assert events == ["build", "power", "settle"]
    assert fact._workflow.access is access and access.custody.settled
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("invalid", ["missing", "site", "no-marker", "marker", "account", "level", "context-vm"])
def test_selection_is_validated_before_factory_or_read(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    platform = WSL2Platform("other" if invalid == "site" else "wsl2", {})
    vm = database.get_vm("box")
    assert vm is not None
    if invalid == "missing":
        monkeypatch.setattr(database, "get_vm", lambda name: None)
    elif invalid in {"no-marker", "marker", "account"}:
        changes = (
            {"admin_username": "\0"}
            if invalid == "account"
            else {"instance_marker": None if invalid == "no-marker" else "invalid"}
        )
        monkeypatch.setattr(database, "get_vm", lambda name: replace(vm, **changes))
    ctx = RunContext()
    if invalid == "level":
        ctx = RunContext(operation_scope=ContextScope(ScopeLevel.SYSTEM))
    elif invalid == "context-vm":
        ctx = RunContext(operation_scope=ContextScope(ScopeLevel.VM, vm="other"))
    for method in ("build_native_execution_access", "observe_execution_power"):
        monkeypatch.setattr(platform, method, lambda *args, **kwargs: pytest.fail("invalid selection reached provider"))
    expected = NotFoundError if invalid == "missing" else StateError if invalid == "site" else ValidationError
    with (
        pytest.raises(expected),
        native_vm_operation(
            database, "box", platform, ctx, deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("invalid selection admitted a body")
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("kind", ["wsl2", "pve"])
@pytest.mark.parametrize("control_type", [None, *_CONTROLS])
def test_actual_owned_observer_delegates_exact_selection_without_preparation(
    database: Database, monkeypatch: pytest.MonkeyPatch, kind: str, control_type: type[BaseException] | None
) -> None:
    platform = (
        WSL2Platform("wsl2", {})
        if kind == "wsl2"
        else ProxmoxPlatform("wsl2", {"api_url": "https://pve.test", "node": "node", "token_id": "token"})
    )
    vm = database.get_vm("box")
    assert vm is not None
    owner = OperationOwner.acquire(database.operations, _scope(), "owned-read")
    custody = LocalDeliveryCustody()
    ctx = RunContext()
    deadline = Deadline.after(10)
    control = control_type("selected observer") if control_type is not None else None
    calls = []

    def observe(selected, context, *, deadline, custody):
        calls.append((selected, context, deadline, custody))
        if control is not None:
            raise control
        return VMStatus.STOPPED

    monkeypatch.setattr(platform, "observe_execution_power", observe)
    for method in ("observe_provider_locator", "resolve_native_execution_binding", "start"):
        monkeypatch.setattr(platform, method, lambda *args, **kwargs: pytest.fail("passive read performed active work"))
    access = platform.build_native_execution_access(vm, ctx, owner=owner, custody=custody)
    assert isinstance(access, (WSL2OwnedNativePlatformAccess, ProxmoxOwnedNativePlatformAccess))
    if control_type is None:
        assert access.observe_power(deadline) is VMStatus.STOPPED
    else:
        with pytest.raises(control_type) as caught:
            access.observe_power(deadline)
        assert caught.value is control
    assert len(calls) == 1
    assert all(actual is expected for actual, expected in zip(calls[0], (vm, ctx, deadline, custody), strict=True))
    assert access.owner is owner and access.custody is custody
    assert access.binding is None and access.preparation is None and access.route_check is None
    assert not owner.list_pending_lifecycle_obligations()
    assert access.settle(deadline) and custody.settled
    if isinstance(access, WSL2OwnedNativePlatformAccess):
        assert access.selected is None
    else:
        assert isinstance(access, ProxmoxOwnedNativePlatformAccess) and access.activation is None
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_actual_proxmox_factory_and_empty_settlement_are_passive(database: Database, monkeypatch: pytest.MonkeyPatch):
    platform = ProxmoxPlatform("wsl2", {"api_url": "https://pve.test", "node": "node", "token_id": "token"})
    vm = database.get_vm("box")
    assert vm is not None
    owner = OperationOwner.acquire(database.operations, _scope(), "passive-access")
    custody = LocalDeliveryCustody()
    for method in ("observe_execution_power", "observe_provider_locator", "resolve_native_execution_binding", "start"):
        monkeypatch.setattr(platform, method, lambda *args, **kwargs: pytest.fail("passive factory performed work"))
    access = platform.build_native_execution_access(vm, RunContext(), owner=owner, custody=custody)
    assert isinstance(access, ProxmoxOwnedNativePlatformAccess)
    assert access.owner is owner and access.custody is custody
    assert access.activation is None and access.locator is None
    assert access.binding is None and access.preparation is None and access.route_check is None
    assert access.settle(Deadline.after(10)) and custody.settled
    assert not owner.list_pending_lifecycle_obligations()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
