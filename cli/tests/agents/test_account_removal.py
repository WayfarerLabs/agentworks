"""Agent account removal: processes stop before ``userdel -r``, and a failed removal keeps the agent's row.

A scripted guest models the account by the tools' documented statuses:
``pgrep -u`` exits 0 while a process runs, 1 when none does and 2 for a
missing user; ``userdel -r`` exits 0 when it removed the account, 6 when
there was none, and something else (a ``userdel-pre.d`` hook's veto, a home
it could not remove) with the account's home and mail spool as they were.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents import initializer as agent_initializer
from agentworks.agents import manager as agent_manager
from agentworks.errors import ExternalError, StateError
from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Command
from agentworks.secrets.policy import TtyInteractionPolicy
from tests.agents.test_delete_grant_revoke_orchestrated import _reachable, _seed
from tests.vm_access_support import Call, guest_result, is_command_check, route_vm_access_to_guest

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.execution.results import Result
    from tests.conftest import CapturedOutput


@pytest.fixture(autouse=True)
def _quick_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import stub_vm_ssh_identity

    stub_vm_ssh_identity(monkeypatch)
    monkeypatch.setattr(agent_initializer, "ACCOUNT_STOP_SECONDS", 0.2)
    monkeypatch.setattr(agent_initializer, "ACCOUNT_STOP_POLL_SECONDS", 0.0)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)


class _AccountGuest:
    """One agent account: whether it exists, its home and spool, and how its processes and ``userdel`` behave."""

    def __init__(self) -> None:
        self.account = True
        self.home = True
        self.spool = True
        self.alive_polls: int | None = 0
        """How many ``pgrep`` checks still find a process; ``None`` is a process that never ends."""
        self.userdel_veto: int | None = None
        self.commands: list[tuple[str, ...]] = []

    def answer(self, call: Call) -> Result | None:
        request = call.request
        if is_command_check(request) or not isinstance(request, Command):
            return None
        argv = request.argv
        self.commands.append(argv)
        if argv[0] == "pgrep":
            if not self.account:
                return guest_result(2)
            if self.alive_polls is None or self.alive_polls > 0:
                if self.alive_polls:
                    self.alive_polls -= 1
                return guest_result(0)
            return guest_result(1)
        if argv[0] == "userdel":
            if not self.account:
                return guest_result(6)
            if self.userdel_veto is not None:
                return guest_result(self.userdel_veto)
            self.account = self.home = self.spool = False
            return guest_result(0)
        return guest_result(0)

    def ran(self, tool: str) -> bool:
        return any(argv[0] == tool for argv in self.commands)


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch) -> _AccountGuest:
    model = _AccountGuest()
    route_vm_access_to_guest(monkeypatch, model.answer)
    return model


def _delete(db: Database, config: Any) -> None:
    agent_manager.delete_agent(db, config, name="a1", force=True, yes=True, interaction=TtyInteractionPolicy.REFUSE)


@pytest.fixture
def config(db: Database, make_config, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN001
    _seed(db)
    _reachable(monkeypatch, True)
    return make_config()


def test_a_process_that_outlives_the_wait_refuses_the_removal_and_keeps_the_row(
    db: Database, config: Any, guest: _AccountGuest
) -> None:
    guest.alive_polls = None

    with pytest.raises(StateError):
        _delete(db, config)

    assert not guest.ran("userdel")
    assert guest.account and guest.home and guest.spool
    assert db.get_agent("a1") is not None


def test_processes_that_end_within_the_wait_let_the_removal_proceed(
    db: Database, config: Any, guest: _AccountGuest
) -> None:
    guest.alive_polls = 2

    _delete(db, config)

    assert not guest.account
    assert db.get_agent("a1") is None


def test_an_account_already_gone_drops_the_row(db: Database, config: Any, guest: _AccountGuest) -> None:
    """``pgrep`` exits 2 for the missing user and ``userdel`` 6, and both count as done."""
    guest.account = guest.home = guest.spool = False

    _delete(db, config)

    assert guest.ran("userdel")
    assert db.get_agent("a1") is None


@pytest.mark.parametrize("status", [1, 12], ids=["pre-hook-veto", "home-not-removed"])
def test_a_failed_userdel_raises_keeps_the_row_and_a_rerun_retries(
    db: Database, config: Any, guest: _AccountGuest, status: int
) -> None:
    guest.userdel_veto = status

    with pytest.raises(CommandFailed) as raised:
        _delete(db, config)

    assert raised.value.status == status
    assert guest.account and guest.home and guest.spool
    assert db.get_agent("a1") is not None

    guest.userdel_veto = None
    _delete(db, config)

    assert not guest.account
    assert db.get_agent("a1") is None


def test_agent_create_rollback_still_warns_when_the_removal_fails(
    db: Database,
    make_config,  # noqa: ANN001
    guest: _AccountGuest,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Agent create's rollback runs before any row exists, so it warns and the create's own failure surfaces."""
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    from tests.conftest import seed_vm_trust

    seed_vm_trust("box")
    _reachable(monkeypatch, True)
    monkeypatch.setattr(agent_initializer, "create_new_agent_user", lambda *a, **k: None)

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("ssh exploded")

    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", _boom)
    guest.userdel_veto = 1

    with pytest.raises(ExternalError, match="ssh exploded"):
        agent_manager.create_agent(db, make_config(), name="a1", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)

    assert captured_output.warnings
    assert db.get_agent("a1") is None
