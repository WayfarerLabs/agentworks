"""The ``claude-code`` harness integration driven through the real orchestrator: the
carry the unit test cannot prove on its own (plan Tests P2 / P4).

- ``session create`` produces the launch pane string through the real op
  call site, and the minted Claude session id persists to the row's
  ``harness_integration_state`` in the NAMESPACED shape
  (``{"claude-code": {"session_id": ...}}``);
- ``session restart`` produces the resume string, reading the stored id
  back, with the restart-post-kill end state (row survives, kill precedes
  the tmux recreate);
- a session predating the ``harness_integration_state`` column (blob ``{}``) mints and
  persists its id on the first restart;
- a pre-NAMESPACING row (flat ``{"session_id": ...}``) is hoisted into the
  ``claude-code`` namespace and resumed with the SAME id, a foreign
  integration's namespace in the blob survives a claude op untouched, and the
  flat legacy key survives ANOTHER integration's op for a later hoist;
- the visible decision reaches the pane string through the real launch;
- the relocated template-var substitution does not mangle the generated
  ``sh -c`` snippet, and DOES substitute an operator ``extra_args`` var.

No test spawns a real ``claude`` binary: the one transport call the op
makes (the ``<sid>.jsonl`` find probe) is stubbed, keyed on transcript
presence.
"""

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


def _harness_integration_template(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object] | None = None,
    *,
    harness_integration: str = "claude-code",
) -> None:
    from agentworks.sessions import manager as session_manager

    resolved = SimpleNamespace(
        name="claude", harness_integration=harness_integration, harness_integration_config=config or {}, env={}
    )
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: resolved)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, target: _ClaudeTarget) -> None:
    admin_factory = lambda vm, config, **kwargs: target  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", admin_factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", admin_factory)


def _capture_pane_command(monkeypatch: pytest.MonkeyPatch, events: list[str], captured: dict[str, str]) -> None:
    from agentworks.sessions import tmux as tmux_mod

    def _capture(name: str, ws_path: str, command: str, linux_user: str, **kwargs: object) -> tuple[str, int]:
        events.append("tmux_create")
        captured["command"] = command
        return ("/tmp/s1.sock", 4243)

    monkeypatch.setattr(tmux_mod, "create_session", _capture)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: FingerprintProbe(
            ProbeStatus.PRESENT,
            TmuxServerFingerprint(pid=4243, boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", start_ticks=1),
        ),
    )


def _claude_ns(db: Database, name: str = "s1") -> dict[str, object]:
    """The persisted row's ``claude-code`` namespace of the (namespaced)
    ``harness_integration_state`` blob."""
    session = db.get_session(name)
    assert session is not None
    namespace = session.harness_integration_state["claude-code"]
    assert isinstance(namespace, dict)
    return namespace


def _common_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    stub_vm_gates(monkeypatch)
    stub_session_resolvers(monkeypatch)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *a, **k: None)
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *a, **k: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setattr(session_manager, "_regenerate_tmuxinator", lambda *a, **k: None)


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
    _harness_integration_template(monkeypatch)
    _capture_pane_command(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    # The op minted an id, recorded it on the row in the namespaced shape
    # (nothing lands at the blob's top level), and used it in a fresh
    # launch (no transcript on disk).
    session = db.get_session("s1")
    assert session is not None
    assert set(session.harness_integration_state) == {"claude-code"}
    sid = _claude_ns(db)["session_id"]
    assert isinstance(sid, str) and len(sid) == 36
    assert f"--session-id {sid}" in captured["command"]
    # The visible decision reaches the pane through the real launch (R4).
    assert "starting new session s1" in captured["command"]
    assert "--resume" not in captured["command"]
    db.close()


def test_create_forces_a_fresh_conversation_when_a_transcript_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _ClaudeTarget(events, transcript_present=True))
    _common_stubs(monkeypatch)
    _harness_integration_template(monkeypatch)
    _capture_pane_command(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    sid = _claude_ns(db)["session_id"]
    assert f"--session-id {sid}" in captured["command"]
    assert "--resume" not in captured["command"]
    db.close()


# -- restart: reads the stored id, continues, post-kill end state --------------


def _restart_stubs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    transcript_present: bool,
    stored_state: dict[str, object] | None,
    harness_integration: str = "claude-code",
) -> tuple[Database, list[str], dict[str, str]]:
    from agentworks.sessions import manager as session_manager

    db = _seed_db(tmp_path)
    db.insert_session("s1", "ws1", "claude", SessionMode.ADMIN, harness_integration_state=stored_state)
    db.update_session_runtime(
        "s1",
        socket_path="/tmp/s1.sock",
        pid=4242,
        boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        tmux_server_start_ticks=77,
    )

    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _ClaudeTarget(events, transcript_present=transcript_present))
    _common_stubs(monkeypatch)
    _harness_integration_template(monkeypatch, harness_integration=harness_integration)
    _capture_pane_command(monkeypatch, events, captured)

    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *a, **k: SessionStatus.RUNNING)

    def _spy_teardown(*args: object, **kwargs: object) -> None:
        events.append("kill")

    monkeypatch.setattr("agentworks.sessions.manager._lifecycle._teardown_session", _spy_teardown)
    return db, events, captured


def test_restart_reads_stored_id_and_resumes_after_the_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.sessions.manager import restart_session

    db, events, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        transcript_present=True,
        stored_state={"claude-code": {"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"}},
    )

    restart_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )  # type: ignore[arg-type]

    # The stored id is read back verbatim and resumed.
    assert "--resume 939b1597-7c61-5ace-80f4-14617b7b4257" in captured["command"]
    assert "resuming session s1" in captured["command"]
    # Restart ordering (R7): the detect probe runs AFTER the kill (the old
    # process is dead before the resume-vs-launch decision), and the tmux
    # recreate follows. The row survives.
    assert events.index("kill") < events.index("detect") < events.index("tmux_create")
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {
        "claude-code": {"session_id": "939b1597-7c61-5ace-80f4-14617b7b4257"}
    }
    db.close()


def test_restart_of_a_pre_column_session_mints_and_persists_the_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session predating the harness_integration_state column backfilled to ``{}``:
    its first restart under claude-code mints a fresh id (no transcript to
    resume) and persists it, so the NEXT restart can resume."""
    from agentworks.sessions.manager import restart_session

    db, events, captured = _restart_stubs(tmp_path, monkeypatch, transcript_present=False, stored_state=None)
    assert db.get_session("s1").harness_integration_state == {}  # type: ignore[union-attr]

    restart_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )  # type: ignore[arg-type]

    sid = _claude_ns(db)["session_id"]
    assert isinstance(sid, str) and len(sid) == 36
    assert f"--session-id {sid}" in captured["command"]
    assert "starting new session s1" in captured["command"]
    db.close()


def test_restart_hoists_a_pre_namespacing_row_and_resumes_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compatibility (pre-namespacing harness_integration_state): DELETE on the next
    major release, with the hoist. A row written before the blob was
    namespaced stores ``session_id`` at the top level. Its first restart
    hoists the id into the ``claude-code`` namespace and RESUMES with the
    SAME id (no new id is minted, history is kept), and the persisted row
    comes out namespaced with the flat key gone."""
    from agentworks.sessions.manager import restart_session

    sid = "939b1597-7c61-5ace-80f4-14617b7b4257"
    db, events, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        transcript_present=True,
        stored_state={"session_id": sid},
    )

    restart_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )  # type: ignore[arg-type]

    assert f"--resume {sid}" in captured["command"]
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"claude-code": {"session_id": sid}}
    db.close()


def test_restart_leaves_a_foreign_namespace_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign integration's namespace in the blob (a template re-pointed
    from another stateful integration) survives a claude-code op untouched:
    the claude id lands in its OWN namespace, and the foreign keys are
    neither read (no inherited ``session_id``) nor dropped on persist."""
    from agentworks.sessions.manager import restart_session

    foreign_sid = "11111111-2222-4333-8444-555555555555"
    db, events, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        transcript_present=False,
        stored_state={"other-harness": {"session_id": foreign_sid}},
    )

    restart_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )  # type: ignore[arg-type]

    # claude-code did NOT inherit the foreign id; it minted its own.
    sid = _claude_ns(db)["session_id"]
    assert isinstance(sid, str) and sid != foreign_sid
    assert f"--session-id {sid}" in captured["command"]
    # The foreign namespace survived the persist verbatim.
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state["other-harness"] == {"session_id": foreign_sid}
    db.close()


def test_restart_under_another_harness_integration_leaves_the_flat_legacy_key_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compatibility (pre-namespacing harness_integration_state): DELETE on the next
    major release, with the hoist. The flat legacy ``session_id`` belongs
    to claude-code; a restart under a DIFFERENT integration (whose hoist is
    the base no-op) must neither adopt nor drop it, so the persisted row
    keeps the flat key verbatim and a later re-point back to claude-code
    can still hoist it."""
    from agentworks.sessions.manager import restart_session

    sid = "939b1597-7c61-5ace-80f4-14617b7b4257"
    db, events, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        transcript_present=False,
        stored_state={"session_id": sid},
        harness_integration="shell",
    )

    restart_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )  # type: ignore[arg-type]

    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"session_id": sid, "shell": {}}
    db.close()


# -- substitution-safety: the generated snippet is not mangled ---------------


def test_substitution_preserves_workload_values_and_substitutes_extra_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workload fields are literal harness input across the real manager
    substitution boundary, while ``extra_args`` retains template expansion."""
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _ClaudeTarget(events, transcript_present=False))
    _common_stubs(monkeypatch)
    _harness_integration_template(
        monkeypatch,
        {
            "goal": "goal {{session_name}} {{component}}",
            "initial_prompt": "prompt {{session_name}} {{component}}",
            "agent": "agent {{session_name}} {{component}}",
            "append_system_prompt": "system {{session_name}} {{component}}",
            "extra_args": ["--future-flag", "extra-{{session_name}}"],
        },
    )
    _capture_pane_command(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    command = captured["command"]
    sid = _claude_ns(db)["session_id"]
    # The generated skeleton survived substitution unmangled.
    assert command.startswith("sh -c ")
    assert f"--session-id {sid}" in command
    assert "exec claude" in command
    inner = shlex.split(command)[2]
    inner_argv = shlex.split(inner)
    argv = inner_argv[inner_argv.index("claude") + 1 :]
    assert argv[argv.index("--agent") + 1] == "agent {{session_name}} {{component}}"
    assert argv[argv.index("--append-system-prompt") + 1] == "system {{session_name}} {{component}}"
    prompt = argv[-1]
    assert "goal {{session_name}} {{component}}" in prompt
    assert "prompt {{session_name}} {{component}}" in prompt
    assert argv[argv.index("--future-flag") + 1] == "extra-s1"
    db.close()
