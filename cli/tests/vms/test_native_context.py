"""Native context composition with real claims and simulated guest/account facts.

Packed command and file helpers run intact. These fixtures do not prove native
credential transitions or root-owned guest scratch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel, TargetAbsence
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, OperationClaimState
from agentworks.errors import StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import Create, NewMetadata
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionIdentity, ExecutionRoute
from agentworks.operations import OperationOwner
from agentworks.orchestration.secrets import ScopedSecrets
from agentworks.vms import _native_operation
from agentworks.vms._native_operation import VMOperation, VMOperationControlFact, vm_execution_context
from tests.test_run_context import _UntouchableTarget
from tests.vms.canonical_support import ADDRESS, canonical_config, install_canonical_delivery
from tests.vms.test_native_operation import _install_route, _root, _scope
from tests.vms.test_native_operation import database as database

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.execution.target import ExecutionTarget
    from agentworks.transports import Transport

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux native context composition")


@pytest.fixture
def prepared(monkeypatch: pytest.MonkeyPatch) -> list[VMOperation]:
    original = _native_operation._prepare
    operations: list[VMOperation] = []

    def prepare(*args, **kwargs):
        operation = original(*args, **kwargs)
        operations.append(operation)
        return operation

    monkeypatch.setattr(_native_operation, "_prepare", prepare)
    return operations


@pytest.mark.parametrize("agent_name", [None, "worker"])
@pytest.mark.parametrize("execution_route", [ExecutionRoute.NATIVE, ExecutionRoute.CANONICAL])
def test_context_delivers_exact_prepared_views_and_preserves_input_world(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[VMOperation],
    agent_name: str | None,
    execution_route: ExecutionRoute,
) -> None:
    import grp
    import os
    import pwd

    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    route.accounts.identities["recorded-worker"] = IdentityExpectation(2401, 2402, (2402, 2403))
    original_build = platform.build_native_execution_access
    original_power = platform.observe_execution_power
    original_start = native.spawn_owned
    owners: list[OperationOwner] = []
    events: list[str] = []

    def claim_matches() -> None:
        claim = database.operations.inspect(_scope())
        assert claim is not None and claim.ownership == owners[0].ownership

    def build(vm, ctx, *, owner, custody):
        owners.append(owner)
        claim_matches()
        assert ctx is incoming
        events.append("build")
        return original_build(vm, ctx, owner=owner, custody=custody)

    def power(vm, ctx, *, deadline, custody=None):
        claim_matches()
        events.append("power")
        return original_power(vm, ctx, deadline=deadline, custody=custody)

    def start(argv, deadline):
        claim_matches()
        events.append("start")
        return original_start(argv, deadline)

    monkeypatch.setattr(platform, "build_native_execution_access", build)
    monkeypatch.setattr(platform, "observe_execution_power", power)
    monkeypatch.setattr(native, "spawn_owned", start)
    if execution_route is ExecutionRoute.CANONICAL:
        database.update_vm_tailscale("box", ADDRESS)
        config = canonical_config(tmp_path)
        install_canonical_delivery(monkeypatch, route)
    else:
        config = cast("Config", _UntouchableTarget())
    legacy_admin = cast("Transport", _UntouchableTarget())
    legacy_agent = cast("Transport", _UntouchableTarget())
    stale_target = cast("ExecutionTarget", _UntouchableTarget())
    scope = OperationScope(ScopeLevel.SESSION, vm="box", workspace="work", agent="descriptive", session="s")
    values = {"allowed": "one", "other": "hidden"}
    secrets = ScopedSecrets(values, ("allowed",))
    incoming = RunContext(
        config=config,
        operation_scope=scope,
        secrets=secrets,
        admin_target=legacy_admin,
        agent_target=legacy_agent,
        admin_execution_target=stale_target,
        agent_execution_target=stale_target,
    )
    root = _root(tmp_path)
    Path(root).mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    route.local.scratch = scratch
    deadline = Deadline.after(30)
    metadata = NewMetadata(pwd.getpwuid(os.geteuid()).pw_name, grp.getgrgid(os.getegid()).gr_name, 0o600)
    with vm_execution_context(
        database,
        "box",
        platform,
        incoming,
        deadline=deadline,
        route=execution_route,
        trusted_root=root,
        agent_name=agent_name,
    ) as ctx:
        assert type(ctx) is RunContext and ctx is not incoming
        assert events[:3] == ["build", "power", "start"] and set(events[3:]) <= {"power"}
        assert len(prepared) == 1 and prepared[0].owner is owners[0]
        operation = prepared[0]
        assert route.prepared_guest is not None and route.guest.calls == 1
        assert route.accounts.calls == ["admin", "root"] + (["admin", "recorded-worker"] if agent_name else [])
        claim_matches()
        claim = database.operations.inspect(_scope())
        assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
        before = (len(route.routes), list(native.events), list(observer.events), list(route.accounts.calls))
        obligations = operation.owner.list_pending_lifecycle_obligations()
        for _ in range(2):
            assert ctx.config is config and ctx.operation_scope is scope
            assert ctx.admin_target() is legacy_admin and ctx.agent_target() is legacy_agent
            assert ctx.admin_execution_target() is operation.target
            assert ctx.agent_execution_target() is operation.agent_target
            assert ctx.admin_execution_target_absence() is None
            assert ctx.agent_execution_target_absence() is (TargetAbsence.UNAVAILABLE if agent_name is None else None)
            assert ctx.secret("allowed") == "one"
            with pytest.raises(StateError):
                ctx.secret("other")
        assert before == (len(route.routes), native.events, observer.events, route.accounts.calls)
        assert operation.owner.list_pending_lifecycle_obligations() == obligations
        assert ctx._secrets is secrets
        values["allowed"] = "two"
        assert ctx.secret("allowed") == "two"
        assert incoming.admin_execution_target() is incoming.agent_execution_target() is stale_target
        target = ctx.admin_execution_target()
        assert target is not None
        expected = route.accounts.identities["admin"]
        assert target.identity == ExecutionIdentity("admin", expected.euid, expected.egid, expected.groups)
        assert target.route is execution_route
        files, execution = target.files(), target.execution()
        assert files is not None and execution is not None
        code = (
            "import json,os; print(json.dumps([os.geteuid(),os.getegid(),sorted(set(os.getgroups())|{os.getegid()})]))"
        )
        result = execution.run(Command(["/usr/bin/python3", "-I", "-S", "-B", "-c", code]), profile=Protection.DIRECT)
        identity = operation.execution._ordinary_plan.expected
        assert result.ok and json.loads(result.stdout.data) == [identity.euid, identity.egid, list(identity.groups)]
        destination = root / "created"
        files.write_file(destination, b"context-file", condition=Create(), create_metadata=metadata)
        read = files.read_file(destination, max_bytes=64)
        assert read is not None and read.data == b"context-file"
        assert all(selected is deadline for selected in route.local_deadlines)
        agent = ctx.agent_execution_target()
        if agent_name is None:
            assert agent is None
        else:
            assert agent is not None
            assert agent.identity == ExecutionIdentity("recorded-worker", 2401, 2402, (2402, 2403))
            assert agent.route is execution_route
            agent_files, agent_execution = agent.files(), agent.execution()
            assert agent_files is not None and agent_execution is not None
            plans = []

            class SelectedPlan(Exception):
                pass

            def selected_plan(operation, carrier, *args, **kwargs):
                plans.append(kwargs["plan"])
                raise SelectedPlan

            with monkeypatch.context() as selection:
                selection.setattr(ExecutionOperation, "run_inline", selected_plan)
                selection.setattr(FileOperation, "stat", selected_plan)
                with pytest.raises(SelectedPlan):
                    agent_execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
                with pytest.raises(SelectedPlan):
                    agent_files.stat(destination)
            assert plans[0] is plans[1] and plans[0].expected == route.accounts.identities["recorded-worker"]
            assert plans[0] is not operation.execution._ordinary_plan
            effects = len(route.routes)
            with pytest.raises(StateError):
                agent_execution.run(Command(["/bin/true"]), profile=Protection.DIRECT, sudo=True)
            with pytest.raises(StateError):
                agent_files.stat(destination, sudo=True)
            assert len(route.routes) == effects
    assert database.operations.inspect(_scope()) is None
    effects = len(route.routes)
    for target in (ctx.admin_execution_target(), ctx.agent_execution_target()):
        if target is None:
            continue
        reused = RunContext(admin_execution_target=target)
        assert reused.admin_execution_target() is target
        assert reused.admin_execution_target_absence() is None
        assert target.route is execution_route
        assert target.identity.account == ("admin" if target is operation.target else "recorded-worker")
        files, execution = target.files(), target.execution()
        assert files is not None and execution is not None
        with pytest.raises(StateError):
            execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
        with pytest.raises(StateError):
            files.write_file(root / "after-close", b"refused", condition=Create(), create_metadata=metadata)
    assert len(route.routes) == effects and not Path(root / "after-close").exists()
    assert not list(scratch.iterdir())


@pytest.mark.parametrize(
    "scope",
    [
        None,
        OperationScope(ScopeLevel.SYSTEM),
        OperationScope(ScopeLevel.VM, vm="box"),
        OperationScope(ScopeLevel.WORKSPACE, vm="box", workspace="work"),
        OperationScope(ScopeLevel.AGENT, vm="box", agent="unselected"),
        OperationScope(ScopeLevel.SESSION, vm="box", workspace="work", agent="unselected", session="s"),
        OperationScope(ScopeLevel.SESSION, vm="box", workspace="work", admin=True, session="s"),
    ],
)
@pytest.mark.parametrize("absence", list(TargetAbsence))
def test_descriptive_scope_never_selects_an_agent(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: OperationScope | None,
    absence: TargetAbsence,
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, _, _ = _install_route(database, platform, monkeypatch)
    monkeypatch.setattr(database, "get_agent", lambda name: pytest.fail("unrequested agent lookup"))
    with vm_execution_context(
        database,
        "box",
        platform,
        RunContext(
            operation_scope=scope,
            admin_execution_target_absence=TargetAbsence.WITHHELD,
            agent_execution_target_absence=absence,
        ),
        deadline=Deadline.after(30),
        route=ExecutionRoute.NATIVE,
        trusted_root=_root(tmp_path),
    ) as ctx:
        assert ctx.operation_scope is scope and ctx.agent_execution_target() is None
        assert ctx.admin_execution_target() is not None
        assert ctx.admin_execution_target_absence() is None
        assert ctx.agent_execution_target_absence() is absence
        assert ctx.admin_target() is ctx.agent_target() is None
    assert route.accounts.calls == ["admin", "root"]
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("refusal", ["scope", "claim"])
def test_refusal_precedes_provider_build(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    platform = WSL2Platform("wsl2", {})
    monkeypatch.setattr(
        platform, "build_native_execution_access", lambda *args, **kwargs: pytest.fail("provider build")
    )
    owner = OperationOwner.acquire(database.operations, _scope(), "other") if refusal == "claim" else None
    scope = OperationScope(ScopeLevel.AGENT, vm="other", agent="worker") if refusal == "scope" else None
    with (
        pytest.raises(ValidationError if refusal == "scope" else StateError),
        vm_execution_context(
            database,
            "box",
            platform,
            RunContext(operation_scope=scope),
            deadline=Deadline.after(30),
            route=ExecutionRoute.NATIVE,
            trusted_root=_root(tmp_path),
        ),
    ):
        pytest.fail("refused context admitted body")
    if owner is not None:
        claim = database.operations.inspect(_scope())
        assert claim is not None and claim.ownership == owner.ownership
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("stage", ["construction", "body"])
@pytest.mark.parametrize("control_type", [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("execution_route", [ExecutionRoute.NATIVE, ExecutionRoute.CANONICAL])
def test_context_failure_unwinds_original_span(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[VMOperation],
    stage: str,
    control_type: type[BaseException],
    execution_route: ExecutionRoute,
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    if execution_route is ExecutionRoute.CANONICAL:
        database.update_vm_tailscale("box", ADDRESS)
        install_canonical_delivery(monkeypatch, route)
        incoming = RunContext(config=canonical_config(tmp_path))
    else:
        incoming = RunContext()
    control, cause = control_type(), ValueError("fixture cause")

    def fail(*args: object, **kwargs: object) -> None:
        assert len(prepared) == 1
        raise control from cause

    with monkeypatch.context() as construction:
        if stage == "construction":
            construction.setattr(RunContext, "__init__", fail)
        with (
            pytest.raises(control_type) as caught,
            vm_execution_context(
                database,
                "box",
                platform,
                incoming,
                deadline=Deadline.after(30),
                route=execution_route,
                trusted_root=_root(tmp_path),
            ),
        ):
            assert stage == "body"
            fail()
    assert caught.value is control and control.__cause__ is cause
    assert len(prepared) == 1 and native.events.count("dispatch") == 1 and observer.events == ["observe"]
    assert database.operations.inspect(_scope()) is None
    with pytest.raises(StateError):
        prepared[0].owner.borrow()


@pytest.mark.parametrize("stage", ["construction", "body"])
@pytest.mark.parametrize("unsettled", ["local", "remote"])
@pytest.mark.parametrize("execution_route", [ExecutionRoute.NATIVE, ExecutionRoute.CANONICAL])
def test_uncertain_cleanup_retains_original_control_cause_and_owner(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepared: list[VMOperation],
    stage: str,
    unsettled: str,
    execution_route: ExecutionRoute,
) -> None:
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    if execution_route is ExecutionRoute.CANONICAL:
        database.update_vm_tailscale("box", ADDRESS)
        install_canonical_delivery(monkeypatch, route)
        incoming = RunContext(config=canonical_config(tmp_path))
    else:
        incoming = RunContext()
    original = _native_operation._prepare
    local_calls = []
    control, cause = KeyboardInterrupt(), ValueError("fixture cause")

    def prepare(*args, **kwargs):
        operation = original(*args, **kwargs)
        if unsettled == "local":
            local_calls.append(operation.file_operation.begin_local_download())
        else:
            observer.presence = GuestAnchorPresence.UNKNOWN
        return operation

    def fail(*args: object, **kwargs: object) -> None:
        raise control from cause

    monkeypatch.setattr(_native_operation, "_prepare", prepare)
    with monkeypatch.context() as construction:
        if stage == "construction":
            construction.setattr(RunContext, "__init__", fail)
        with (
            pytest.raises(KeyboardInterrupt) as caught,
            vm_execution_context(
                database,
                "box",
                platform,
                incoming,
                deadline=Deadline.after(30),
                route=execution_route,
                trusted_root=_root(tmp_path),
            ),
        ):
            assert stage == "body"
            fail()
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, VMOperationControlFact) and fact.__cause__ is cause
    operation = prepared[0]
    assert fact._workflow.views is operation and fact._workflow.owner is operation.owner
    claim = database.operations.inspect(_scope())
    assert claim is not None and claim.ownership == operation.owner.ownership
    with pytest.raises(StateError):
        operation.owner.borrow()
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is not None
    if unsettled == "local":
        operation.file_operation.finish_local_download(local_calls[0])
    else:
        observer.presence = GuestAnchorPresence.ABSENT_CONFIRMED
    fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None and native.events.count("dispatch") == 1
