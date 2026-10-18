"""Paired native composition with real claims and simulated guest/account facts.

These checks do not prove native credential transitions or root-owned scratch.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution._target_identity import (
    TargetIdentityControlFact,
    TargetIdentityPreparation,
    TargetIdentityStatus,
    prepare_target_identity,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.vms import _native_operation
from agentworks.vms._native_operation import VMOperationControlFact, native_vm_operation
from tests.vms.test_native_operation import _install_route, _root, _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux native composition")

_AGENT = IdentityExpectation(2401, 2402, (2402, 2403))


@pytest.mark.parametrize("root_delivery", [False, True])
def test_paired_views_share_operations_and_select_exact_ordinary_identity(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_delivery: bool
) -> None:
    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, _, _ = _install_route(database, platform, monkeypatch)
    route.accounts.identities["recorded-worker"] = _AGENT
    if root_delivery:
        route.accounts.identities["admin"] = IdentityExpectation(0, 0, (0,))
    original = prepare_target_identity
    preparations: list[tuple[dict, TargetIdentityPreparation]] = []

    def prepare(carrier, **kwargs):
        result = original(carrier, **kwargs)
        preparations.append(({"carrier": carrier, **kwargs}, result))
        return result

    monkeypatch.setattr(_native_operation, "prepare_target_identity", prepare)
    deadline = Deadline.after(30)
    root = _root(tmp_path)
    with native_vm_operation(
        database, "box", platform, RunContext(), deadline=deadline, trusted_root=root, agent_name="worker"
    ) as selected:
        assert len(preparations) == 2
        admin_arguments, admin_identity = preparations[0]
        agent_arguments, agent_identity = preparations[1]
        target = selected.agent_target
        assert target is not None
        files, execution = target.files(), target.execution()
        assert files is not None and execution is not None
        assert selected.target.files() is selected.files and selected.target.execution() is selected.execution
        assert files._operation is selected.files._operation is selected.file_operation
        assert execution._operation is selected.execution._operation is selected.execution_operation
        assert selected.file_operation._owner is selected.execution_operation._owner is selected.owner
        bootstrap = selected.file_operation._bootstrap
        assert bootstrap is selected.execution_operation._bootstrap is agent_arguments["bootstrap"]
        assert bootstrap is not None and bootstrap.guest is route.prepared_guest
        assert bootstrap.root_entry is admin_identity.elevated_plan
        assert "bootstrap" not in admin_arguments
        assert admin_arguments["include_elevated"] is True and agent_arguments["include_elevated"] is False
        assert agent_arguments["workload_account"] == "recorded-worker"
        assert agent_arguments["delivery_account"] == admin_arguments["delivery_account"] == "admin"
        for key in ("carrier", "runtime_selection", "deadline", "owner"):
            assert agent_arguments[key] is admin_arguments[key]
        assert agent_arguments["owner"] is selected.owner and agent_arguments["deadline"] is deadline
        for view in (files, execution, selected.files, selected.execution):
            assert view._carrier is agent_arguments["carrier"]
            assert view._runtime_selection is agent_arguments["runtime_selection"]
            assert view._deadline() is deadline
        assert files._trusted_root is selected.files._trusted_root is root
        assert files._ordinary_plan is execution._ordinary_plan is agent_identity.ordinary_plan
        assert selected.files._ordinary_plan is selected.execution._ordinary_plan is admin_identity.ordinary_plan
        assert execution._ordinary_plan.expected == _AGENT
        assert execution._ordinary_plan.mode is IdentityMode.DEMOTE
        assert execution._ordinary_plan is not selected.execution._ordinary_plan
        assert execution._elevated_plan is files._elevated_plan is agent_identity.elevated_plan is None
        assert target.features is selected.target.features is files._carrier.features

        class SelectedPlan(Exception):
            pass

        calls = []

        def selected_plan(operation, carrier, *args, **kwargs):
            calls.append((operation, carrier, kwargs["plan"], kwargs["deadline"], kwargs["runtime_selection"]))
            raise SelectedPlan

        with monkeypatch.context() as invocation_patch:
            invocation_patch.setattr(ExecutionOperation, "run_inline", selected_plan)
            invocation_patch.setattr(FileOperation, "stat", selected_plan)
            for file_view, execution_view, identity in (
                (selected.files, selected.execution, admin_identity),
                (files, execution, agent_identity),
            ):
                with pytest.raises(SelectedPlan):
                    execution_view.run(Command(["/bin/true"]), profile=Protection.DIRECT)
                with pytest.raises(SelectedPlan):
                    file_view.stat(root / "source")
                assert calls[-2][2] is calls[-1][2] is identity.ordinary_plan
            assert [call[0] for call in calls] == [
                selected.execution_operation,
                selected.file_operation,
                selected.execution_operation,
                selected.file_operation,
            ]
            assert all(call[3] is deadline and call[4] is agent_arguments["runtime_selection"] for call in calls)
        effects = len(route.routes)
        with pytest.raises(StateError):
            execution.run(Command(["/bin/true"]), profile=Protection.DIRECT, sudo=True)
        with pytest.raises(StateError):
            files.stat(root / "source", sudo=True)
        assert len(route.routes) == effects
    assert database.operations.inspect(_scope()) is None
    effects = len(route.routes)
    for file_view, execution_view in ((selected.files, selected.execution), (files, execution)):
        with pytest.raises(StateError):
            execution_view.run(Command(["/bin/true"]), profile=Protection.DIRECT)
        with pytest.raises(StateError):
            file_view.stat(root / "source")
    assert len(route.routes) == effects


@pytest.mark.parametrize("selection", ["missing", "other-vm", "invalid-account"])
def test_agent_selection_refuses_under_claim_before_provider_access(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    if selection == "other-vm":
        database.insert_vm("other", "wsl2", "other")
        database.insert_agent("worker", "other", "recorded-worker")
    elif selection == "invalid-account":
        database.insert_agent("worker", "box", "invalid\0account")
    original = database.get_agent
    selections = []

    def get_agent(name):
        assert database.operations.inspect(_scope()) is not None
        selections.append(name)
        return original(name)

    monkeypatch.setattr(database, "get_agent", get_agent)
    platform = WSL2Platform("wsl2", {})
    monkeypatch.setattr(
        platform,
        "build_native_execution_access",
        lambda *args, **kwargs: pytest.fail("provider access before validation"),
    )
    error = {"missing": NotFoundError, "other-vm": StateError, "invalid-account": ValidationError}[selection]
    with (
        pytest.raises(error),
        native_vm_operation(
            database,
            "box",
            platform,
            RunContext(),
            deadline=Deadline.after(30),
            trusted_root=_root(tmp_path),
            agent_name="worker",
        ),
    ):
        pytest.fail("invalid agent admitted body")
    assert selections == ["worker"]
    assert database.operations.inspect(_scope()) is None


def test_unselected_agent_keeps_admin_only_composition(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, _, _ = _install_route(database, platform, monkeypatch)
    monkeypatch.setattr(database, "get_agent", lambda name: pytest.fail("unrequested agent lookup"))
    with native_vm_operation(
        database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
    ) as selected:
        assert selected.agent_target is None
        assert selected.target.files() is selected.files and selected.target.execution() is selected.execution
    assert route.accounts.calls == ["admin", "root"]
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("failure", ["missing", "malformed", "uncertain", "deadline"])
def test_agent_preparation_failure_never_admits_partial_pair(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, _, _ = _install_route(database, platform, monkeypatch)
    route.accounts.identities["recorded-worker"] = _AGENT
    if failure == "missing":
        route.accounts.missing.add("recorded-worker")
    elif failure == "malformed":
        route.accounts.malformed.add("recorded-worker")
    elif failure == "uncertain":
        route.accounts.uncertain_on_account = "recorded-worker"
    else:
        route.accounts.expire_on_account = "recorded-worker"
    with (
        pytest.raises(StateError) as caught,
        native_vm_operation(
            database,
            "box",
            platform,
            RunContext(),
            deadline=Deadline.after(30),
            trusted_root=_root(tmp_path),
            agent_name="worker",
        ),
    ):
        pytest.fail("failed requested pair admitted body")
    assert route.local.calls == 0
    assert route.accounts.calls == ["admin", "root", "admin", "recorded-worker"]
    if failure in {"uncertain", "deadline"}:
        fact = caught.value.__cause__
        assert isinstance(fact, VMOperationControlFact)
        workflow = fact._workflow
        assert workflow.views is None
        assert workflow.identity is not None and workflow.identity.status is TargetIdentityStatus.PREPARED
        assert workflow.agent_identity is not None and workflow.agent_identity.workload_result is not None
        assert workflow.agent_identity.status is not TargetIdentityStatus.PREPARED
        assert database.operations.inspect(_scope()) is not None
        if failure == "uncertain":
            assert not workflow._components_settled()
            with pytest.raises(StateError):
                fact.retry_cleanup(Deadline.after(10))
            assert database.operations.inspect(_scope()) is not None
    else:
        assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("flag", ["pending_remote_effects", "coordination_uncertain", "requires_owner_retention"])
def test_each_agent_custody_fact_keeps_aggregate_claim(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, _, _ = _install_route(database, platform, monkeypatch)
    route.accounts.identities["recorded-worker"] = _AGENT
    original = prepare_target_identity
    observed = []

    def prepare(carrier, **kwargs):
        result = original(carrier, **kwargs)
        observed.append(result)
        if kwargs["workload_account"] == "recorded-worker":
            return replace(result, status=TargetIdentityStatus.UNCERTAIN, **{flag: True})
        return result

    monkeypatch.setattr(_native_operation, "prepare_target_identity", prepare)
    with (
        pytest.raises(StateError) as caught,
        native_vm_operation(
            database,
            "box",
            platform,
            RunContext(),
            deadline=Deadline.after(30),
            trusted_root=_root(tmp_path),
            agent_name="worker",
        ),
    ):
        pytest.fail("unsettled agent preparation admitted body")
    fact = caught.value.__cause__
    assert isinstance(fact, VMOperationControlFact)
    workflow = fact._workflow
    assert workflow.identity is observed[0] and workflow.views is None
    assert workflow.agent_identity is not None
    assert workflow.agent_identity.delivery_result is observed[1].delivery_result
    assert workflow.agent_identity.workload_result is observed[1].workload_result
    assert not workflow._components_settled()
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is not None
    workflow.agent_identity = observed[1]
    fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None


def test_interrupted_agent_lookup_preserves_control_partial_facts_and_claim(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, _, _ = _install_route(database, platform, monkeypatch)
    route.accounts.identities["recorded-worker"] = _AGENT
    original = route.accounts.execute
    primary = KeyboardInterrupt()
    underlying = ValueError("fixture control cause")

    def interrupt(invocation, *, io, deadline, custody=None):
        result = original(invocation, io=io, deadline=deadline, custody=custody)
        if json.loads(io.input.data)["account"] == "recorded-worker":
            raise primary from underlying
        return result

    monkeypatch.setattr(route.accounts, "execute", interrupt)
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database,
            "box",
            platform,
            RunContext(),
            deadline=Deadline.after(30),
            trusted_root=_root(tmp_path),
            agent_name="worker",
        ),
    ):
        pytest.fail("interrupted pair admitted body")
    assert caught.value is primary
    aggregate = primary.__cause__
    assert isinstance(aggregate, VMOperationControlFact)
    account_fact = aggregate.__cause__
    assert isinstance(account_fact, TargetIdentityControlFact)
    assert account_fact.__cause__ is underlying
    workflow = aggregate._workflow
    assert workflow.identity is not None and workflow.identity.status is TargetIdentityStatus.PREPARED
    assert workflow.agent_identity is account_fact.preparation
    assert workflow.agent_identity.delivery_result is not None and workflow.agent_identity.workload_result is None
    assert workflow.agent_identity.status is TargetIdentityStatus.UNCERTAIN
    assert workflow.views is None and not workflow._components_settled()
    assert database.operations.inspect(_scope()) is not None
    assert route.local.calls == 0
