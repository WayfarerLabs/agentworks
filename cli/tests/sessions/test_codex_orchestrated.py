"""The ``codex`` harness integration driven through the real orchestrator: the carry
the unit test cannot prove on its own.

- ``session create`` is ALWAYS fresh through the real op call site: it
  probes no session state at all (a brand-new row owns no codex
  conversation, and both identity channels are name-derived, so adopting
  here is how a namesake would inherit a dead session's conversation),
  provisions the recorder, clears any stale recording, and persists an
  EMPTY ``codex`` namespace;
- the binding round trip through a real restart: a session whose recorder
  file already holds a codex-reported thread id adopts it, resumes it, and
  persists it to the row, and the NEXT restart reads the stored id back
  without re-reading anything else;
- the discovery fallback and the picker leaf through the same call site;
- ``codex`` and ``claude-code`` state coexist in one row blob without
  collision (the first real two-stateful-integration pairing, pinning the
  namespacing seam's promise);
- the marker-era ``discovery_marker`` key is retired from a legacy row.

No test spawns a real ``codex`` binary: the transport calls the op makes
(the recorder read, the ``*-<sid>.jsonl`` rollout probe, and the
source-filtered discovery probe) are stubbed.
"""

from __future__ import annotations

import json
import shlex
import tomllib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, SessionMode, SessionStatus
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.tmux import FingerprintProbe, ProbeStatus, TmuxServerFingerprint

from ..conftest import stub_build_registry, stub_session_resolvers, stub_vm_gates

if TYPE_CHECKING:
    from pathlib import Path

_SID = "939b1597-7c61-4ace-80f4-14617b7b4257"
_OTHER_SID = "22222222-3333-4444-8555-666666666666"
_ROLLOUT = f"/home/me/.codex/sessions/2026/08/01/rollout-2026-08-01T12-00-00-{_SID}.jsonl"
_OTHER_ROLLOUT = f"/home/me/.codex/sessions/2026/08/01/rollout-2026-08-01T12-05-00-{_OTHER_SID}.jsonl"
_CLAUDE_SID = "11111111-2222-4333-8444-555555555555"

# A marker-era blob key, retired by the 2026-08-04 notify-bound redesign.
_LEGACY_MARKER = ".agentworks/codex/s1-deadbeefdeadbeefdeadbeefdeadbeef.launch"


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_build_registry(monkeypatch)


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.ok = returncode == 0
        self.stdout = stdout
        self.stderr = ""


class _CodexTarget:
    """Transport double for the codex op: answers the readiness
    ``command -v codex`` probe and the three op-time probes (the recorder
    read, the ``*-<sid>.jsonl`` rollout presence probe, and the
    source-filtered discovery probe), recording each into a shared event
    log. ``recorded`` seeds the recorder file with a codex-reported thread
    id; ``rollout_present`` decides the bound-id fork; ``discovered``
    (rollout paths, one per line) drives discovery."""

    def __init__(
        self,
        events: list[str],
        *,
        recorded: str | None = None,
        rollout_present: bool = False,
        discovered: str | None = None,
    ) -> None:
        self._events = events
        self._recorded = recorded
        self._present = rollout_present
        self._discovered = discovered

    def run(self, cmd: str, **kwargs: object) -> _Result:
        if "command -v codex" in cmd:
            self._events.append("probe")
            return _Result()
        if ".thread" in cmd:
            self._events.append("read_recorder")
            if self._recorded is None:
                return _Result(returncode=1)  # no recorder file: nothing bound
            return _Result(returncode=0, stdout=f"{self._recorded}\n")
        if "-exec awk" in cmd:
            self._events.append("discover")
            return _Result(returncode=0, stdout=self._discovered or "")
        if ".jsonl" in cmd:
            self._events.append("detect")
            return _Result(returncode=0 if self._present else 1)
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


def _harness_integration_template(monkeypatch: pytest.MonkeyPatch, config: dict[str, object] | None = None) -> None:
    from agentworks.sessions import manager as session_manager

    resolved = SimpleNamespace(
        name="codex", harness_integration="codex", harness_integration_config=config or {}, env={}
    )
    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: resolved)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, target: _CodexTarget) -> None:
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


def _common_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    stub_vm_gates(monkeypatch)
    stub_session_resolvers(monkeypatch)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *a, **k: None)
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *a, **k: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setattr(session_manager, "_regenerate_tmuxinator", lambda *a, **k: None)


def _restart_stubs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: _CodexTarget,
    events: list[str],
    stored_state: dict[str, object] | None,
) -> tuple[Database, dict[str, str]]:
    from agentworks.sessions import manager as session_manager

    db = _seed_db(tmp_path)
    db.insert_session("s1", "ws1", "codex", SessionMode.ADMIN, harness_integration_state=stored_state)
    db.update_session_runtime(
        "s1",
        socket_path="/tmp/s1.sock",
        pid=4242,
        boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        tmux_server_start_ticks=77,
    )

    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, target)
    _common_stubs(monkeypatch)
    _harness_integration_template(monkeypatch)
    _capture_pane_command(monkeypatch, events, captured)

    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *a, **k: SessionStatus.RUNNING)

    def _spy_teardown(*args: object, **kwargs: object) -> None:
        events.append("kill")

    monkeypatch.setattr("agentworks.sessions.manager._lifecycle._teardown_session", _spy_teardown)
    return db, captured


def _restart(db: Database) -> None:
    from agentworks.sessions.manager import restart_session

    restart_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),
        name="s1",
        interaction=TtyInteractionPolicy.REFUSE,
    )  # type: ignore[arg-type]


# -- create: always fresh, probing nothing ------------------------------------


def test_create_launches_fresh_without_probing_any_session_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``session create`` mints a brand-new row, which by definition owns no
    codex conversation, so the op adopts nothing. This
    target would happily report a recording AND a discovery candidate, and
    rollout-discovery probe is not issued; the recorder is only fingerprinted
    so a stale notification cannot bind the new session to prior state. The pane provisions
    the recorder so the following start has something new to bind."""
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _CodexTarget(events, recorded=_SID, rollout_present=True, discovered=f"{_ROLLOUT}\n"))
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

    assert set(events) == {"probe", "read_recorder", "tmux_create"}
    session = db.get_session("s1")
    assert session is not None
    assert session.harness_integration_state == {"codex": {"fresh_pending": _SID}}
    command = captured["command"]
    assert 'chmod +x "$HOME"/.agentworks/codex/.record-thread-v1.sh."$$"' in command
    # No namesake's binding survives a create.
    assert 'rm -f "$HOME"/.agentworks/codex/s1.thread' in command
    assert "notify=[" in command
    assert "exec codex " in command
    assert _SID not in command
    db.close()


# -- restart: the notify-binding round trip ------------------------------------


def test_restart_adopts_the_recorded_thread_id_and_persists_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The binding crux: codex reported this conversation's thread id to
    the recorder during the last launch, so the next op reads it, resumes
    it, and the id lands on the row in the namespaced shape."""
    events: list[str] = []
    target = _CodexTarget(events, recorded=_SID, rollout_present=True)
    db, captured = _restart_stubs(tmp_path, monkeypatch, target=target, events=events, stored_state={"codex": {}})

    _restart(db)

    assert f"resume {_SID}" in captured["command"]
    assert "resuming session s1" in captured["command"]
    # Core obtains the launch decision before the destructive boundary so a
    # strict-resume refusal could leave the old runtime intact.
    assert events.index("read_recorder") < events.index("kill") < events.index("tmux_create")
    assert "discover" not in events  # a bound id needs no fallback
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"codex": {"session_id": _SID}}
    db.close()


def test_restart_resumes_the_stored_id_without_a_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The round trip's second half: with the id stored, a later op
    resumes it verbatim even though the recorder file is gone (the
    recording is a binding mechanism, not the system of record)."""
    events: list[str] = []
    target = _CodexTarget(events, rollout_present=True)
    db, captured = _restart_stubs(
        tmp_path, monkeypatch, target=target, events=events, stored_state={"codex": {"session_id": _SID}}
    )

    _restart(db)

    assert f"resume {_SID}" in captured["command"]
    assert "resuming session s1" in captured["command"]
    assert "discover" not in events
    assert events.index("detect") < events.index("kill") < events.index("tmux_create")
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"codex": {"session_id": _SID}}  # unchanged
    db.close()


def test_restart_adopts_a_discovered_rollout_when_nothing_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback through the real call site: no recording and no stored
    id, one source-filtered rollout in the workspace, so the op adopts its
    uuid and persists it."""
    events: list[str] = []
    target = _CodexTarget(events, discovered=f"{_ROLLOUT}\n", rollout_present=True)
    db, captured = _restart_stubs(tmp_path, monkeypatch, target=target, events=events, stored_state={"codex": {}})

    _restart(db)

    assert f"resume {_SID}" in captured["command"]
    assert "identified this session" in captured["command"]
    assert events.index("read_recorder") < events.index("discover")
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"codex": {"session_id": _SID}}
    db.close()


def test_restart_with_several_candidates_opens_the_picker_and_binds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambiguity reaches the operator as codex's own session picker in the
    pane, not as a failed op: the row keeps no id, and the next completed
    turn binds whatever the human picked."""
    events: list[str] = []
    target = _CodexTarget(events, discovered=f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n")
    db, captured = _restart_stubs(tmp_path, monkeypatch, target=target, events=events, stored_state={"codex": {}})

    _restart(db)

    assert "exec codex resume -c tui.resume_cwd=current" in captured["command"]
    assert "could not identify this session" in captured["command"]
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"codex": {}}  # nothing bound
    db.close()


def test_restart_with_a_gone_rollout_drops_the_id_and_launches_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned archived policy end to end: a bound id whose rollout is
    gone (archived or deleted) drops the id, finds nothing to adopt, and
    launches fresh with the stale-specific decision line."""
    events: list[str] = []
    target = _CodexTarget(events, rollout_present=False)
    db, captured = _restart_stubs(
        tmp_path, monkeypatch, target=target, events=events, stored_state={"codex": {"session_id": _SID}}
    )

    _restart(db)

    assert "archived or gone; starting new session s1" in captured["command"]
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"codex": {}}  # the stale id is gone
    db.close()


def test_restart_retires_a_marker_era_blob_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row written by the marker-era integration (2026-08-01 through
    2026-08-04) still resumes: the retired ``discovery_marker`` key is
    dropped from the persisted blob and its file removed by the pane."""
    events: list[str] = []
    target = _CodexTarget(events, rollout_present=True)
    db, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        target=target,
        events=events,
        stored_state={"codex": {"session_id": _SID, "discovery_marker": _LEGACY_MARKER}},
    )

    _restart(db)

    assert f"resume {_SID}" in captured["command"]
    assert f'rm -f "$HOME"/{_LEGACY_MARKER}' in captured["command"]
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"codex": {"session_id": _SID}}
    db.close()


# -- two stateful harness integrations share one row blob ---------------------


def test_codex_and_claude_code_state_coexist_in_one_blob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first real two-stateful-integration pairing: a row whose blob
    already carries a ``claude-code`` namespace (the template was
    re-pointed to codex) runs a codex op that binds its own id into the
    ``codex`` namespace, and the claude id is neither read (no inherited
    ``session_id``) nor dropped on persist."""
    events: list[str] = []
    target = _CodexTarget(events, recorded=_SID, rollout_present=True)
    db, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        target=target,
        events=events,
        stored_state={"claude-code": {"session_id": _CLAUDE_SID}, "codex": {}},
    )

    _restart(db)

    # codex did NOT inherit the claude id; it bound the one codex reported.
    assert f"resume {_SID}" in captured["command"]
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {
        "claude-code": {"session_id": _CLAUDE_SID},
        "codex": {"session_id": _SID},
    }
    db.close()


# -- substitution-safety: the generated snippet is not mangled ----------------


def test_substitution_preserves_workload_values_and_substitutes_extra_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex JSON setup and TOML developer instructions remain literal
    across manager substitution, while ``extra_args`` still expands."""
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _CodexTarget(events))
    _common_stubs(monkeypatch)
    _harness_integration_template(
        monkeypatch,
        {
            "goal": "goal {{session_name}} {{component}}",
            "initial_prompt": "prompt {{session_name}} {{component}}",
            "agent": "agent {{session_name}} {{component}}",
            "developer_instructions": 'developer {{session_name}} {{component}} "quoted"\nline',
            "extra_args": ["-p", "profile-{{session_name}}"],
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
    # The generated skeleton survived substitution unmangled, the recorder
    # provisioning and the notify override's nested quoting included.
    assert command.startswith("sh -c ")
    assert 'mkdir -p "$HOME"/.agentworks/codex' in command
    assert 'notify=["' in command
    assert '"$HOME"/.agentworks/codex/record-thread-v1.sh' in command
    assert "exec codex " in command
    inner = shlex.split(command)[2]
    inner_argv = shlex.split(inner)
    argv = inner_argv[inner_argv.index("codex") + 1 :]
    developer = next(token for token in argv if token.startswith("developer_instructions="))
    assert tomllib.loads(developer)["developer_instructions"] == (
        'developer {{session_name}} {{component}} "quoted"\nline'
    )
    prompt = argv[-1]
    setup_json = next(part for part in prompt.split("\n\n") if part.startswith("{"))
    assert json.loads(setup_json) == {
        "agent": "agent {{session_name}} {{component}}",
        "goal": "goal {{session_name}} {{component}}",
    }
    assert "prompt {{session_name}} {{component}}" in prompt
    assert argv[argv.index("-p") + 1] == "profile-s1"
    db.close()
