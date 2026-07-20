"""The ``claude-code`` harness driven through the real orchestrator: the
carry the unit test cannot prove on its own (plan Tests P2 / P4).

- ``session create`` produces the launch pane string through the real op
  call site, and the minted Claude session id persists to the row's
  ``harness_state``;
- ``session restart`` produces the resume string, reading the stored id
  back, with the restart-post-kill end state (row survives, kill precedes
  the tmux recreate);
- a session predating the ``harness_state`` column (blob ``{}``) mints and
  persists its id on the first restart;
- the visible decision reaches the pane string through the real launch;
- the relocated template-var substitution does not mangle the generated
  ``sh -c`` snippet, and DOES substitute an operator ``extra_args`` var.

No test spawns a real ``claude`` binary: the one transport call the op
makes (the ``<sid>.jsonl`` find probe) is stubbed, keyed on transcript
presence.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, SessionMode, SessionStatus

from ..conftest import stub_build_registry, stub_session_resolvers, stub_vm_gates

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_build_registry(monkeypatch)


class _Result:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = ""
        self.stderr = ""


class _ClaudeTarget:
    """Transport double for the claude-code op: answers the readiness
    ``command -v claude`` probe and the ``<sid>.jsonl`` find probe,
    recording each into a shared event log. ``transcript_present`` decides
    resume-vs-launch."""

    def __init__(self, events: list[str], *, transcript_present: bool) -> None:
        self._events = events
        self._present = transcript_present

    def run(self, cmd: str, **kwargs: object) -> _Result:
        if "command -v claude" in cmd:
            self._events.append("probe")
            return _Result(ok=True)
        if ".jsonl" in cmd:
            self._events.append("detect")
            return _Result(ok=self._present)
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


def _cc_template(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, object] | None = None
) -> None:
    from agentworks.sessions import manager as session_manager

    resolved = SimpleNamespace(
        name="claude", harness="claude-code", harness_config=config or {}, env={}
    )
    monkeypatch.setattr(
        session_manager, "_resolve_template", lambda *a, **k: resolved
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch, target: _ClaudeTarget) -> None:
    admin_factory = lambda vm, config, **kwargs: target  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", admin_factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", admin_factory)


def _capture_pane_command(
    monkeypatch: pytest.MonkeyPatch, events: list[str], captured: dict[str, str]
) -> None:
    from agentworks.sessions import tmux as tmux_mod

    def _capture(
        name: str, ws_path: str, command: str, linux_user: str, **kwargs: object
    ) -> tuple[str, int]:
        events.append("tmux_create")
        captured["command"] = command
        return ("/tmp/s1.sock", 4243)

    monkeypatch.setattr(tmux_mod, "create_session", _capture)


def _common_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    stub_vm_gates(monkeypatch)
    stub_session_resolvers(monkeypatch)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *a, **k: None)
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *a, **k: "boot-x")
    monkeypatch.setattr(
        session_manager, "_regenerate_tmuxinator", lambda *a, **k: None
    )


# -- create: launch string + minted id persists ------------------------------


def test_create_produces_launch_string_and_persists_the_minted_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _ClaudeTarget(events, transcript_present=False))
    _common_stubs(monkeypatch)
    _cc_template(monkeypatch)
    _capture_pane_command(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
    )

    # The op minted an id, recorded it on the row, and used it in a fresh
    # launch (no transcript on disk).
    session = db.get_session("s1")
    assert session is not None
    sid = session.harness_state["session_id"]
    assert isinstance(sid, str) and len(sid) == 36
    assert f"--session-id {sid}" in captured["command"]
    # The visible decision reaches the pane through the real launch (R4).
    assert "starting new session s1" in captured["command"]
    assert "--resume" not in captured["command"]
    db.close()


def test_create_resumes_when_a_transcript_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _ClaudeTarget(events, transcript_present=True))
    _common_stubs(monkeypatch)
    _cc_template(monkeypatch)
    _capture_pane_command(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
    )

    session = db.get_session("s1")
    assert session is not None
    sid = session.harness_state["session_id"]
    assert f"--resume {sid}" in captured["command"]
    assert "resuming session s1" in captured["command"]
    db.close()


# -- restart: reads the stored id, resumes, post-kill end state --------------


def _restart_stubs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transcript_present: bool,
    stored_state: dict[str, object] | None,
) -> tuple[Database, list[str], dict[str, str]]:
    from agentworks.sessions import manager as session_manager

    db = _seed_db(tmp_path)
    db.insert_session(
        "s1", "ws1", "claude", SessionMode.ADMIN, harness_state=stored_state
    )
    db.update_session_pid("s1", 4242, boot_id="boot-x")

    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(
        monkeypatch, _ClaudeTarget(events, transcript_present=transcript_present)
    )
    _common_stubs(monkeypatch)
    _cc_template(monkeypatch)
    _capture_pane_command(monkeypatch, events, captured)

    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.OK
    )

    def _spy_kill(name: str, **kwargs: object) -> bool:
        events.append("kill")
        return True

    monkeypatch.setattr(session_manager, "_kill_session", _spy_kill)
    return db, events, captured


def test_restart_reads_stored_id_and_resumes_after_the_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.sessions.manager import restart_session

    db, events, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        transcript_present=True,
        stored_state={"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"},
    )

    restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    # The stored id is read back verbatim and resumed.
    assert "--resume 939b1597-7c61-5ace-80f4-14617b7b4257" in captured["command"]
    assert "resuming session s1" in captured["command"]
    # Restart ordering (R7): the detect probe runs AFTER the kill (the old
    # process is dead before the resume-vs-launch decision), and the tmux
    # recreate follows. The row survives.
    assert events.index("kill") < events.index("detect") < events.index("tmux_create")
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_state == {"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"}
    db.close()


def test_restart_of_a_pre_column_session_mints_and_persists_the_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session predating the harness_state column backfilled to ``{}``:
    its first restart under claude-code mints a fresh id (no transcript to
    resume) and persists it, so the NEXT restart can resume."""
    from agentworks.sessions.manager import restart_session

    db, events, captured = _restart_stubs(
        tmp_path, monkeypatch, transcript_present=False, stored_state=None
    )
    assert db.get_session("s1").harness_state == {}  # type: ignore[union-attr]

    restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    session = db.get_session("s1")
    assert session is not None
    sid = session.harness_state["session_id"]
    assert isinstance(sid, str) and len(sid) == 36
    assert f"--session-id {sid}" in captured["command"]
    assert "starting new session s1" in captured["command"]
    db.close()


# -- substitution-safety: the generated snippet is not mangled ---------------


def test_substitution_leaves_the_generated_snippet_intact_and_substitutes_extra_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claude-code snippet is the first harness output carrying literal
    braces (the ``sh -c '...'`` quoting). The relocated template-var
    substitution must not mangle the generated skeleton, while an operator
    ``extra_args`` var still substitutes."""
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _ClaudeTarget(events, transcript_present=False))
    _common_stubs(monkeypatch)
    _cc_template(
        monkeypatch,
        {"extra_args": ["--append-system-prompt", "session {{session_name}}"]},
    )
    _capture_pane_command(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
    )

    command = captured["command"]
    session = db.get_session("s1")
    assert session is not None
    sid = session.harness_state["session_id"]
    # The generated skeleton survived substitution unmangled.
    assert command.startswith("sh -c ")
    assert f"--session-id {sid}" in command
    assert "exec claude" in command
    # The operator's extra_args var WAS substituted (parity with shell).
    assert "session s1" in command
    assert "{{session_name}}" not in command
    db.close()
