"""Grok Build state persistence through the real session manager call sites."""

from __future__ import annotations

import shlex
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, SessionMode, SessionStatus
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.tmux import FingerprintProbe, ProbeStatus, TmuxServerFingerprint

from ..conftest import stub_build_registry, stub_session_resolvers, stub_vm_gates

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _stub_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_build_registry(monkeypatch)


class _Result:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = ""
        self.stderr = ""


class _GrokTarget:
    def __init__(self, events: list[str], *, session_present: bool) -> None:
        self._events = events
        self._present = session_present

    def run(self, command: str, **kwargs: object) -> _Result:
        if "command -v grok" in command:
            self._events.append("probe")
            return _Result()
        if "summary.json" in command:
            self._events.append("detect")
            return _Result(self._present)
        return _Result()


def _seed_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5', 'complete')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    return db


def _template(monkeypatch: pytest.MonkeyPatch, config: dict[str, object] | None = None) -> None:
    from agentworks.sessions import manager as session_manager

    resolved = SimpleNamespace(
        name="grok", harness_integration="grok-build", harness_integration_config=config or {}, env={}
    )
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *args, **kwargs: resolved)


def _common_stubs(monkeypatch: pytest.MonkeyPatch, target: _GrokTarget) -> None:
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    factory = lambda vm, config, **kwargs: target  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", factory)
    stub_vm_gates(monkeypatch)
    stub_session_resolvers(monkeypatch)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        session_manager,
        "_get_boot_id",
        lambda *args, **kwargs: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    monkeypatch.setattr(session_manager, "_regenerate_tmuxinator", lambda *args, **kwargs: None)
    _template(monkeypatch)


def _capture_tmux(monkeypatch: pytest.MonkeyPatch, events: list[str], captured: dict[str, str]) -> None:
    from agentworks.sessions import tmux as tmux_mod

    def capture(name: str, ws_path: str, command: str, linux_user: str, **kwargs: object) -> tuple[str, int]:
        events.append("tmux_create")
        captured["command"] = command
        return ("/tmp/s1.sock", 4243)

    monkeypatch.setattr(tmux_mod, "create_session", capture)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: FingerprintProbe(
            ProbeStatus.PRESENT,
            TmuxServerFingerprint(pid=4243, boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", start_ticks=1),
        ),
    )


def test_create_persists_minted_uuid_and_launches_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _common_stubs(monkeypatch, _GrokTarget(events, session_present=False))
    _capture_tmux(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    session = db.get_session("s1")
    assert session is not None
    namespace = session.harness_integration_state["grok-build"]
    assert isinstance(namespace, dict)
    sid = namespace["session_id"]
    assert isinstance(sid, str) and len(sid) == 36
    assert f"--session-id {sid}" in captured["command"]
    db.close()


def test_substitution_preserves_workload_values_and_substitutes_extra_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    target = _GrokTarget(events, session_present=False)
    _common_stubs(monkeypatch, target)
    _template(
        monkeypatch,
        {
            "goal": "goal {{session_name}} {{component}}",
            "initial_prompt": "prompt {{session_name}} {{component}}",
            "agent": "profile {{session_name}} {{component}}",
            "rules": "rules {{session_name}} {{component}}",
            "extra_args": ["--future-flag", "extra-{{session_name}}"],
        },
    )
    _capture_tmux(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    inner = shlex.split(captured["command"])[2]
    inner_argv = shlex.split(inner)
    argv = inner_argv[inner_argv.index("grok") + 1 :]
    assert argv[argv.index("--agent") + 1] == "profile {{session_name}} {{component}}"
    assert argv[argv.index("--rules") + 1] == "rules {{session_name}} {{component}}"
    assert "--prompt" not in argv
    assert "--agent-profile" not in argv
    assert argv[-2] == "--"
    prompt = argv[-1]
    assert "goal {{session_name}} {{component}}" in prompt
    assert "prompt {{session_name}} {{component}}" in prompt
    assert argv[argv.index("--future-flag") + 1] == "extra-s1"
    assert argv.index("--future-flag") < argv.index("--")
    db.close()


def test_restart_reads_uuid_and_detects_after_killing_old_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.sessions import manager as session_manager

    sid = "939b1597-7c61-5ace-80f4-14617b7b4257"
    db = _seed_db(tmp_path)
    db.insert_session(
        "s1",
        "ws1",
        "grok",
        SessionMode.ADMIN,
        harness_integration_state={"grok-build": {"session_id": sid}},
    )
    db.update_session_runtime(
        "s1",
        socket_path="/tmp/s1.sock",
        pid=4242,
        boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        tmux_server_start_ticks=77,
    )

    events: list[str] = []
    captured: dict[str, str] = {}
    _common_stubs(monkeypatch, _GrokTarget(events, session_present=True))
    _capture_tmux(monkeypatch, events, captured)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **kwargs: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *args, **kwargs: SessionStatus.RUNNING)

    def teardown(*args: object, **kwargs: object) -> None:
        events.append("kill")

    monkeypatch.setattr("agentworks.sessions.manager._lifecycle._teardown_session", teardown)
    session_manager.restart_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert f"--resume {sid}" in captured["command"]
    assert events.index("kill") < events.index("detect") < events.index("tmux_create")
    assert db.get_session("s1").harness_integration_state == {  # type: ignore[union-attr]
        "grok-build": {"session_id": sid}
    }
    db.close()
