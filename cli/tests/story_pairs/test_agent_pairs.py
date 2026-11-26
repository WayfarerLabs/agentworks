"""Story pairs around agent changes: another agent's sessions and access run beside them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from agentworks.agents import initializer as agent_initializer
from agentworks.agents import manager as agent_manager
from agentworks.db import SessionStatus
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager
from agentworks.sessions.manager import _teardown
from agentworks.vms import manager as vm_manager
from tests.runtime_state.test_composition import REFUSE, seed_vm
from tests.runtime_state.test_composition import runtime as runtime
from tests.runtime_state.test_public_stop import _running
from tests.story_pairs._pairs import Pause, labelled

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from agentworks.db import Database


def _pause_account_setup(monkeypatch: pytest.MonkeyPatch, pause: Pause) -> None:
    """Pause an agent create or reinit at its on-VM account setup, under its final set."""
    _pause_account_setup_at(monkeypatch, lambda: pause.reached())


def _pause_account_setup_at(monkeypatch: pytest.MonkeyPatch, reached: Callable[[], object]) -> None:
    # A new account's ownership probe finds nothing; the account setup itself is the pause.
    monkeypatch.setattr(agent_initializer, "create_new_agent_user", lambda *a, **k: None)
    monkeypatch.setattr(agent_initializer, "create_agent_on_vm", lambda *a, **k: reached())
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)


def _stop_teardowns_succeed(monkeypatch: pytest.MonkeyPatch, statuses: dict[str, SessionStatus]) -> None:
    def teardown(plan: object) -> None:
        statuses[plan.session_name] = SessionStatus.STOPPED  # type: ignore[attr-defined]

    monkeypatch.setattr(_teardown, "execute_dedicated_teardown", teardown)
    monkeypatch.setattr(
        manager, "_identity_access", lambda vm, config, agent, *, logger: SimpleNamespace(vm=vm.name, log=logger)
    )


def test_a_session_stop_of_another_agent_runs_beside_an_agent_reinit(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _powers, statuses, _events = runtime
    seed_vm(db, "box")
    db.insert_agent("dev", "box", "agt-dev", template="default")
    _running(db, statuses, "beside", "box", agent=True)
    _stop_teardowns_succeed(monkeypatch, statuses)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    config = make_config()
    pause = Pause(
        db.path,
        ("agw", "session", "stop"),
        lambda other: manager.stop_session(other, config, name="beside", interaction=REFUSE),
    )
    _pause_account_setup(monkeypatch, pause)

    with labelled(("agw", "agent", "reinit")):
        agent_manager.reinit_agent(db, config, name="dev", interaction=TtyInteractionPolicy.REFUSE)

    assert pause.result().succeeded, pause.result().error
    assert statuses["beside"] is SessionStatus.STOPPED
    assert db.get_agent("dev") is not None


def test_access_commands_are_admitted_while_an_agent_is_created(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _powers, statuses, _events = runtime
    seed_vm(db, "box")
    _running(db, statuses, "live", "box")
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    monkeypatch.setattr("agentworks.sessions.tmux.capture_output", lambda *a, **k: "")
    config = make_config()
    access = [
        Pause(
            db.path,
            ("agw", "vm", "exec"),
            lambda other: vm_manager.exec_vm(other, config, "box", ["true"], interaction=REFUSE),
        ),
        Pause(
            db.path,
            ("agw", "session", "attach"),
            lambda other: manager.attach_session(other, config, name="live", interaction=REFUSE),
        ),
        Pause(
            db.path,
            ("agw", "session", "logs"),
            lambda other: manager.session_logs(other, config, name="live", interaction=REFUSE),
        ),
    ]
    _pause_account_setup_at(monkeypatch, lambda: [pause.reached() for pause in access])
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)

    with labelled(("agw", "agent", "create")):
        agent_manager.create_agent(db, config, name="new", vm_name="box", interaction=TtyInteractionPolicy.REFUSE)

    for pause in access:
        assert pause.result().succeeded, pause.result().error
    assert db.get_agent("new") is not None
