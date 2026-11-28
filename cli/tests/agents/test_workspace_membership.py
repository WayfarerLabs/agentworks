"""Workspace-group membership on the guest: creating the group, removing a member, and removal before the row.

A scripted guest models the shadow tools by their documented exit statuses:
``groupadd -f`` succeeds whether or not the group exists, and
``usermod -r -G`` exits 0 for a member or a non-member, 6 for a missing user
or group, and something else (1, or 10 when the shadow lock times out) when
the membership is left as it was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.agents import grants as agent_grants
from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Command
from agentworks.secrets.policy import TtyInteractionPolicy
from tests.agents.test_delete_grant_revoke_orchestrated import _reachable, _seed, _seed_workspace
from tests.vm_access_support import Call, guest_result, is_command_check, route_vm_access_to_guest

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.execution.results import Result


@pytest.fixture(autouse=True)
def _stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import stub_vm_ssh_identity

    stub_vm_ssh_identity(monkeypatch)


class _GroupGuest:
    """The guest's group file: which groups exist and who is in each."""

    def __init__(self) -> None:
        self.members: dict[str, set[str]] = {}
        self.removal_status: list[int] = []
        self.on_group_request: list[object] = []

    def answer(self, call: Call) -> Result | None:
        request = call.request
        if is_command_check(request) or not isinstance(request, Command):
            return None
        argv = request.argv
        if argv[0] == "/usr/sbin/groupadd":
            for hook in self.on_group_request:
                hook()  # type: ignore[operator]
            group = argv[-1]
            if group in self.members and "-f" not in argv:
                return guest_result(9)
            self.members.setdefault(group, set())
            return guest_result(0)
        if argv[:2] == ("usermod", "-aG"):
            self.members[argv[2]].add(argv[3])
            return guest_result(0)
        if argv[:3] == ("usermod", "-r", "-G"):
            if self.removal_status:
                return guest_result(self.removal_status.pop(0))
            group, user = argv[3], argv[4]
            if group not in self.members:
                return guest_result(6)
            self.members[group].discard(user)
            return guest_result(0)
        return None


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch) -> _GroupGuest:
    model = _GroupGuest()
    route_vm_access_to_guest(monkeypatch, model.answer)
    return model


def _grant(db: Database, config: object, agent: str, workspace: str) -> None:
    agent_grants.grant_workspaces(
        db,
        config,  # type: ignore[arg-type]
        agent_name=agent,
        workspace_names=[workspace],
        interaction=TtyInteractionPolicy.REFUSE,
    )


def _revoke(db: Database, config: object, *, workspaces: list[str], revoke_all: bool = False) -> None:
    agent_grants.revoke_workspaces(
        db,
        config,  # type: ignore[arg-type]
        agent_name="a1",
        workspace_names=workspaces,
        revoke_all=revoke_all,
        interaction=TtyInteractionPolicy.REFUSE,
    )


def test_grants_racing_to_create_a_missing_group_both_succeed(
    db: Database,
    make_config,  # noqa: ANN001
    guest: _GroupGuest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each grant ensures the group. The other grant creates it just before
    this one's request lands, the window a check-then-create had, and both
    grants still succeed and both agents are members."""
    config = make_config()
    _seed(db)
    db.insert_agent("a2", "box", "agt-a2", template="default")
    _seed_workspace(db, vm_name="box", name="ws1")
    _reachable(monkeypatch, True)
    guest.on_group_request.append(lambda: guest.members.setdefault("ws-ws1", set()))

    _grant(db, config, "a1", "ws1")
    _grant(db, config, "a2", "ws1")

    assert guest.members["ws-ws1"] == {"agt-a1", "agt-a2"}
    assert db.has_any_grant("a1", "ws1") and db.has_any_grant("a2", "ws1")


@pytest.mark.parametrize(
    "seeded",
    [
        pytest.param({"ws-ws1": {"agt-a1"}}, id="member"),
        pytest.param({"ws-ws1": set()}, id="non-member"),
        pytest.param({}, id="missing-group"),
    ],
)
def test_membership_removal_counts_every_absent_outcome_as_removed(
    db: Database,
    make_config,  # noqa: ANN001
    guest: _GroupGuest,
    monkeypatch: pytest.MonkeyPatch,
    seeded: dict[str, set[str]],
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    db.insert_agent_grant("a1", "ws1", "explicit")
    _reachable(monkeypatch, True)
    guest.members.update(seeded)

    _revoke(db, config, workspaces=["ws1"])

    assert "agt-a1" not in guest.members.get("ws-ws1", set())
    assert not db.has_any_grant("a1", "ws1")


def test_membership_removal_of_a_missing_user_counts_as_removed(
    db: Database,
    make_config,  # noqa: ANN001
    guest: _GroupGuest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    db.insert_agent_grant("a1", "ws1", "explicit")
    _reachable(monkeypatch, True)
    guest.removal_status.append(6)

    _revoke(db, config, workspaces=["ws1"])

    assert not db.has_any_grant("a1", "ws1")


@pytest.mark.parametrize("revoke_all", [False, True], ids=["named", "all"])
@pytest.mark.parametrize("status", [1, 10], ids=["failed", "lock-timeout"])
def test_a_failed_removal_raises_and_keeps_the_grant_row_for_a_rerun(
    db: Database,
    make_config,  # noqa: ANN001
    guest: _GroupGuest,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    revoke_all: bool,
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    db.insert_agent_grant("a1", "ws1", "explicit")
    _reachable(monkeypatch, True)
    guest.members["ws-ws1"] = {"agt-a1"}
    guest.removal_status.append(status)

    with pytest.raises(CommandFailed) as raised:
        _revoke(db, config, workspaces=[] if revoke_all else ["ws1"], revoke_all=revoke_all)

    assert raised.value.status == status
    assert db.has_any_grant("a1", "ws1")
    assert guest.members["ws-ws1"] == {"agt-a1"}

    _revoke(db, config, workspaces=[] if revoke_all else ["ws1"], revoke_all=revoke_all)

    assert guest.members["ws-ws1"] == set()
    assert not db.has_any_grant("a1", "ws1")
