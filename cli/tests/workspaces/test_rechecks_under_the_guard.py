"""Grant, revoke, repair and copy read their rows again under the guard, and refuse what changed since.

Each command's gate is replaced by one that changes a row while it is open,
standing in for another command that ran between the gate's prompts and the
guard. The refusal comes before the command changes anything.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.agents import grants as agent_grants
from agentworks.errors import StateConflictError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.workspaces import manager as workspace_manager
from tests.conftest import locking_node
from tests.workspaces._lifecycle_support import Guest, route, seed, seed_workspace

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.db import Database, VMRow


def _changing_gate(change: Callable[[Database], object]) -> Callable[..., contextlib.AbstractContextManager[object]]:
    """A ``gated_vm_boundary`` stand-in that admits the VM as read, then runs ``change`` once."""
    changed: list[bool] = []

    @contextlib.contextmanager
    def gate(db: Database, _config: object, _registry: object, vm: VMRow, **_kwargs: object) -> Iterator[object]:
        node = locking_node(db, vm)
        if not changed:
            changed.append(True)
            change(db)
        yield node, None, None

    return gate


@pytest.fixture
def guest(db: Database, monkeypatch: pytest.MonkeyPatch) -> Guest:
    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *a, **k: object())
    fake = Guest()
    route(monkeypatch, fake)
    return fake


VM_CHANGED = ("vm-changed", lambda db: db.update_vm_tailscale("box", "100.64.0.77"))
AGENT_CHANGED = ("agent-changed", lambda db: db.update_agent_grant_all("a1", True))


@pytest.mark.parametrize("change", [VM_CHANGED[1], AGENT_CHANGED[1]], ids=[VM_CHANGED[0], AGENT_CHANGED[0]])
def test_grant_refuses_what_changed_before_its_guard(
    db: Database, guest: Guest, make_config, monkeypatch: pytest.MonkeyPatch, change: Callable[[Database], object]
) -> None:
    monkeypatch.setattr(agent_grants, "gated_vm_boundary", _changing_gate(change))

    with pytest.raises(StateConflictError):
        agent_grants.grant_workspaces(
            db, make_config(), agent_name="a1", workspace_names=["ws1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert not db.has_any_grant("a1", "ws1")
    assert guest.requests == []


@pytest.mark.parametrize("change", [VM_CHANGED[1], AGENT_CHANGED[1]], ids=[VM_CHANGED[0], AGENT_CHANGED[0]])
def test_revoke_refuses_what_changed_before_its_guard(
    db: Database, guest: Guest, make_config, monkeypatch: pytest.MonkeyPatch, change: Callable[[Database], object]
) -> None:
    db.insert_agent_grant("a1", "ws1", "explicit")
    monkeypatch.setattr(agent_grants, "gated_vm_boundary", _changing_gate(change))

    with pytest.raises(StateConflictError):
        agent_grants.revoke_workspaces(
            db, make_config(), agent_name="a1", workspace_names=["ws1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert db.has_any_grant("a1", "ws1")
    assert guest.requests == []


@pytest.mark.parametrize(
    "change",
    [
        VM_CHANGED[1],
        lambda db: db.update_workspace_path("ws1", "/elsewhere/ws1"),
        lambda db: db.insert_agent_grant("a1", "ws1", "explicit"),
    ],
    ids=[VM_CHANGED[0], "workspace-changed", "grant-added"],
)
def test_repair_refuses_what_changed_before_its_guard(
    db: Database, guest: Guest, make_config, monkeypatch: pytest.MonkeyPatch, change: Callable[[Database], object]
) -> None:
    monkeypatch.setattr("agentworks.workspaces.manager.repair.gated_vm_boundary", _changing_gate(change))

    with pytest.raises(StateConflictError):
        workspace_manager.repair_workspace(db, make_config(), "ws1", interaction=TtyInteractionPolicy.REFUSE)

    assert guest.requests == []


@pytest.mark.parametrize(
    "change",
    [
        lambda db: db.update_workspace_path("ws1", "/elsewhere/ws1"),
        lambda db: seed_workspace(db, vm_name="box", name="ws2"),
        lambda db: db.insert_agent("late", "box", "agt-late", template="default", grant_all=True),
    ],
    ids=["source-changed", "destination-appeared", "grant-all-agent-appeared"],
)
def test_copy_refuses_what_changed_before_its_guards(
    db: Database, guest: Guest, make_config, monkeypatch: pytest.MonkeyPatch, change: Callable[[Database], object]
) -> None:
    monkeypatch.setattr("agentworks.workspaces.manager.copy.gated_vm_boundary", _changing_gate(change))

    with pytest.raises(StateConflictError):
        workspace_manager.copy_workspace(
            db, make_config(), "ws1", dest_name="ws2", vm_name="box", interaction=TtyInteractionPolicy.REFUSE
        )

    assert guest.requests == []
