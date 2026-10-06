"""Private running-QGA composition with SQLite custody and local packed bodies.

Provider and account observations are scripted. The packed body helper executes
with fixture admission, so these tests do not prove native root or PVE behavior.
"""

from __future__ import annotations

from agentworks.execution._delivery_custody import LocalDeliveryCustody
import builtins
import json
import os
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import (
    Database,
    OperationClaimState,
    OperationOwnership,
    OperationResourceKind,
    OperationScope,
    VMStatus,
)
from agentworks.db.operations import OperationRepository
from agentworks.errors import ExternalError, StateError, ValidationError
from agentworks.execution import _target_identity
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, FiniteInput, PreparedInvocation
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection, _ProxmoxWire
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.result import CheckedExecutionError
from agentworks.operations import OperationAttempt, OperationOwner, release_borrow_after_custody
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.vms._native_operation import NativeVMOperationControlFact, native_vm_operation
from tests.execution.test_target_identity import SyntheticCarrier
from tests.execution.test_wsl2_owned_download import GuestThenFileCarrier
from tests.vms.test_native_operation import _PackedCarrier
from tests.vms.test_target_preparation import _MARKER

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux packed helper proof")


def _scope() -> OperationScope:
    return OperationScope(OperationResourceKind.VM, "box")


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    with closing(Database(tmp_path / "state.db")) as db:
        db.insert_vm("box", "pve", "101", admin_username="admin", instance_marker=_MARKER)
        yield db


class _Route:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.guest = GuestThenFileCarrier(database)
        gid = os.getegid()
        self.accounts = SyntheticCarrier(
            {
                "admin": IdentityExpectation(os.geteuid(), gid, tuple(sorted(set(os.getgroups()) | {gid}))),
                "root": IdentityExpectation(0, 0, (0,)),
            }
        )
        self.local = _PackedCarrier()
        self.locators: list[ProviderLocator] = []
        self.resolutions = 0
        self.ownership: OperationOwnership | None = None
        self.deadlines: list[Deadline] = []

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody | None = None) -> CarrierReport:
        claim = self.database.operations.inspect(_scope())
        assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
        if self.ownership is None:
            self.ownership = claim.ownership
        assert claim.ownership == self.ownership
        self.deadlines.append(deadline)
        if self.guest.calls == 0:
            return self.guest.execute(invocation, io=io, deadline=deadline, custody=custody)
        if isinstance(io.input, FiniteInput):
            try:
                request = json.loads(io.input.data)
            except ValueError:
                request = None
            if isinstance(request, dict) and "account" in request:
                return self.accounts.execute(invocation, io=io, deadline=deadline, custody=custody)
        return self.local.execute(invocation, io=io, deadline=deadline, custody=custody)


def _install(database: Database, monkeypatch: pytest.MonkeyPatch) -> tuple[ProxmoxPlatform, _Route]:
    platform = ProxmoxPlatform("pve", {"api_url": "https://pve.test", "node": "node", "token_id": "token"})
    route = _Route(database)
    binding = NativeExecutionBinding(
        ProxmoxCarrier(ProxmoxConnection("https://pve.test", "node", 101, "token", "secret")),
        "root",
        RuntimeSelection(RuntimeTargetOS.LINUX),
    )

    def locator(vm, ctx, *, deadline):
        assert database.operations.inspect(_scope()) is not None
        return route.locators.pop(0) if route.locators else ProviderLocator("pve:generation")

    def resolve(vm, ctx, *, deadline, config):
        assert database.operations.inspect(_scope()) is not None
        route.resolutions += 1
        return binding

    monkeypatch.setattr(platform, "observe_execution_power", lambda *args, **kwargs: VMStatus.RUNNING)
    monkeypatch.setattr(platform, "observe_provider_locator", locator)
    monkeypatch.setattr(platform, "resolve_native_execution_binding", resolve)
    monkeypatch.setattr(
        ProxmoxCarrier,
        "execute",
        lambda self, invocation, *, io, deadline: route.execute(invocation, io=io, deadline=deadline),
    )
    return platform, route


def test_running_views_share_owner_bootstrap_and_settle(database, tmp_path, monkeypatch):
    platform, route = _install(database, monkeypatch)
    monkeypatch.setattr(_ProxmoxWire, "request_vm_start", lambda *args, **kwargs: pytest.fail("running VM started"))
    monkeypatch.setattr(_ProxmoxWire, "request_guest_info", lambda *args, **kwargs: pytest.fail("running VM waited"))
    database.set_operator_stopped("box", True)
    root = tmp_path / "files"
    root.mkdir()
    source = root / "source"
    source.write_bytes(b"original")
    deadline = Deadline.after(30)
    ctx = RunContext()
    original_import = builtins.__import__
    retired_roots = (
        "agentworks.transports",
        "agentworks.ssh",
        "agentworks.remote_exec",
        "agentworks.harness_setup.runner",
        "agentworks.native_files",
        "agentworks.plugins.proxmox.transport",
    )

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        requested = (name, *(f"{name}.{member}" for member in fromlist or ()))
        if any(
            candidate == retired or candidate.startswith(retired + ".")
            for candidate in requested
            for retired in retired_roots
        ):
            raise AssertionError(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    for retired in retired_roots:
        with pytest.raises(AssertionError):
            __import__(retired)
        parent, _, leaf = retired.rpartition(".")
        with pytest.raises(AssertionError):
            __import__(parent, fromlist=(leaf,))
    with native_vm_operation(
        database, "box", platform, ctx, deadline=deadline, trusted_root=PurePosixPath(root)
    ) as views:
        assert views.file_operation._owner is views.execution_operation._owner is views.owner
        assert views.file_operation._bootstrap is views.execution_operation._bootstrap
        assert views.files._carrier is views.execution._carrier
        metadata = views.files.stat(PurePosixPath(source))
        assert metadata is not None
        views.files.remove(PurePosixPath(source), expected_kind=metadata.kind, expected=metadata.revision)
        effect = root / "command-effect"
        assert views.execution.run(Command(["/usr/bin/touch", str(effect)]), profile=Protection.DIRECT, check=True).ok
        assert effect.exists() and not source.exists()
        assert route.ownership == views.owner.ownership
    assert route.resolutions == 1 and route.local.calls == 3
    assert all(value is deadline for value in route.deadlines)
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize(
    "power,intent",
    [
        (VMStatus.STOPPED, True),
        (VMStatus.UNKNOWN, False),
        (VMStatus.UNKNOWN, True),
        (VMStatus.DEALLOCATED, False),
        (VMStatus.DEALLOCATED, True),
        ("starting", False),
        ("starting", True),
    ],
)
def test_nonrunning_refuses_before_route(database, tmp_path, monkeypatch, power, intent):
    platform, route = _install(database, monkeypatch)
    database.set_operator_stopped("box", intent)
    monkeypatch.setattr(platform, "observe_execution_power", lambda *args, **kwargs: power)
    with (
        pytest.raises(StateError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("nonrunning VM admitted")
    assert route.resolutions == route.guest.calls == route.local.calls == 0
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize(
    "field,value", [("instance_marker", None), ("instance_marker", "invalid"), ("admin_username", "\0")]
)
def test_bad_persisted_selection_refuses_before_observation(database, tmp_path, monkeypatch, field, value):
    platform, route = _install(database, monkeypatch)
    vm = database.get_vm("box")
    assert vm is not None
    monkeypatch.setattr(database, "get_vm", lambda name: replace(vm, **{field: value}))
    monkeypatch.setattr(
        platform, "observe_execution_power", lambda *args, **kwargs: pytest.fail("observed invalid marker")
    )
    with (
        pytest.raises(ValidationError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("invalid marker admitted")
    assert route.guest.calls == 0 and database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("after_guest", [False, True])
def test_changed_locator_suppresses_body(database, tmp_path, monkeypatch, after_guest):
    platform, route = _install(database, monkeypatch)
    route.locators = [ProviderLocator("initial")]
    if after_guest:
        route.locators.append(ProviderLocator("initial"))
    route.locators.append(ProviderLocator("changed"))
    with (
        pytest.raises(StateError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("changed route admitted")
    assert route.guest.calls == int(after_guest) and not route.accounts.calls and route.local.calls == 0
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("action", ["stat", "command"])
@pytest.mark.parametrize("field", ["instance_marker", "boot_id", "init_start_ticks"])
def test_changed_guest_after_plausible_accounts_blocks_body(database, tmp_path, monkeypatch, action, field):
    platform, route = _install(database, monkeypatch)
    effect = tmp_path / "effect"
    with (
        pytest.raises(StateError) as retained,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=PurePosixPath(tmp_path)
        ) as views,
    ):
        assert route.accounts.calls
        route.local.observed = replace(
            route.local.observed,
            **{
                field: {
                    "instance_marker": "b" * 32,
                    "boot_id": "123e4567-e89b-12d3-a456-426614174000",
                    "init_start_ticks": 4097,
                }[field]
            },
        )
        with pytest.raises(ExternalError if action == "stat" else CheckedExecutionError):
            if action == "stat":
                views.files.stat(PurePosixPath(effect))
            else:
                views.execution.run(Command(["/usr/bin/touch", str(effect)]), profile=Protection.DIRECT, check=True)
    assert not effect.exists() and database.operations.inspect(_scope()) is not None
    assert isinstance(retained.value.__cause__, NativeVMOperationControlFact)
    assert retained.value.__cause__._workflow.views is views


@pytest.mark.parametrize("phase", ["guest", "accounts"])
@pytest.mark.parametrize("control", [KeyboardInterrupt(), SystemExit(3)])
def test_preparation_control_retains_exact_facts(database, tmp_path, monkeypatch, phase, control):
    platform, route = _install(database, monkeypatch)

    def interrupted(*args, **kwargs):
        raise control

    monkeypatch.setattr(route.guest if phase == "guest" else route.accounts, "execute", interrupted)
    with (
        pytest.raises(type(control)) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("interrupted preparation admitted")
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    workflow = fact._workflow
    assert workflow.selected_locator == ProviderLocator("pve:generation")
    assert workflow.selected_binding is not None
    assert workflow.preparation_fact is not None
    if phase == "guest":
        assert workflow.preparation_fact.requires_owner_retention
    else:
        assert workflow.identity is not None and workflow.identity.requires_owner_retention
    assert database.operations.inspect(_scope()) is not None
    with pytest.raises(StateError):
        workflow.owner.borrow()
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert route.local.calls == 0


@pytest.mark.parametrize("phase", ["guest", "accounts"])
def test_unsettled_preparation_reply_retains_owner(database, tmp_path, monkeypatch, phase):
    platform, route = _install(database, monkeypatch)
    if phase == "accounts":
        route.accounts.completion = None
    else:
        original = route.guest.execute
        monkeypatch.setattr(
            route.guest, "execute", lambda *args, **kwargs: replace(original(*args, **kwargs), completion=None)
        )
    with (
        pytest.raises(StateError) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("uncertain account preparation admitted")
    assert database.operations.inspect(_scope()) is not None
    fact = caught.value.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    preparation = fact._workflow.identity if phase == "accounts" else fact._workflow.preparation_fact
    assert preparation is not None and preparation.pending_remote_effects
    assert route.local.calls == 0


def test_body_failure_and_closed_view_release_settled_owner(database, tmp_path, monkeypatch):
    platform, route = _install(database, monkeypatch)
    effect = tmp_path / "partial-effect"
    with (
        pytest.raises(CheckedExecutionError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=PurePosixPath(tmp_path)
        ) as views,
    ):
        views.execution.run(
            Command(
                [
                    "/usr/bin/python3",
                    "-c",
                    "import pathlib,sys;pathlib.Path(sys.argv[1]).touch();sys.exit(7)",
                    str(effect),
                ]
            ),
            profile=Protection.DIRECT,
            check=True,
        )
    assert effect.exists()
    assert database.operations.inspect(_scope()) is None
    with pytest.raises(StateError):
        views.execution.run(Command(["/usr/bin/true"]), profile=Protection.DIRECT)
    assert route.local.calls == 1


def test_claim_conflict_precedes_passive_observation(database, tmp_path, monkeypatch):
    platform, route = _install(database, monkeypatch)
    owner = OperationOwner.acquire(database.operations, _scope(), "other")
    monkeypatch.setattr(platform, "observe_execution_power", lambda *args, **kwargs: pytest.fail("observed conflict"))
    with (
        pytest.raises(StateError),
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("conflict admitted")
    assert route.resolutions == 0
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize("phase", ["guest", "accounts"])
def test_settlement_failure_retains_observed_preparation_facts(database, tmp_path, monkeypatch, phase):
    platform, route = _install(database, monkeypatch)
    original = OperationAttempt.settle
    control = KeyboardInterrupt()

    def interrupted(attempt):
        if phase == "guest" or route.accounts.calls:
            raise control
        original(attempt)

    monkeypatch.setattr(OperationAttempt, "settle", interrupted)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("unsettled observation admitted")
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    workflow = fact._workflow
    assert workflow.preparation_fact is not None and workflow.preparation_fact.guest_result is not None
    if phase == "accounts":
        assert workflow.identity is not None and workflow.identity.delivery_result is not None
        assert workflow.identity.coordination_uncertain
    else:
        assert workflow.preparation_fact.coordination_uncertain
    assert database.operations.inspect(_scope()) is not None


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_owner_release_retries_exact_cleanup(database, tmp_path, monkeypatch, committed):
    platform, route = _install(database, monkeypatch)
    original = OperationRepository.release_resolved
    releases = 0
    control = KeyboardInterrupt()

    def interrupted(repository, ownership):
        nonlocal releases
        releases += 1
        if releases == 1:
            if committed:
                original(repository, ownership)
            raise control
        original(repository, ownership)

    monkeypatch.setattr(OperationRepository, "release_resolved", interrupted)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ) as views,
    ):
        pass
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    assert (database.operations.inspect(_scope()) is None) is committed
    fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None
    assert releases == (1 if committed else 2)
    with pytest.raises(StateError):
        views.owner.borrow()
    assert route.local.calls == 0


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_locator_control_survives_borrow_release_failure(database, tmp_path, monkeypatch, committed, control_type):
    platform, route = _install(database, monkeypatch)
    control = control_type()
    original_resolution = OperationRepository.resolve_lifecycle_obligation
    observations = 0
    resolutions = 0

    def locator(*args, **kwargs):
        nonlocal observations
        observations += 1
        if observations == 3:
            raise control
        return ProviderLocator("pve:generation")

    def interrupted_release(repository, ownership, obligation_id):
        nonlocal resolutions
        resolutions += 1
        if committed:
            original_resolution(repository, ownership, obligation_id)
        raise StateError("injected obligation resolution failure")

    monkeypatch.setattr(platform, "observe_provider_locator", locator)
    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", interrupted_release)
    with (
        pytest.raises(control_type) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("uncertain preparation admitted body")
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    preparation = fact._workflow.preparation_fact
    assert preparation is not None and preparation.requires_owner_retention
    assert preparation.guest_result is not None and preparation.guest_result.observation is not None
    assert preparation.guest_result.observation.identity == route.local.observed
    claim = database.operations.inspect(_scope())
    assert claim is not None and claim.ownership == fact._workflow.owner.ownership
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert route.guest.calls == resolutions == 1
    assert not route.accounts.calls and route.local.calls == 0


def test_identity_borrow_release_failure_cannot_release_owner(database, tmp_path, monkeypatch):
    platform, route = _install(database, monkeypatch)
    original = release_borrow_after_custody
    control = KeyboardInterrupt()
    retained_borrow = None

    def interrupted(borrow):
        nonlocal retained_borrow
        retained_borrow = borrow
        raise control

    monkeypatch.setattr(_target_identity, "release_borrow_after_custody", interrupted)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(10), trusted_root=PurePosixPath(tmp_path)
        ),
    ):
        pytest.fail("unreleased identity borrow admitted")
    assert caught.value is control and retained_borrow is not None
    fact = control.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    assert fact._workflow.preparation_fact is not None
    assert fact._workflow.identity is not None and fact._workflow.identity.delivery_result is not None
    assert fact._workflow.identity.coordination_uncertain
    assert database.operations.inspect(_scope()) is not None and route.local.calls == 0
    with pytest.raises(StateError):
        fact._workflow.owner.borrow()
    original(retained_borrow)
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is not None


def test_private_operation_imports_without_retired_execution(tmp_path):
    code = """
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
roots = ('agentworks.transports', 'agentworks.ssh', 'agentworks.remote_exec',
         'agentworks.harness_setup.runner', 'agentworks.native_files',
         'agentworks.plugins.proxmox.transport')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == root or fullname.startswith(root + '.') for root in roots):
            raise AssertionError(fullname)
        return None
sys.meta_path.insert(0, Guard())
from contextlib import closing
from pathlib import Path, PurePosixPath
from agentworks.capabilities.base import RunContext
from agentworks.db import Database, VMStatus
from agentworks.errors import StateError
from agentworks.execution.carrier import Deadline
from agentworks.vms._native_operation import native_vm_operation
assert not any(name == root or name.startswith(root + '.') for name in sys.modules for root in roots)
sys.meta_path.pop(0)
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
platform = ProxmoxPlatform('pve', {'api_url':'https://pve.test', 'node':'node', 'token_id':'token'})
sys.meta_path.insert(0, Guard())
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if any(name == root or name.startswith(root + '.') for root in roots):
        raise AssertionError(name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
platform.observe_execution_power = lambda *args, **kwargs: VMStatus.UNKNOWN
with closing(Database(Path(sys.argv[2]) / 'state.db')) as db:
    db.insert_vm('box', 'pve', '101', admin_username='admin', instance_marker='a'*32)
    try:
        with native_vm_operation(db, 'box', platform, RunContext(),
             deadline=Deadline.after(10), trusted_root=PurePosixPath(sys.argv[2])):
            raise AssertionError('stopped operation admitted')
    except StateError:
        pass
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[2]), str(tmp_path)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
