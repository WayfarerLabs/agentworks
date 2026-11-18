"""A tmux client aimed at an agent's server from the admin connection runs as that agent.

A tmux client carries out its server's requests with the client's own rights,
so neither root nor the VM admin may be the client of an agent-owned server.
Each test drives one caller and checks the shape of every tmux request it
sends: switched, from ``/``, to the agent that owns the server.
"""

from __future__ import annotations

import contextlib
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager, tmux
from agentworks.sessions.manager import _status
from tests._runtime_probes import server_owner_parts

from .test_create_start_restart_orchestrated import BOOT_ID, _restart_fixture
from .test_create_start_restart_orchestrated import _stub_build_registry as _stub_build_registry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db import Database, SessionRow

# The restart fixture's agent session: agent ``a1`` runs as ``agt-a1``.
OWNER = "agt-a1"
SOCKET = "/run/agentworks/agent-tmux-sockets/agt-a1/s1.sock"


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.ok = returncode == 0
        self.stdout = stdout
        self.stderr = ""


class _Recorder:
    """Admin transport double: records each command and whether it elevates."""

    def __init__(self, absent: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.interactive_commands: list[str] = []
        self._absent = absent

    def run(self, command: str, **kwargs: Any) -> _Result:
        self.calls.append((command, kwargs.get("sudo") is True))
        return _Result(1 if command.startswith(self._absent) else 0)

    def interactive(self, command: str, **kwargs: Any) -> int:
        self.interactive_commands.append(command)
        return 0


def _session(db: Database) -> SessionRow:
    row = db.get_session("s1")
    assert row is not None and row.socket_path == SOCKET
    return row


def _server(db: Database) -> manager.SessionTmuxServer | None:
    return manager.resolve_session_tmux_server(db, _session(db))


def _assert_runs_as_owner(command: str, elevated: bool) -> str:
    owned = server_owner_parts(command)
    assert owned is not None and owned[0] == OWNER, command
    return owned[1]


def _fingerprint_recorder(owners: list[object], status: tmux.ProbeStatus) -> Any:
    def capture(**kwargs: Any) -> tmux.FingerprintProbe:
        owners.append(kwargs.get("owner"))
        if status is tmux.ProbeStatus.PRESENT:
            return tmux.FingerprintProbe(status, tmux.TmuxServerFingerprint(4242, BOOT_ID, 77))
        return tmux.FingerprintProbe(status)

    return capture


def test_observation_and_checkpoint_fingerprints_probe_as_the_owner(tmp_path, monkeypatch):
    from agentworks.sessions.manager._runtime_observation import can_checkpoint_session, observe_session_runtime

    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.UNKNOWN)
    owners: list[object] = []
    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", _fingerprint_recorder(owners, tmux.ProbeStatus.ABSENT))
    monkeypatch.setattr(manager, "_prove_stored_runtime_absent", lambda *a, **k: True)
    monkeypatch.setattr(manager, "_get_boot_id", lambda *a: BOOT_ID)
    db._conn.execute("UPDATE sessions SET pid = NULL WHERE name = 's1'")
    db._conn.commit()
    assert observe_session_runtime(_session(db), admin_target=_Recorder(), server=_server(db)) is SessionStatus.STOPPED  # type: ignore[arg-type]
    db._conn.execute("UPDATE sessions SET boot_id = NULL WHERE name = 's1'")
    db._conn.commit()
    vm = db.get_vm("vm1")
    assert vm is not None
    assert can_checkpoint_session(_session(db), vm=vm, admin_target=_Recorder(), server=_server(db))  # type: ignore[arg-type]
    assert owners == [OWNER, OWNER]
    db.close()


def test_startup_protection_check_runs_as_the_owner(tmp_path, monkeypatch):
    from agentworks.sessions.manager._runtime import prepare_session_launch

    from .test_prepared_runtime import _launch_definition

    db, _ = _restart_fixture(tmp_path, monkeypatch, status=SessionStatus.RUNNING)
    admin = _Recorder()
    monkeypatch.setattr(manager, "transport", lambda *a, **k: admin)

    def protection(session_name: str, *, run_command: tmux.RunCommand, socket_path: str) -> bool:
        run_command(tmux.tmux_cmd("show-option -gq @agentworks-launch-pane", socket_path), check=False)
        return False

    monkeypatch.setattr(tmux, "startup_protection_enabled", protection)
    prepare_session_launch(_launch_definition(db))
    probes = [(command, elevated) for command, elevated in admin.calls if "@agentworks-launch-pane" in command]
    assert len(probes) == 1
    _assert_runs_as_owner(*probes[0])
    db.close()


@pytest.mark.parametrize("target_owns_session", [False, True])
def test_dedicated_teardown_kills_the_server_as_its_owner(tmp_path, monkeypatch, target_owns_session):
    from agentworks.sessions.manager import _pids
    from agentworks.sessions.manager._teardown import execute_dedicated_teardown, prepare_dedicated_teardown

    db, _ = _restart_fixture(tmp_path, monkeypatch)
    owners: list[object] = []
    monkeypatch.setattr(
        tmux, "capture_tmux_server_fingerprint", _fingerprint_recorder(owners, tmux.ProbeStatus.PRESENT)
    )
    monkeypatch.setattr(_pids, "_prove_runtime_absent", lambda **k: True)
    target = _Recorder(absent=("test -e ",))
    plan = prepare_dedicated_teardown(
        db,
        _session(db),
        vm_name="vm1",
        target=target,  # type: ignore[arg-type]
        target_owns_session=target_owns_session,
        force=False,
    )
    execute_dedicated_teardown(plan)
    expected_owner = None if target_owns_session else OWNER
    assert plan.owner == expected_owner and owners == [expected_owner]
    kills = [(command, elevated) for command, elevated in target.calls if "kill-server" in command]
    assert len(kills) == 1
    if target_owns_session:
        assert kills[0] == (tmux.tmux_cmd("kill-server", SOCKET), False)
    else:
        assert _assert_runs_as_owner(*kills[0]) == tmux.tmux_cmd("kill-server", SOCKET)
    removals = [(command, elevated) for command, elevated in target.calls if "rm -f" in command]
    assert removals and all(server_owner_parts(command) is None for command, _ in removals)
    assert all(elevated is not target_owns_session for _, elevated in removals)
    db.close()


def test_incomplete_identity_teardown_probes_as_the_owner(tmp_path, monkeypatch):
    from agentworks.sessions.manager import _lifecycle

    db, _ = _restart_fixture(tmp_path, monkeypatch)
    db.update_session_runtime("s1", socket_path=SOCKET, pid=PID_STOPPED, boot_id=None, tmux_server_start_ticks=None)
    owners: list[object] = []
    monkeypatch.setattr(tmux, "capture_tmux_server_fingerprint", _fingerprint_recorder(owners, tmux.ProbeStatus.ABSENT))
    target = _Recorder(absent=("test -e ",))
    _lifecycle._teardown_session(
        _session(db),
        target=target,  # type: ignore[arg-type]
        target_owns_session=False,
        db=db,
        force=False,
        legacy_running_authorized=False,
    )
    assert owners == [OWNER]
    db.close()


@contextlib.contextmanager
def _prepared(target: _Recorder, db: Database) -> Iterator[tuple[object, ...]]:
    vm = db.get_vm("vm1")
    yield (db.get_workspace("ws1"), vm, target.run, partial(target.run, sudo=True), target)


def test_session_logs_capture_as_the_owner(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    target = _Recorder()
    monkeypatch.setattr(manager, "_prepare_vm", lambda db, *a, **k: _prepared(target, db))

    def capture(name: str, *, run_command: tmux.RunCommand, socket_path: str | None, **kwargs: Any) -> str:
        assert socket_path is not None
        run_command(tmux.tmux_cmd("capture-pane -p", socket_path), check=False)
        return ""

    monkeypatch.setattr(tmux, "capture_output", capture)
    manager.session_logs(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=5)),
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )  # type: ignore[arg-type]
    captures = [(command, elevated) for command, elevated in target.calls if "capture-pane" in command]
    assert len(captures) == 1
    assert _assert_runs_as_owner(*captures[0]) == tmux.tmux_cmd("capture-pane -p", SOCKET)
    db.close()


def test_session_attach_runs_the_client_as_the_owner(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    target = _Recorder()
    monkeypatch.setattr(manager, "_prepare_vm", lambda db, *a, **k: _prepared(target, db))
    config = SimpleNamespace(terminal=SimpleNamespace(clear_on_detach="never"))
    manager.attach_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]
    assert target.interactive_commands == [tmux.tmux_attach_cmd("s1", SOCKET, owner=OWNER)]
    db.close()


@pytest.mark.parametrize(
    ("mode", "socket_path"),
    [
        # An admin row outside the VM admin's own directory.
        ("admin", "/run/agentworks/admin-tmux-sockets/someone-else/adm.sock"),
        ("admin", "/tmp/adm.sock"),
        # An agent row in another agent's directory.
        ("agent", "/run/agentworks/agent-tmux-sockets/agt-other/adm.sock"),
    ],
)
@pytest.mark.parametrize("operation", ["attach", "logs"])
def test_logs_and_attach_refuse_a_socket_outside_its_owners_directory(
    tmp_path, monkeypatch, mode, socket_path, operation
):
    from agentworks.db import SessionMode
    from agentworks.errors import StateError

    db, _ = _restart_fixture(tmp_path, monkeypatch)
    db.insert_session(
        "adm",
        "ws1",
        "claude",
        SessionMode.ADMIN if mode == "admin" else SessionMode.AGENT,
        agent_name=None if mode == "admin" else "a1",
        socket_path=socket_path,
    )
    # The real order: the refusal comes before any VM work or status probe.
    monkeypatch.setattr(manager, "check_session_status", _status.check_session_status)
    monkeypatch.setattr(manager, "_prepare_vm", lambda *a, **k: pytest.fail("VM work before the owner check"))
    config = SimpleNamespace(
        terminal=SimpleNamespace(clear_on_detach="never"), session=SimpleNamespace(history_limit=5)
    )
    with pytest.raises(StateError):
        if operation == "attach":
            manager.attach_session(db, config, name="adm", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]
        else:
            manager.session_logs(db, config, name="adm", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]
    db.close()


def _seed_misplaced_agent_row(db: Database) -> None:
    from agentworks.db import SessionMode

    db.insert_session(
        "adm",
        "ws1",
        "claude",
        SessionMode.AGENT,
        agent_name="a1",
        socket_path="/run/agentworks/agent-tmux-sockets/agt-other/adm.sock",
    )


def test_status_observation_never_probes_a_socket_outside_its_agents_directory(tmp_path, monkeypatch):
    db, _ = _restart_fixture(tmp_path, monkeypatch)
    _seed_misplaced_agent_row(db)
    probed: list[str] = []

    def batch(rows: list[tuple[SessionRow, object]], **kwargs: Any) -> dict[str, SessionStatus]:
        probed.extend(row.name for row, _server in rows)
        return {row.name: SessionStatus.RUNNING for row, _server in rows}

    monkeypatch.setattr(_status, "batch_check_status", batch)
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *a, **k: None)
    rows = [row for row in (db.get_session("s1"), db.get_session("adm")) if row is not None]
    assert _status.observe_session_statuses(rows, db=db, config=SimpleNamespace()) == {
        "s1": SessionStatus.RUNNING,
        "adm": SessionStatus.UNKNOWN,
    }
    assert probed == ["s1"]
    db.close()


def test_describe_never_probes_a_socket_outside_its_agents_directory(tmp_path, monkeypatch):
    from agentworks.sessions.manager import _queries

    db, _ = _restart_fixture(tmp_path, monkeypatch)
    _seed_misplaced_agent_row(db)
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *a, **k: None)
    monkeypatch.setattr(manager, "transport", lambda *a, **k: pytest.fail("connected to observe a refused socket"))
    monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: pytest.fail("probed a refused socket"))
    monkeypatch.setattr(_queries, "_session_structural_description", lambda db, config, name, status: status)
    status: object = _queries.session_description(db, SimpleNamespace(), name="adm")  # type: ignore[arg-type]
    assert status is SessionStatus.UNKNOWN
    db.close()
