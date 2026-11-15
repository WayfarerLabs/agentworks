"""Every tmux request against an agent's server runs as that agent, never as root.

Each path that reaches an agent session's dedicated server from the admin's
access must send each tmux line through ``as_socket_owner_line``: root's
socket guard, then ``runuser`` to the owner. These tests drive status, the
pending-launch proof, pid repair, the runtime observation's fallback, the
checkpoint proof and teardown for a caller that does not own the session on a
recording access, and check the shape of every request that runs tmux;
``test_prepared_runtime.py`` does the same for startup protection.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import PID_STOPPED, SessionMode, SessionRow, SessionStatus
from agentworks.errors import AgentworksError
from agentworks.execution.requests import Script
from agentworks.execution.results import Completed, Result
from agentworks.sessions import tmux
from agentworks.sessions.manager import _pids, _runtime_observation, _status, _teardown
from agentworks.sessions.tmux import AGENT_SOCKET_ROOT, as_socket_owner_line
from tests._runtime_probes import framed_runtime_result, owner_probe_line
from tests.vm_access_support import Admin, Call, RecordingVMAccess

if TYPE_CHECKING:
    from agentworks.db import Database

AGENT_USER = "agt-a1"
SOCKET = f"{AGENT_SOCKET_ROOT}/{AGENT_USER}/s1.sock"
BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@dataclass
class _Reply:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


ABSENT_SERVER = {
    "has-session": _Reply(stderr="can't find session: s1", returncode=1),
    "list-sessions": _Reply(stderr=f"no server running on {SOCKET}", returncode=1),
    "boot_id": _Reply(stdout=BOOT + "\n"),
    # The stale socket's removal, then the check that nothing is left.
    "rm -f": _Reply(),
    "test -e ": _Reply(returncode=1),
}


def _admin(replies: dict[str, _Reply] | None = None) -> RecordingVMAccess:
    """The admin's recording access; each line's owner command answers from the first ``replies`` key it contains."""

    def answer(call: Call) -> Result:
        request = call.request
        assert isinstance(request, Script)
        line = owner_probe_line(request.source)
        reply = next((reply for key, reply in (replies or {}).items() if key in line), _Reply())
        framed = framed_runtime_result(line, replace(reply))
        return Result(Completed(framed.returncode), framed.stdout.encode(), framed.stderr.encode())

    access = RecordingVMAccess([], Admin())
    access.answer = answer
    return access


def _sent(admin: RecordingVMAccess, word: str) -> bool:
    """Whether any script ``admin`` ran contains ``word``."""
    return any(isinstance(request, Script) and word in request.source for request in admin.requests())


def assert_tmux_runs_as_the_agent(admin: RecordingVMAccess) -> None:
    """Every request that runs tmux is root's guard and ``runuser`` to the agent around its own line."""
    requests = [request for request in admin.requests() if isinstance(request, Script)]
    tmux_requests = [request for request in requests if "tmux " in request.source]
    assert tmux_requests, "the path sent no tmux request"
    for request in tmux_requests:
        expected = as_socket_owner_line(
            owner_probe_line(request.source), socket_path=SOCKET, owner=AGENT_USER, switch_user=True
        )
        assert (request.elevated, request.source) == (True, expected)


@pytest.fixture
def agent_row(db: Database) -> SessionRow:
    db.insert_vm("box", site="site", hostname="box")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_agent("a1", "box", AGENT_USER)
    db.insert_session("s1", "ws", "default", SessionMode.AGENT, agent_name="a1", socket_path=SOCKET)
    db.update_session_runtime("s1", socket_path=SOCKET, pid=4242, boot_id=BOOT, tmux_server_start_ticks=77)
    row = db.get_session("s1")
    assert row is not None
    return row


def test_status_of_an_agent_session(db: Database, agent_row: SessionRow) -> None:
    admin = _admin({**ABSENT_SERVER, "/proc/4242": _Reply(stdout="absent")})

    assert _status.check_session_status(agent_row, admin=admin, db=db) is SessionStatus.STOPPED
    assert_tmux_runs_as_the_agent(admin)


def test_status_of_a_pending_launch(db: Database, agent_row: SessionRow) -> None:
    pending = replace(agent_row, pid=None, tmux_server_start_ticks=None, run_id="11111111-2222-4333-8444-555555555555")
    admin = _admin({"has-session": _Reply()})

    _status.check_session_status(pending, admin=admin, db=db)

    assert _sent(admin, "display-message")
    assert_tmux_runs_as_the_agent(admin)


def test_pid_repair(db: Database, agent_row: SessionRow) -> None:
    unrepaired = replace(agent_row, pid=None, tmux_server_start_ticks=None)
    admin = _admin({"display-message": _Reply(stdout="not a pid")})

    with pytest.raises(AgentworksError):
        _pids._repair_session_pid(unrepaired, admin=admin, db=db, announce=False)

    assert_tmux_runs_as_the_agent(admin)


def test_runtime_observation_fallback(db: Database, agent_row: SessionRow) -> None:
    unrepaired = replace(agent_row, pid=None, tmux_server_start_ticks=None)
    admin = _admin(ABSENT_SERVER)

    _runtime_observation.observe_session_runtime(unrepaired, admin=admin, db=db)

    assert _sent(admin, "display-message")
    assert_tmux_runs_as_the_agent(admin)


def test_checkpoint_proof(db: Database, agent_row: SessionRow) -> None:
    unbooted = replace(agent_row, pid=None, boot_id=None, tmux_server_start_ticks=None)
    vm = db.get_vm("box")
    assert vm is not None
    admin = _admin(ABSENT_SERVER)

    _runtime_observation.can_checkpoint_session(unbooted, vm=vm, admin=admin, db=db)

    assert_tmux_runs_as_the_agent(admin)


@pytest.mark.parametrize("complete", [True, False], ids=["complete-identity", "stopped-row"])
def test_teardown_by_a_caller_that_does_not_own_the_session(
    db: Database, agent_row: SessionRow, monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    """Stop-all, agent delete and workspace delete tear an agent's session down on the admin's access."""
    row = agent_row if complete else replace(agent_row, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None)
    if not complete:
        db.update_session_runtime("s1", socket_path=SOCKET, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None)
    admin = _admin(
        {
            "display-message -p '#{pid}'": _Reply(stdout="4242\n"),
            "rm -f": _Reply(),
            "test -e ": _Reply(returncode=1),
            **({} if complete else {"display-message": _Reply(stderr=f"no server running on {SOCKET}", returncode=1)}),
        }
    )

    def capture(*, run_command: tmux.RunCommand, socket_path: str) -> tmux.FingerprintProbe:
        run_command(tmux.tmux_cmd("display-message -p '#{pid}'", socket_path), check=False)
        if not complete:
            return tmux.FingerprintProbe(tmux.ProbeStatus.ABSENT)
        return tmux.FingerprintProbe(tmux.ProbeStatus.PRESENT, tmux.TmuxServerFingerprint(4242, BOOT, 77))

    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", capture)
    monkeypatch.setattr(_teardown, "_prove_plan_runtime_absent", lambda plan: True)

    _teardown.teardown_dedicated_session(row, access=admin, target_owns_session=False, db=db, force=False)

    if complete:
        assert _sent(admin, "kill-server")
    assert_tmux_runs_as_the_agent(admin)
