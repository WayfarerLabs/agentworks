"""Account edits beside an agent removal that holds shadow's lock rely on shadow's own serialization.

An agent delete holds only its account exclusive, so other account edits on
the VM are admitted beside it. Its ``userdel -r`` holds shadow's lock
(``lckpwdf``) while it removes the home; a concurrent edit waits on that lock
and, past its 15 s, the tool exits non-zero. The scripted guest models that
exit (status 10, shadow's "cannot lock" status for ``usermod``, ``userdel``
and ``groupadd``) for every account edit while the removal runs. Whether real
shadow serializes this on each release is the integration run's to prove.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.agents import grants as agent_grants
from agentworks.agents import manager as agent_manager
from agentworks.db import PID_STOPPED, DesiredRuntimeState, SessionMode, SessionStatus
from agentworks.execution.errors import CommandFailed
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.vms import manager as vm_manager
from tests._runtime_probes import owner_probe_line
from tests.native_locks._lock_support import labelled
from tests.runtime_state.runtime_support import REFUSE, running_session, seed_vm
from tests.runtime_state.runtime_support import runtime as runtime
from tests.story_pairs._pairs import Pause
from tests.vm_access_support import command_line, guest_result, is_command_check, route_vm_access_to_guest

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.execution.results import Result
    from tests.vm_access_support import Call

SHADOW_LOCK_TIMEOUT_STATUS = 10
ACCOUNT_EDITS = ("useradd ", "usermod ", "userdel ", "groupadd ", "groupdel ", "gpasswd ")


class _Shadow:
    """The VM's account files: ``userdel -r agt-gone`` holds their lock while the pause runs."""

    def __init__(self, removal: list[Pause]) -> None:
        self._removal = removal
        self.locked = False

    def answer(self, call: Call) -> Result | None:
        line = command_line(call.request)
        if line is None or is_command_check(call.request):
            return None
        probe = owner_probe_line(line)
        if probe.startswith(("test -e ", "pgrep ")):
            return guest_result(1)
        if "AGW_RUNTIME_STARTUP=" in line:
            return guest_result(stdout="AGW_RUNTIME_STARTUP=off\n")
        if probe == "userdel -r agt-gone":
            self.locked = True
            try:
                for pause in self._removal:
                    pause.reached()
            finally:
                self.locked = False
            return guest_result()
        if self.locked and probe.startswith(ACCOUNT_EDITS):
            return guest_result(SHADOW_LOCK_TIMEOUT_STATUS)
        return guest_result()


def _seed(db: Database, statuses: dict[str, SessionStatus]) -> None:
    seed_vm(db, "box")
    for agent in ("gone", "other", "third", "runner"):
        db.insert_agent(agent, "box", f"agt-{agent}")
    running_session(db, statuses, "attached", "box")
    socket = "/run/agentworks/agent-tmux-sockets/agt-runner/theirs.sock"
    db.insert_session(
        "theirs",
        "ws-box",
        "default",
        SessionMode.AGENT,
        agent_name="runner",
        socket_path=socket,
        desired_runtime_state=DesiredRuntimeState.STOPPED,
    )
    db.update_session_runtime("theirs", socket_path=socket, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None)


def test_account_edits_beside_an_agent_removal_holding_shadows_lock_fail_retryably_and_access_is_admitted(
    db: Database, make_config, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _powers, statuses, events = runtime
    _seed(db, statuses)
    config = make_config()
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)
    launch = tmux.create_session

    def launch_at_owner_socket(name: str, *args: object, **kwargs: object) -> tuple[str, int | None]:
        _socket, pid = launch(name, *args, **kwargs)  # type: ignore[arg-type]
        session = db.get_session(name)
        assert session is not None and session.socket_path is not None
        return session.socket_path, pid

    monkeypatch.setattr(tmux, "create_session", launch_at_owner_socket)

    def grant(other: Database) -> None:
        agent_grants.grant_workspaces(
            other, config, agent_name="other", workspace_names=["ws-box"], interaction=TtyInteractionPolicy.REFUSE
        )

    def delete_third(other: Database) -> None:
        agent_manager.delete_agent(other, config, name="third", force=True, yes=True, interaction=REFUSE)

    meanwhile = {
        "grant": Pause(db.path, ("agw", "agent", "grant-workspaces"), grant),
        "delete": Pause(db.path, ("agw", "agent", "delete"), delete_third),
        "attach": Pause(
            db.path,
            ("agw", "session", "attach"),
            lambda other: manager.attach_session(other, config, name="attached", interaction=REFUSE),
        ),
        "start": Pause(
            db.path,
            ("agw", "session", "start"),
            lambda other: manager.start_session(other, config, name="theirs", interaction=REFUSE),
        ),
    }
    route_vm_access_to_guest(monkeypatch, _Shadow(list(meanwhile.values())).answer)

    with labelled("agent", "delete"):
        agent_manager.delete_agent(db, config, name="gone", force=True, yes=True, interaction=REFUSE)

    assert db.get_agent("gone") is None
    for name in ("grant", "delete"):
        error = meanwhile[name].result().error
        assert isinstance(error, CommandFailed), (name, error)
        assert error.status == SHADOW_LOCK_TIMEOUT_STATUS
    assert not db.has_any_grant("other", "ws-box")
    assert db.get_agent("third") is not None
    for name in ("attach", "start"):
        assert meanwhile[name].result().succeeded, (name, meanwhile[name].result().error)
    assert ("session-start", "theirs") in events

    # Each rerun after the removal finished succeeds.
    grant(db)
    delete_third(db)
    assert db.has_any_grant("other", "ws-box")
    assert db.get_agent("third") is None
