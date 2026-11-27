"""Owning workspace writers exclude competing native mutations through their bodies."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents import grants
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms.manager import gated_vm_boundary
from agentworks.workspaces import manager
from agentworks.workspaces.guest import ensure_group
from tests.vm_access_support import Call, install_access_recorder
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import reachable, seed
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity
from tests.workspaces.test_copy_orchestrated import CopyGuest, _second_vm

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.execution.results import Result

pytestmark = [pytest.mark.windows, pytest.mark.usefixtures("stub_ssh_identity")]


def _run(operation: str, db: Database, config: Any, vm_name: str) -> None:
    if operation == "repair":
        manager.repair_workspace(db, config, "ws1", interaction=TtyInteractionPolicy.REFUSE)
    elif operation == "grant":
        grants.grant_workspaces(
            db, config, agent_name="dev", workspace_names=[], grant_all=True, interaction=TtyInteractionPolicy.REFUSE
        )
    else:
        manager.copy_workspace(
            db, config, "ws1", dest_name="ws2", vm_name=vm_name, interaction=TtyInteractionPolicy.REFUSE
        )


@pytest.mark.parametrize("operation,vm_name", [("repair", "box"), ("grant", "box"), ("copy", "box"), ("copy", "box2")])
def test_group_writes_and_copy_grants_keep_the_destination_guard(
    db: Database, make_config: Any, monkeypatch: pytest.MonkeyPatch, operation: str, vm_name: str
) -> None:
    config = make_config()
    seed(db)
    if vm_name == "box2":
        _second_vm(db)
    db.insert_agent("dev", vm_name, "agt-dev", template="default", grant_all=operation == "copy")
    reachable(monkeypatch, True)
    guest = CopyGuest([])
    recorder = install_access_recorder(monkeypatch)
    guarded_writes: list[object] = []
    group = "ws-ws2" if operation == "copy" else "ws-ws1"

    def answer(call: Call) -> Result:
        if call.request == ensure_group(group):
            # This is the native writer itself, after activation admitted the
            # operation. A competing operation must still fail at this point.
            with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, vm_name):
                pass
            # A cross-VM copy holds its source VM's guard across the unpack too.
            with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, "box"):
                pass
            guarded_writes.append(call.request)
        if operation == "repair" and getattr(call.request, "argv", ()) == ("getent", "group", group):
            from agentworks.execution.results import Completed, Result

            return Result(Completed(2))
        return guest.answer(call)

    recorder.answer = answer
    _run(operation, db, config, vm_name)

    # Copy creates its group and materializes grant-all membership under the
    # same body guard. A nested acquisition would instead abort the copy.
    assert len(guarded_writes) == (2 if operation == "copy" else 1)
    if operation == "copy":
        assert db.get_workspace("ws2") is not None
        assert db.list_agent_grants("dev")
    with native_mutation_guard(db.path, vm_name):
        pass


@pytest.mark.parametrize("operation", ["repair", "grant"])
def test_contended_guard_refuses_before_guest_or_grant_state_changes(
    db: Database, make_config: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    config = make_config()
    seed(db)
    db.insert_agent("dev", "box", "agt-dev", template="default")
    reachable(monkeypatch, True)
    recorder = install_access_recorder(monkeypatch)

    with native_mutation_guard(db.path, "box"), pytest.raises(NativeSetupBusyError):
        _run(operation, db, config, "box")

    assert recorder.calls == []
    agent = db.get_agent("dev")
    assert agent is not None and not agent.grant_all
    assert db.list_agent_grants("dev") == []


@pytest.mark.parametrize(
    ("vm_name", "contended"),
    [("box", "box"), ("box2", "box2"), ("box2", "box")],
    ids=["same-vm", "destination", "source"],
)
def test_copy_contention_after_admission_refuses_before_the_pack(
    db: Database, make_config: Any, monkeypatch: pytest.MonkeyPatch, vm_name: str, contended: str
) -> None:
    config = make_config()
    seed(db)
    if vm_name == "box2":
        _second_vm(db)
    reachable(monkeypatch, True)
    guest = CopyGuest([])
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = guest.answer
    boundary = gated_vm_boundary

    @contextlib.contextmanager
    def competing_after_admission(*args: Any, **kwargs: Any):  # noqa: ANN202
        with boundary(*args, **kwargs) as admitted:
            if args[3].name == contended:
                with native_mutation_guard(db.path, contended):
                    yield admitted
            else:
                yield admitted

    monkeypatch.setattr("agentworks.workspaces.manager.copy.gated_vm_boundary", competing_after_admission)
    with pytest.raises(NativeSetupBusyError):
        _run("copy", db, config, vm_name)

    assert db.get_workspace("ws2") is None
    assert guest.requests == []


@pytest.mark.parametrize("revoke_all", [False, True], ids=["named", "all"])
def test_revoke_contention_after_admission_refuses_before_guest_or_grant_state_changes(
    db: Database, make_config: Any, monkeypatch: pytest.MonkeyPatch, revoke_all: bool
) -> None:
    config = make_config()
    seed(db)
    db.insert_agent("dev", "box", "agt-dev", template="default", grant_all=revoke_all)
    db.insert_agent_grant("dev", "ws1", "explicit")
    reachable(monkeypatch, True)
    recorder = install_access_recorder(monkeypatch)
    boundary = gated_vm_boundary

    @contextlib.contextmanager
    def competing_after_admission(*args: Any, **kwargs: Any):  # noqa: ANN202
        with boundary(*args, **kwargs) as admitted, native_mutation_guard(db.path, "box"):
            yield admitted

    monkeypatch.setattr("agentworks.agents.grants.gated_vm_boundary", competing_after_admission)
    with pytest.raises(NativeSetupBusyError):
        grants.revoke_workspaces(
            db,
            config,
            agent_name="dev",
            workspace_names=[] if revoke_all else ["ws1"],
            revoke_all=revoke_all,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert recorder.calls == []
    agent = db.get_agent("dev")
    assert agent is not None and agent.grant_all == revoke_all
    assert db.has_any_grant("dev", "ws1")
