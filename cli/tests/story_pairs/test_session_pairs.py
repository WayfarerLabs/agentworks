"""Story pairs around sessions: what runs beside a create, a start and a delete, and what each refuses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import PID_STOPPED, DesiredRuntimeState, SessionMode, VMStatus
from agentworks.native_locks import LockMode
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.vms import manager as vm_manager
from tests.native_locks._lock_support import labelled
from tests.runtime_state.runtime_support import REFUSE, seed_session, seed_vm
from tests.runtime_state.runtime_support import runtime as runtime
from tests.story_pairs._pairs import Pause
from tests.vm_access_support import command_line, guest_result, is_command_check, route_vm_access_to_guest

if TYPE_CHECKING:
    import pytest

    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.execution.results import Result
    from tests.vm_access_support import Call

START = ("agw", "session", "start")


def _pause_launch_of(monkeypatch: pytest.MonkeyPatch, name: str, pause: Pause) -> None:
    """Pause the start of session ``name`` at its tmux launch, holding its family's final set."""
    launch = tmux.create_session

    def paused(session_name: str, *args: object, **kwargs: object) -> object:
        if session_name == name:
            pause.reached()
        return launch(session_name, *args, **kwargs)

    monkeypatch.setattr(tmux, "create_session", paused)


def test_a_vm_stop_is_refused_while_a_session_starts_and_names_the_start(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_vm(db, "box")
    seed_session(db, "work", "box", target=DesiredRuntimeState.STOPPED)
    config = make_config()
    pause = Pause(
        db.path, ("agw", "vm", "stop"), lambda other: vm_manager.stop_vm(other, config, "box", interaction=REFUSE)
    )
    _pause_launch_of(monkeypatch, "work", pause)

    with labelled("session", "start"):
        manager.start_session(db, config, name="work", interaction=TtyInteractionPolicy.REFUSE)

    refused = pause.result().refusal()
    assert refused.entity_kind == "vm" and refused.entity_name == "box"
    assert refused.holder is not None
    assert refused.holder.command == START
    assert refused.holder.entity == "vm/box" and refused.holder.mode is LockMode.SHARED
    assert refused.holder.held_for >= 0
    # The stop was refused before it recorded anything or touched the provider.
    vm = db.get_vm("box")
    assert vm is not None and vm.desired_runtime_state is DesiredRuntimeState.RUNNING
    assert ("vm-stop", "box") not in runtime[2]
    assert runtime[0].get("box", VMStatus.RUNNING) is VMStatus.RUNNING
    assert ("session-start", "work") in runtime[2]


def _seed_workspace(db: Database, name: str, vm: str = "box") -> None:
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
        (name, vm, f"/srv/{name}", f"ws-{name}"),
    )
    db._conn.commit()


def test_a_session_start_runs_beside_a_create_of_a_session_sharing_neither_workspace_nor_agent(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_vm(db, "box")
    _seed_workspace(db, "other-ws")
    seed_session(db, "other", "box", target=DesiredRuntimeState.STOPPED)
    db._conn.execute("UPDATE sessions SET workspace_name = 'other-ws' WHERE name = 'other'")
    db._conn.commit()
    config = make_config()
    pause = Pause(
        db.path,
        START,
        lambda other: manager.start_session(other, config, name="other", interaction=TtyInteractionPolicy.REFUSE),
    )
    _pause_launch_of(monkeypatch, "new", pause)

    with labelled("session", "create"):
        manager.create_session(
            db, config, name="new", workspace="ws-box", admin=True, interaction=TtyInteractionPolicy.REFUSE
        )

    assert pause.result().succeeded, pause.result().error
    assert {("session-start", "new"), ("session-start", "other")} <= set(runtime[2])
    assert db.get_session("new") is not None


def _pause_at_membership_removal(monkeypatch: pytest.MonkeyPatch, pause: Pause) -> None:
    """Pause a session delete at its removal of the agent from the workspace group, under its set."""

    def guest(call: Call) -> Result | None:
        line = command_line(call.request)
        if line is None or is_command_check(call.request):
            return None
        if line.startswith("usermod -r -G"):
            pause.reached()
        return guest_result()

    route_vm_access_to_guest(monkeypatch, guest)


def _seed_agent_session(db: Database, name: str) -> None:
    db.insert_agent("bot", "box", "agt-bot")
    socket = f"/run/agentworks/agent-tmux-sockets/agt-bot/{name}.sock"
    db.insert_session(name, "ws-box", "default", SessionMode.AGENT, agent_name="bot", socket_path=socket)
    db.update_session_runtime(name, socket_path=socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None)


def _delete_agent_session_beside(db: Database, config: Config, monkeypatch: pytest.MonkeyPatch, pause: Pause) -> None:
    _pause_at_membership_removal(monkeypatch, pause)
    with labelled("session", "delete"):
        manager.delete_session(db, config, name="work", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert db.get_session("work") is None


def test_a_session_create_for_the_workspace_and_agent_of_a_session_being_deleted_is_refused(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_vm(db, "box")
    _seed_agent_session(db, "work")
    config = make_config()
    pause = Pause(
        db.path,
        ("agw", "session", "create"),
        lambda other: manager.create_session(
            other, config, name="again", workspace="ws-box", agent="bot", interaction=TtyInteractionPolicy.REFUSE
        ),
    )

    _delete_agent_session_beside(db, config, monkeypatch, pause)

    refused = pause.result().refusal()
    assert refused.entity_kind == "grant"
    assert refused.holder is not None and refused.holder.command == ("agw", "session", "delete")
    assert db.get_session("again") is None


def test_a_revoke_of_the_workspace_of_an_agent_session_being_deleted_is_refused(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.agents.grants import revoke_workspaces

    seed_vm(db, "box")
    _seed_agent_session(db, "work")
    config = make_config()
    pause = Pause(
        db.path,
        ("agw", "agent", "revoke-workspaces"),
        lambda other: revoke_workspaces(
            other, config, agent_name="bot", workspace_names=["ws-box"], interaction=TtyInteractionPolicy.REFUSE
        ),
    )

    _delete_agent_session_beside(db, config, monkeypatch, pause)

    refused = pause.result().refusal()
    assert refused.entity_kind == "grant"
    assert refused.holder is not None and refused.holder.command == ("agw", "session", "delete")
