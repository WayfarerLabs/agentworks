"""Explicit VM preparation retains namespace custody before exposing either view."""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import StateError
from agentworks.execution import _file_gate_namespace as namespace
from agentworks.execution._file_gate_namespace import NamespacePreparation
from agentworks.execution._file_gate_namespace_protocol import NamespaceStatus
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution.carrier import Deadline
from agentworks.execution.target import ExecutionRoute
from agentworks.vms import _native_operation
from agentworks.vms._native_operation import VMOperationControlFact, _vm_operation
from tests.vms.test_native_operation import _install_route, _root, _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux VM composition")


def test_namespace_prepared_after_numeric_agent_and_before_file_operations(database, tmp_path, monkeypatch):
    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, _, _ = _install_route(database, platform, monkeypatch)
    route.accounts.identities["recorded-worker"] = IdentityExpectation(2401, 2402, (2402,))
    original = NamespacePreparation.prepare
    selected = []

    def prepare(preparation):
        assert route.accounts.calls == ["admin", "root", "admin", "recorded-worker"]
        assert database.operations.inspect(_scope()) is not None
        assert not preparation.prepared and not preparation.settled
        original(preparation)
        assert preparation.prepared and preparation.settled
        selected.append(preparation)

    original_files = vars(_native_operation)["FileOperation"]

    def files(owner, target, **kwargs):
        assert len(selected) == 1 and selected[0]._owner is owner
        assert selected[0]._record.bootstrap is kwargs["bootstrap"]
        return original_files(owner, target, **kwargs)

    monkeypatch.setattr(NamespacePreparation, "prepare", prepare)
    monkeypatch.setattr(_native_operation, "FileOperation", files)
    with _vm_operation(
        database,
        "box",
        platform,
        RunContext(),
        route=ExecutionRoute.NATIVE,
        deadline=Deadline.after(10),
        trusted_root=_root(tmp_path),
        agent_name="worker",
    ) as operation:
        assert operation.agent_target is not None
        assert len(selected) == 1
        assert all(
            row.obligation_kind != "file-gate-namespace" for row in operation.owner.list_pending_lifecycle_obligations()
        )
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("case", ["lost-reply", "partial", "control", "refused"])
def test_failed_namespace_admits_no_views_and_retains_exact_workflow_as_needed(database, tmp_path, monkeypatch, case):
    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, native, _ = _install_route(database, platform, monkeypatch)
    route.accounts.identities["recorded-worker"] = IdentityExpectation(2401, 2402, (2402,))
    original = vars(namespace)["exchange_namespace"]
    control = KeyboardInterrupt()

    def effect(*args, **kwargs):
        result = original(*args, **kwargs)
        if case == "control":
            raise control
        return replace(
            result,
            status={"lost-reply": None, "partial": NamespaceStatus.UNCERTAIN, "refused": NamespaceStatus.REFUSED}[case],
        )

    monkeypatch.setattr(namespace, "exchange_namespace", effect)
    monkeypatch.setattr(_native_operation, "FileOperation", lambda *args, **kwargs: pytest.fail("unprepared views"))
    with (
        pytest.raises(KeyboardInterrupt if case == "control" else StateError) as caught,
        _vm_operation(
            database,
            "box",
            platform,
            RunContext(),
            route=ExecutionRoute.NATIVE,
            deadline=Deadline.after(10),
            trusted_root=_root(tmp_path),
            agent_name="worker",
        ),
    ):
        pytest.fail("namespace failure admitted body")
    if case == "refused":
        assert database.operations.inspect(_scope()) is None
        return
    assert isinstance(caught.value.__cause__, VMOperationControlFact)
    workflow = caught.value.__cause__._workflow
    assert workflow.views is None and workflow.namespace is not None
    preparation = workflow.namespace
    row = next(
        row
        for row in workflow.owner.list_pending_lifecycle_obligations()
        if row.obligation_kind == "file-gate-namespace"
    )
    assert row.obligation_kind == "file-gate-namespace"
    assert preparation._borrow is not None and not preparation.settled
    with pytest.raises(StateError):
        caught.value.__cause__.retry_cleanup(Deadline.after(10))
    assert row in workflow.owner.list_pending_lifecycle_obligations()
    assert database.operations.inspect(_scope()) is not None
    assert native.events.count("close") == 0


@pytest.mark.parametrize("phase", ["before-prepare", "borrow-reply"])
@pytest.mark.parametrize("retry", [False, True])
def test_no_effect_namespace_control_retires_only_fenced_unused_work(database, tmp_path, monkeypatch, phase, retry):
    database.insert_agent("worker", "box", "recorded-worker")
    platform = WSL2Platform("wsl2", {})
    route, _, observer = _install_route(database, platform, monkeypatch)
    route.accounts.identities["recorded-worker"] = IdentityExpectation(2401, 2402, (2402,))
    original = NamespacePreparation.prepare
    control = KeyboardInterrupt("namespace preparation control")
    orphans = []
    selected = []

    def prepare(preparation):
        selected.append(preparation)
        if phase == "before-prepare":
            raise control
        original_borrow = preparation._owner.borrow

        def borrow():
            orphans.append(original_borrow())
            raise control

        monkeypatch.setattr(preparation._owner, "borrow", borrow)
        original(preparation)

    monkeypatch.setattr(NamespacePreparation, "prepare", prepare)
    if retry:
        observer.presence = GuestAnchorPresence.UNKNOWN
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        _vm_operation(
            database,
            "box",
            platform,
            RunContext(),
            route=ExecutionRoute.NATIVE,
            deadline=Deadline.after(10),
            trusted_root=_root(tmp_path),
            agent_name="worker",
        ),
    ):
        pytest.fail("interrupted namespace admitted views")
    assert caught.value is control and len(selected) == 1
    assert all(borrow.closed for borrow in orphans)
    if retry:
        assert isinstance(control.__cause__, VMOperationControlFact)
        assert database.operations.inspect(_scope()) is not None
        observer.presence = GuestAnchorPresence.ABSENT_CONFIRMED
        control.__cause__.retry_cleanup(Deadline.after(10))
    assert selected[0].settled and not selected[0].prepared
    assert database.operations.inspect(_scope()) is None
