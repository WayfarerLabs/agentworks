"""Every database-holding entry point validates a row's tmux server before any guest request.

A row whose stored socket fails the owner rule, or names another agent's
directory, is refused or reported unknown, and no command naming that socket
ever reaches the guest. Agent ``a`` and agent ``b`` share one VM; the rows
below belong to ``a``.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.db import SessionMode, SessionStatus
from agentworks.errors import AgentworksError, StateError
from agentworks.sessions import manager
from agentworks.sessions.manager import _queries, _status
from agentworks.sessions.manager._tmux_server import resolve_session_tmux_server
from tests.runtime_state.test_composition import BOOT, REFUSE, seed_vm
from tests.runtime_state.test_composition import runtime as runtime

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database

AGENT_ROOT = "/run/agentworks/agent-tmux-sockets"
ADMIN_ROOT = "/run/agentworks/admin-tmux-sockets"

# (mode, agent, socket path): each fails one part of the owner rule.
REFUSED_ROWS = {
    # A path outside every socket directory.
    "outside-socket-directories": (SessionMode.AGENT, "a", "/tmp/s.sock"),
    # An agent's row with a canonical path in another agent's directory.
    "other-agents-directory": (SessionMode.AGENT, "a", f"{AGENT_ROOT}/agt-b/s.sock"),
    # The row's mode contradicts its socket's directory.
    "agent-row-admin-socket": (SessionMode.AGENT, "a", f"{ADMIN_ROOT}/admin/s.sock"),
    "admin-row-agent-socket": (SessionMode.ADMIN, None, f"{AGENT_ROOT}/agt-a/s.sock"),
}


def _seed(db: Database, mode: SessionMode, agent: str | None, socket_path: str) -> None:
    seed_vm(db, "box")
    db.insert_agent("a", "box", "agt-a")
    db.insert_agent("b", "box", "agt-b")
    db.insert_session("s", "ws-box", "default", mode, agent_name=agent, socket_path=socket_path)
    db.update_session_runtime("s", socket_path=socket_path, pid=4242, boot_id=BOOT, tmux_server_start_ticks=77)


class _Recorder:
    """Transport double that records every command, and any stdin data, it is asked to run."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: Any) -> SimpleNamespace:
        self.commands.append(command)
        self.commands.append(str(kwargs.get("input_data") or ""))
        return SimpleNamespace(returncode=0, ok=True, stdout="", stderr="")

    def naming(self, socket_path: str) -> list[str]:
        """The requests that name ``socket_path``, plainly or in batch status's encoding."""
        encoded = base64.b64encode(socket_path.encode()).decode()
        return [command for command in self.commands if socket_path in command or encoded in command]

    def interactive(self, command: str, **kwargs: Any) -> int:
        self.commands.append(command)
        return 0

    def write_file(self, path: str, content: str, **kwargs: Any) -> None:
        self.commands.append(content)


def _describe(db: Database, config: object) -> object:
    return _queries.session_description(db, config, name="s")  # type: ignore[arg-type]


ENTRY_POINTS: dict[str, Callable[[Database, Any], object]] = {
    "stop": lambda db, config: manager.stop_session(db, config, name="s", interaction=REFUSE),
    "stop --all": lambda db, config: manager.stop_all_sessions(db, config, interaction=REFUSE),
    "start": lambda db, config: manager.start_session(db, config, name="s", interaction=REFUSE),
    "restart": lambda db, config: manager.restart_session(db, config, name="s", yes=True, interaction=REFUSE),
    "delete": lambda db, config: manager.delete_session(db, config, name="s", yes=True, interaction=REFUSE),
    "logs": lambda db, config: manager.session_logs(db, config, name="s", interaction=REFUSE),
    "attach": lambda db, config: manager.attach_session(db, config, name="s", interaction=REFUSE),
}

OBSERVERS: dict[str, Callable[[Database, Any], object]] = {
    "status": lambda db, config: manager.observe_session_statuses(
        [row for row in (db.get_session("s"),) if row is not None], db=db, config=config
    )["s"],
    "describe": _describe,
}


@pytest.fixture
def recorder(runtime: object, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """The composition runtime, with the real status checks and every transport recorded."""
    target = _Recorder()
    factory = lambda *a, **k: target  # noqa: E731
    monkeypatch.setattr(manager, "transport", factory)
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.transports.agent_transport", factory)
    monkeypatch.setattr("agentworks.agents.manager._assert_agent_ssh_works", lambda *a, **k: None)
    monkeypatch.setattr(manager, "check_session_status", _status.check_session_status)
    monkeypatch.setattr(manager, "observe_session_statuses", _status.observe_session_statuses)
    monkeypatch.setattr(_queries, "_session_structural_description", lambda db, config, name, status: status)
    return target


@pytest.mark.parametrize("row", REFUSED_ROWS)
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_lifecycle_logs_and_attach_refuse_before_any_request_names_the_socket(db, make_config, recorder, row, entry):
    mode, agent, socket_path = REFUSED_ROWS[row]
    _seed(db, mode, agent, socket_path)
    with pytest.raises(AgentworksError):
        ENTRY_POINTS[entry](db, make_config())
    assert not recorder.naming(socket_path)


@pytest.mark.parametrize("row", REFUSED_ROWS)
@pytest.mark.parametrize("observer", OBSERVERS)
def test_status_and_describe_report_unknown_without_any_request_naming_the_socket(
    db, make_config, recorder, row, observer
):
    mode, agent, socket_path = REFUSED_ROWS[row]
    _seed(db, mode, agent, socket_path)
    assert OBSERVERS[observer](db, make_config()) is SessionStatus.UNKNOWN
    assert not recorder.naming(socket_path)


@pytest.mark.parametrize("row", REFUSED_ROWS)
def test_the_resolver_refuses_each_rule_violation(db, row):
    mode, agent, socket_path = REFUSED_ROWS[row]
    _seed(db, mode, agent, socket_path)
    session = db.get_session("s")
    assert session is not None
    with pytest.raises(StateError):
        resolve_session_tmux_server(db, session)


@pytest.mark.parametrize(
    ("mode", "agent", "socket_path", "owner"),
    [
        (SessionMode.AGENT, "a", f"{AGENT_ROOT}/agt-a/s.sock", "agt-a"),
        (SessionMode.ADMIN, None, f"{ADMIN_ROOT}/admin/s.sock", None),
    ],
)
def test_the_resolver_names_the_owner_of_a_valid_row(db, mode, agent, socket_path, owner):
    _seed(db, mode, agent, socket_path)
    session = db.get_session("s")
    assert session is not None
    server = resolve_session_tmux_server(db, session)
    assert server is not None and (server.socket_path, server.owner) == (socket_path, owner)
