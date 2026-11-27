"""Session create rebuilds its lock set under itself, so a grant-all agent created after its probe refuses it.

``session create --new-workspace`` grants the new workspace to every
grant-all agent on the VM, so it locks a grant per such agent. One created
between the probe and the lock is not in the set the command holds; the
request rebuilt under the set names it, and the command refuses before it
realizes anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateConflictError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager as session_manager
from agentworks.vms import manager as vm_manager
from tests.conftest import change_after_probe
from tests.workspaces._lifecycle_support import Guest, seed
from tests.workspaces._lifecycle_support import guest as guest
from tests.workspaces._lifecycle_support import make_config as make_config
from tests.workspaces._lifecycle_support import stub_ssh_identity as stub_ssh_identity

if TYPE_CHECKING:
    from agentworks.db import Database


def test_a_new_workspace_session_refuses_a_grant_all_agent_created_between_its_probe_and_its_lock(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    # The new workspace's tree adds nothing this test needs to the set.
    monkeypatch.setattr("agentworks.workspaces.locks.walk_trees", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.workspaces.locks.with_trees", lambda request, *trees: request)
    monkeypatch.setattr("agentworks.workspaces.locks.require_trees_unchanged", lambda *a, **k: None)
    realized: list[object] = []
    monkeypatch.setattr("agentworks.workspaces.realize.realize_workspace", lambda *a, **k: realized.append(k))
    ran = change_after_probe(
        monkeypatch, lambda: db.insert_agent("late", "box", "agt-late", template="default", grant_all=True)
    )

    with pytest.raises(StateConflictError):
        session_manager.create_session(
            db,
            make_config(),
            name="s1",
            new_workspace=True,
            workspace_name="ws2",
            admin=True,
            vm_name="box",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert ran
    assert realized == []
    assert db.get_workspace("ws2") is None


def test_a_session_for_an_existing_agent_refuses_its_layer_replaced_while_its_secrets_resolved(
    db: Database,
    make_config,  # noqa: ANN001
    guest: Guest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``agent reinit --spec`` finishing during the create's prompt: the graph was built from the old agent layer."""
    from agentworks.instance_specs import parse_instance_spec
    from agentworks.secrets.resolver import Resolver

    seed(db)
    db.insert_agent("a1", "box", "agt-a1", template="default")
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    resolve = Resolver.resolve

    def resolve_then_the_agent_layer_is_replaced(self: Resolver) -> None:
        resolve(self)
        db.instance_state.put_desired_overlay(
            "agent", "a1", parse_instance_spec("agent", '{"env":{"LATER":"y"}}').payload
        )

    monkeypatch.setattr(Resolver, "resolve", resolve_then_the_agent_layer_is_replaced)
    realized: list[object] = []
    monkeypatch.setattr("agentworks.sessions.manager._create._roll_forward", lambda *a, **k: realized.append(k))

    with pytest.raises(StateConflictError):
        session_manager.create_session(
            db,
            make_config(),
            name="s1",
            workspace="ws1",
            agent="a1",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert realized == []
    assert db.get_session("s1") is None
