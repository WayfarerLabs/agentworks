"""Multiple route records retain one actual claim and phased cleanup custody."""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import StateError
from agentworks.execution._execution_operation import ExecutionOperation, InlineExecutionControlFact
from agentworks.execution._execution_result import reduce_owned_inline_result
from agentworks.execution._file_gate_namespace import NamespacePreparation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._prepared_stage_closing import PreparedStageCleanup
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Carrier
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ExitCode
from agentworks.operations import OperationBorrow, OperationOwner
from agentworks.vms import _native_operation as native
from agentworks.vms._native_operation import VMOperation, _RoutePreparation, _Workflow
from agentworks.vms.target_preparation import VMTargetPreparationControlFact, VMTargetPreparationStatus
from tests.execution.files._file_stage_support import LocalCarrier
from tests.execution.files._file_upload_support import BytesSource
from tests.execution.files._target_support import target_for_owner
from tests.execution.files.test_upload_bookkeeping import bundles as bundles
from tests.execution.files.test_upload_bookkeeping import held as held
from tests.execution.files.test_upload_bookkeeping import upload
from tests.execution.test_prepared_stage_closing import TARGET, NativeDelivery, ScriptedCarrier, stage
from tests.execution.test_prepared_stage_closing import owned as owned
from tests.execution.test_proxmox_inline_closure import _run as run_proxmox
from tests.execution.test_proxmox_inline_closure import scenario as scenario
from tests.vms.test_native_operation import _install_route, _root, _RouteCarrier, _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux route composition")


def _record(owner, files=None, *, target=None):
    target = target or target_for_owner(owner)
    operation = ExecutionOperation(owner, target)
    return _RoutePreparation(
        views=VMOperation(owner, None, None, files or FileOperation(owner, target), operation, None)
    )


def _prepared(database, tmp_path, monkeypatch):
    platform = WSL2Platform("wsl2", {})
    carrier, host, observer = _install_route(database, platform, monkeypatch)
    owner = OperationOwner.acquire(database.operations, _scope(), "route-composition")
    workflow = _Workflow(owner, Deadline.after(30))
    native._prepare(
        workflow, database, "box", platform, RunContext(), _root(tmp_path), None, native.ExecutionRoute.NATIVE
    )
    return workflow, platform, carrier, host, observer


def _completed_inline_routes(scenario):
    _, owner, later_operation, _, plan, runtime, qga, _ = scenario
    earlier = _record(owner, target=later_operation._target)
    carrier = LocalCarrier()
    outcome = earlier.views.execution_operation.run_inline(
        carrier,
        Command(("/bin/true",)),
        plan=plan,
        runtime_selection=runtime,
        deadline=qga.deadline,
    )
    assert reduce_owned_inline_result(outcome).status == ExitCode(0)
    assert not outcome.requires_owner_retention and not earlier.views.execution_operation.active_inline_calls
    assert earlier.views.execution_operation._dispatch_obligation is not None
    later = _RoutePreparation(
        views=VMOperation(owner, None, None, FileOperation(owner, later_operation._target), later_operation, None)
    )
    return _Workflow(owner, qga.deadline, routes=[earlier, later]), carrier


def _next_route(workflow, database, platform, first, tmp_path, monkeypatch, *, agent=None):
    """Use a fresh helper transcript with the retained platform's exact binding.

    The simulated second route is explicitly selected at the common assembly
    boundary. These tests exercise custody composition, not SSH route policy.
    """
    carrier = _RouteCarrier(database)
    carrier.local.gate_root = first.local.gate_root
    carrier.accounts.identities["worker"] = IdentityExpectation(2401, 2402, (2402,))
    monkeypatch.setattr(
        WSL2Carrier,
        "execute",
        lambda selected, invocation, *, io, deadline, custody: carrier.execute(
            selected, invocation, io=io, deadline=deadline, custody=custody
        ),
    )

    def check(deadline):
        assert deadline is workflow.deadline and not deadline.expired
        claim = database.operations.inspect(_scope())
        assert claim is not None and claim.ownership == workflow.owner.ownership

    def forbidden(*args, **kwargs):
        pytest.fail("route assembly reacquired platform availability or ownership")

    monkeypatch.setattr(platform, "build_native_execution_access", forbidden)
    monkeypatch.setattr(workflow.access, "ensure_available", forbidden)
    monkeypatch.setattr(OperationOwner, "acquire", forbidden)
    return native._prepare_route(
        workflow,
        database,
        database.get_vm("box"),
        platform,
        workflow.access,
        ProviderLocator("wsl2:registration"),
        workflow.access.binding,
        trusted_root=_root(tmp_path),
        agent=agent,
        route=native.ExecutionRoute.CANONICAL,
        route_check=check,
    )


def test_later_route_completed_file_borrow_retires_before_earlier_execution_lifetime(held, monkeypatch):
    database, owner, files, root, _ = held
    earlier = _record(owner)
    earlier.views.execution_operation._dispatch_obligation = owner.register_lifecycle_obligation(
        "carrier-dispatch", payload_version=1, payload=b""
    )
    later = _record(owner, files)
    workflow = _Workflow(owner, Deadline.after(30), routes=[earlier, later])
    carrier = LocalCarrier()
    control = KeyboardInterrupt("completed upload borrow close")

    def interrupted(borrow):
        raise control

    with monkeypatch.context() as patch:
        patch.setattr(OperationBorrow, "close", interrupted)
        with pytest.raises(KeyboardInterrupt) as caught:
            upload(held, carrier, BytesSource(b"actual"))
    assert caught.value is control and files.active_uploads
    assert root.joinpath("target").read_bytes() == b"actual"
    requests = carrier.calls
    workflow.close()
    assert not files.active_uploads and carrier.calls == requests
    assert earlier.views.execution_operation._finished
    assert database.operations.inspect(owner.ownership.scope) is None


@pytest.mark.parametrize("committed", [False, True])
def test_completed_later_inline_handoff_retires_before_earlier_lifetime(scenario, monkeypatch, committed):
    database, owner, operation, _, _, _, qga, deliveries = scenario
    workflow, earlier_carrier = _completed_inline_routes(scenario)
    earlier = workflow.routes[0].views.execution_operation
    qga.running = False
    control = KeyboardInterrupt("later route handoff reply lost")
    cause = ValueError("original handoff cause")
    control.__cause__ = cause
    original = OperationBorrow.handoff_retained_effect

    def interrupt(borrow):
        if committed:
            original(borrow)
        raise control

    with monkeypatch.context() as patch:
        patch.setattr(OperationBorrow, "handoff_retained_effect", interrupt)
        with pytest.raises(KeyboardInterrupt) as caught:
            run_proxmox(scenario)
        assert caught.value is control and control.__cause__ is cause
        (active,) = operation.active_inline_calls
        assert active.bookkeeping_retained and deliveries[0].closure_proven
        assert active.prepared is None and active.candidate is None
        assert active.outcome is not None and active.outcome.candidate is None
        assert active.operation.outstanding_attempt is None and not active.borrow.has_outstanding_attempt
        assert active.borrow.closed is committed
        calls = list(qga.calls)
        earlier_calls = earlier_carrier.calls
        assert calls and database.operations.inspect(owner.ownership.scope) is not None
        if not committed:
            with pytest.raises(KeyboardInterrupt) as refused:
                workflow.close()
            assert refused.value is control and control.__cause__ is cause
            assert not earlier._finished and operation.active_inline_calls == (active,)
            assert database.operations.inspect(owner.ownership.scope) is not None
            assert qga.calls == calls and earlier_carrier.calls == earlier_calls
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert not operation.active_inline_calls and not earlier.active_inline_calls
    assert qga.calls == calls and earlier_carrier.calls == earlier_calls
    assert database.operations.inspect(owner.ownership.scope) is None
    assert caught.value is control and control.__cause__ is cause


def test_closing_later_unarmed_registration_retires_without_admission(scenario, monkeypatch):
    database, owner, operation, _, _, _, qga, _ = scenario
    workflow, earlier_carrier = _completed_inline_routes(scenario)
    control = KeyboardInterrupt("later registration reply lost")
    cause = ValueError("original registration cause")
    control.__cause__ = cause
    original = OperationBorrow.install_dispatch_obligation

    def register_then_interrupt(borrow, *args, **kwargs):
        original(borrow, *args, **kwargs)
        raise control

    with monkeypatch.context() as patch:
        patch.setattr(OperationBorrow, "install_dispatch_obligation", register_then_interrupt)
        with pytest.raises(KeyboardInterrupt) as caught:
            run_proxmox(scenario)
    assert caught.value is control and isinstance(control.__cause__, InlineExecutionControlFact)
    assert control.__cause__.__cause__ is cause
    (active,) = operation.active_inline_calls
    assert active.bookkeeping_retained and not active.installed and not active.armed
    assert active.operation.outstanding_attempt is None and not active.borrow.closed
    assert not qga.calls
    earlier_calls = earlier_carrier.calls

    def forbidden(borrow):
        pytest.fail("aggregate cleanup armed an unused registration")

    monkeypatch.setattr(OperationBorrow, "arm_dispatch_obligation", forbidden)
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert active.borrow.closed and not operation.active_inline_calls
    assert not qga.calls and earlier_carrier.calls == earlier_calls
    assert database.operations.inspect(owner.ownership.scope) is None
    assert caught.value is control and control.__cause__.__cause__ is cause


@pytest.mark.parametrize("phase", ["target", "namespace"])
def test_second_route_interruption_retains_both_records_and_exact_cause(database, tmp_path, monkeypatch, phase):
    workflow, platform, carrier, host, _ = _prepared(database, tmp_path, monkeypatch)
    first = workflow.routes[0]
    access = workflow.access
    ownership = workflow.owner.ownership
    agent = database.insert_agent("worker", "box", "worker") if phase == "namespace" else None
    control = KeyboardInterrupt(phase)
    original = native.prepare_managed_vm_target_from_platform
    partial = None

    def target(*args, **kwargs):
        nonlocal partial
        prepared = original(*args, **kwargs)
        partial = replace(
            prepared,
            status=VMTargetPreparationStatus.UNCERTAIN,
            requires_owner_retention=True,
            pending_remote_effects=True,
        )
        raise control from VMTargetPreparationControlFact(partial)

    if phase == "target":
        monkeypatch.setattr(native, "prepare_managed_vm_target_from_platform", target)
    else:
        first.views.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
        assert first.views.execution_operation._dispatch_obligation is not None
        original_settle = NamespacePreparation.settle_local

        def namespace(preparation, deadline):
            assert len(workflow.routes) == 2 and workflow.routes[1].namespace is preparation
            raise control

        monkeypatch.setattr(NamespacePreparation, "settle_local", namespace)
    with pytest.raises(KeyboardInterrupt) as caught:
        _next_route(workflow, database, platform, carrier, tmp_path, monkeypatch, agent=agent)
    assert caught.value is control
    assert len(workflow.routes) == 2 and workflow.routes[0] is first and first.views is not None
    second = workflow.routes[1]
    assert second.views is None and second.preparation is not None
    assert workflow.access is access and workflow.owner.ownership == ownership
    assert host.events.count("dispatch") == 1 and database.operations.inspect(_scope()).ownership == ownership
    if phase == "target":
        assert isinstance(control.__cause__, VMTargetPreparationControlFact)
        assert second.preparation is partial and control.__cause__.preparation is partial
        workflow.local_delivery.begin_process()
        assert not workflow.local_delivery.settled
        with pytest.raises(StateError):
            workflow.close()
        assert workflow.local_delivery.settled
    else:
        assert second.namespace is not None and not second.namespace.settled
        monkeypatch.setattr(NamespacePreparation, "settle_local", original_settle)
        workflow.close()
        assert second.namespace.settled and database.operations.inspect(_scope()) is None


def test_local_native_revocation_preserves_owner_and_outer_close_revokes_both_routes(database, tmp_path, monkeypatch):
    workflow, platform, carrier, host, observer = _prepared(database, tmp_path, monkeypatch)
    first = workflow.routes[0]
    native_views = first.views
    access = workflow.access
    first.body_admission_closed = True
    with pytest.raises(StateError):
        native_views.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    second_views = _next_route(workflow, database, platform, carrier, tmp_path, monkeypatch)
    assert len(workflow.routes) == 2 and workflow.access is access
    assert second_views.owner is native_views.owner is workflow.owner
    assert not workflow.owner._close_requested.is_set()
    assert not observer.events and host.events.count("dispatch") == 1
    control = KeyboardInterrupt("first cleanup phase")

    def fail(deadline):
        raise control

    with monkeypatch.context() as patch:
        patch.setattr(native_views.execution_operation, "settle_closing_scratch_helpers", fail)
        with pytest.raises(KeyboardInterrupt) as caught:
            workflow.close()
    assert caught.value is control
    assert database.operations.inspect(_scope()) is not None and not observer.events
    for view in (native_views, second_views):
        with pytest.raises(StateError):
            view.execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
        with pytest.raises(StateError):
            view.files.stat(_root(tmp_path) / "escaped")
    workflow.close(cleanup_deadline=Deadline.after(30))
    assert observer.events == ["observe"] and host.events.count("dispatch") == 1
    assert database.operations.inspect(_scope()) is None


def test_unfinished_native_file_call_prevents_new_route_preparation_without_retirement(database, tmp_path, monkeypatch):
    workflow, platform, carrier, _, _ = _prepared(database, tmp_path, monkeypatch)
    first = workflow.routes[0]
    call = first.views.file_operation.begin_local_download()
    retire = workflow.owner.retire_unused_borrow
    retirements = []

    def retirement():
        retirements.append(True)
        retire()

    monkeypatch.setattr(workflow.owner, "retire_unused_borrow", retirement)
    with pytest.raises(StateError):
        _next_route(workflow, database, platform, carrier, tmp_path, monkeypatch)
    assert len(workflow.routes) == 2 and workflow.routes[0] is first
    assert first.views.file_operation.has_unfinished_local_download_call
    assert not retirements and not workflow.owner._close_requested.is_set()
    assert database.operations.inspect(_scope()).ownership == workflow.owner.ownership
    first.views.file_operation.finish_local_download(call)
    workflow.close()
    assert retirements == [True] and database.operations.inspect(_scope()) is None


def test_later_retained_closing_helper_blocks_earlier_fresh_scratch_cleanup(owned):
    database, owner, _ = owned
    earlier, later = _record(owner, target=TARGET), _record(owner, target=TARGET)
    carriers = [ScriptedCarrier(owner), ScriptedCarrier(owner)]
    cleanups = []
    for record, carrier in zip((earlier, later), carriers, strict=True):
        prepared = stage((database, owner, carrier))
        assert prepared.begin()
        cleanup = PreparedStageCleanup(prepared)
        record.views.execution_operation._prepared_stages.append(cleanup)
        cleanups.append(cleanup)
    deliveries = []

    def delivery(expectation):
        value = NativeDelivery(carriers[1], expectation)
        deliveries.append(value)
        return value

    cleanups[1]._stage._new_helper_delivery = delivery
    owner.stop_admission()
    assert not cleanups[1].finish(Deadline.after(30))
    workflow = _Workflow(owner, Deadline.after(30), routes=[earlier, later])
    requests = len(carriers[0].requests)
    with pytest.raises(StateError):
        workflow.close()
    assert len(carriers[0].requests) == requests and len(deliveries) == 1
    assert database.operations.inspect(owner.ownership.scope) is not None
    deliveries[0].proven = True
    workflow.close(cleanup_deadline=Deadline.after(30))
    assert len(carriers[0].requests) == requests + 1
    assert all(cleanup._stage.cleaned for cleanup in cleanups)
    assert database.operations.inspect(owner.ownership.scope) is None
