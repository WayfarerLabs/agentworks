"""The lock sets of grants, revokes and grant-all writers, and which of them refuse each other."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.agents.grants import _grant_request
from agentworks.agents.realize import agent_create_request
from agentworks.native_locks import LockRequest, NativeLockBusyError, native_locks
from agentworks.native_locks.rows import agent_account_entity
from agentworks.workspaces.locks import workspace_create_request

if TYPE_CHECKING:
    from agentworks.db import Database

pytestmark = pytest.mark.windows


def _seed(db: Database, *workspaces: str) -> None:
    db.insert_vm("box", site="stub", hostname="box")
    for name in workspaces:
        db.insert_workspace(name, f"/srv/{name}", "box", f"ws-{name}")
    db.insert_agent("bot", "box", "agt-bot")


def _refuse_each_other(db: Database, first: LockRequest, second: LockRequest) -> None:
    for held, asked in ((first, second), (second, first)):
        with native_locks(db.path, held), pytest.raises(NativeLockBusyError), native_locks(db.path, asked):
            pass


def test_a_workspace_create_and_a_grant_all_agent_create_refuse_each_other(db: Database) -> None:
    _seed(db, "old")
    vm = db.get_vm("box")
    assert vm is not None
    workspace_create, _agents = workspace_create_request(db, vm, "new")
    agent_create, workspaces = agent_create_request(db, vm, "grantee", grant_all=True)
    assert workspaces == ("old",)
    _refuse_each_other(db, workspace_create, agent_create)


def test_bulk_revoke_refuses_a_named_grant_of_a_workspace_the_agent_was_not_granted(db: Database) -> None:
    _seed(db, "granted", "other")
    db.insert_agent_grant("bot", "granted", "explicit")
    vm, agent = db.get_vm("box"), db.get_agent("bot")
    assert vm is not None and agent is not None
    revoke_all, names = _grant_request(db, vm, agent, [], every=True)
    named_grant, _ = _grant_request(db, vm, agent, ["other"], every=False)
    assert names == ("granted", "other")
    _refuse_each_other(db, revoke_all, named_grant)


def test_bulk_revoke_on_a_vm_with_no_workspace_still_holds_the_account(db: Database) -> None:
    _seed(db)
    vm, agent = db.get_vm("box"), db.get_agent("bot")
    assert vm is not None and agent is not None
    revoke_all, names = _grant_request(db, vm, agent, [], every=True)
    assert names == ()
    agent_delete = LockRequest(exclusive=(agent_account_entity(agent),))
    _refuse_each_other(db, revoke_all, agent_delete)


def test_grants_of_different_workspaces_run_together(db: Database) -> None:
    _seed(db, "a", "b")
    vm, agent = db.get_vm("box"), db.get_agent("bot")
    assert vm is not None and agent is not None
    first, _ = _grant_request(db, vm, agent, ["a"], every=False)
    second, _ = _grant_request(db, vm, agent, ["b"], every=False)
    with native_locks(db.path, first), native_locks(db.path, second):
        pass
