"""The ``codex`` harness integration driven through the real orchestrator: the carry
the unit test cannot prove on its own.

- ``session create`` produces the fresh-launch pane string through the
  real op call site (marker touch included), runs NO discovery probe (a
  brand-new blob has no stored anchor), and persists the NAMESPACED
  ``codex`` blob carrying only the minted anchor;
- the discovery-adoption round trip through a real restart: a session
  whose stored anchor has one newer matching rollout adopts the
  codex-minted uuid, resumes it, and persists it to the row (anchor
  consumed), and the NEXT restart resumes the stored id without
  re-running discovery;
- ``codex`` and ``claude-code`` state coexist in one row blob without
  collision (the first real two-stateful-integration pairing, pinning the
  namespacing seam's promise);
- ``session restart`` with a stored id resumes it with the
  restart-post-kill end state (kill precedes the probe precedes the tmux
  recreate).

No test spawns a real ``codex`` binary: the transport calls the op makes
(the ``*-<sid>.jsonl`` rollout probe and the ``.launch``-marker discovery
probe) are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, SessionMode, SessionStatus

from ..conftest import stub_build_registry, stub_session_resolvers, stub_vm_gates

if TYPE_CHECKING:
    from pathlib import Path

_SID = "939b1597-7c61-4ace-80f4-14617b7b4257"
_ROLLOUT = f"/home/me/.codex/sessions/2026/08/01/rollout-2026-08-01T12-00-00-{_SID}.jsonl"
_CLAUDE_SID = "11111111-2222-4333-8444-555555555555"

# A stored discovery anchor, as a previous fresh launch would have minted it.
_ANCHOR = ".agentworks/codex/s1-deadbeefdeadbeefdeadbeefdeadbeef.launch"


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
    ``command -v codex`` probe, the ``*-<sid>.jsonl`` rollout probe, and
    the ``.launch``-marker discovery probe, recording each into a shared
    event log. ``rollout_present`` decides the stored-id fork;
    ``discovered`` (rollout paths, one per line) drives discovery."""

    def __init__(
        self,
        events: list[str],
        *,
        rollout_present: bool = False,
        discovered: str | None = None,
    ) -> None:
        self._events = events
        self._present = rollout_present
        self._discovered = discovered

    def run(self, cmd: str, **kwargs: object) -> _Result:
        if "command -v codex" in cmd:
            self._events.append("probe")
            return _Result()
        if ".launch" in cmd:
            self._events.append("discover")
            if self._discovered is None:
                return _Result(returncode=3)  # no marker: definitively fresh
            return _Result(returncode=0, stdout=self._discovered)
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


def _common_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    stub_vm_gates(monkeypatch)
    stub_session_resolvers(monkeypatch)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *a, **k: None)
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *a, **k: "boot-x")
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
    db.update_session_pid("s1", 4242, boot_id="boot-x")

    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, target)
    _common_stubs(monkeypatch)
    _harness_integration_template(monkeypatch)
    _capture_pane_command(monkeypatch, events, captured)

    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(session_manager, "check_session_status", lambda *a, **k: SessionStatus.OK)

    def _spy_kill(name: str, **kwargs: object) -> bool:
        events.append("kill")
        return True

    monkeypatch.setattr(session_manager, "_kill_session", _spy_kill)
    return db, captured


# -- create: fresh launch string + the minted anchor persists -----------------


def test_create_launches_fresh_with_no_discovery_and_persists_the_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh create's blob holds no stored anchor, so there is
    definitively nothing to discover: NO discovery probe runs (the
    anti-namesake rule: a session recreated under a deleted session's
    name cannot adopt the dead session's conversation), the op launches
    fresh with the minted marker touch, and the row's blob carries the
    codex namespace with only that anchor. The id is codex-minted and
    adopted only by a later op's discovery, never stored at create."""
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _CodexTarget(events))
    _common_stubs(monkeypatch)
    _harness_integration_template(monkeypatch)
    _capture_pane_command(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
    )

    assert "discover" not in events  # no anchor stored: no probe at all
    session = db.get_session("s1")
    assert session is not None
    namespace = session.harness_integration_state["codex"]
    assert isinstance(namespace, dict)
    anchor = namespace.get("discovery_marker")
    assert isinstance(anchor, str)
    assert anchor.startswith(".agentworks/codex/s1-") and anchor.endswith(".launch")
    assert set(namespace) == {"discovery_marker"}  # no id minted at create
    command = captured["command"]
    assert "starting new session s1" in command
    assert anchor in command  # the pane touches exactly the stored marker
    assert "exec codex" in command
    assert "resume" not in command
    db.close()


# -- restart: the discovery-adoption round trip -------------------------------


def test_restart_adopts_a_discovered_rollout_and_persists_the_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adoption crux: a session whose stored anchor has exactly one
    newer matching rollout adopts the codex-minted uuid at the next op,
    resumes it, and the id lands on the row in the namespaced shape with
    the anchor consumed."""
    from agentworks.sessions.manager import restart_session

    events: list[str] = []
    target = _CodexTarget(events, discovered=f"{_ROLLOUT}\n")
    db, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        target=target,
        events=events,
        stored_state={"codex": {"discovery_marker": _ANCHOR}},
    )

    restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    assert f"resume {_SID}" in captured["command"]
    assert "adopted a discovered codex session" in captured["command"]
    # Restart ordering: discovery runs AFTER the kill (the old process is
    # dead before the decision), and the tmux recreate follows.
    assert events.index("kill") < events.index("discover") < events.index("tmux_create")
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"codex": {"session_id": _SID}}  # anchor consumed
    db.close()


def test_restart_resumes_the_stored_id_without_rediscovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The round trip's second half: with the adopted id stored, the next
    restart probes THAT id's rollout and resumes it verbatim; discovery
    does not run again."""
    from agentworks.sessions.manager import restart_session

    events: list[str] = []
    target = _CodexTarget(events, rollout_present=True)
    db, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        target=target,
        events=events,
        stored_state={"codex": {"session_id": _SID}},
    )

    restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    assert f"resume {_SID}" in captured["command"]
    assert "resuming session s1" in captured["command"]
    assert "discover" not in events
    assert events.index("kill") < events.index("detect") < events.index("tmux_create")
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {"codex": {"session_id": _SID}}  # unchanged
    db.close()


def test_restart_with_a_gone_rollout_drops_the_id_and_launches_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned archived policy end to end: a stored id whose rollout is
    gone (archived or deleted) launches fresh with the stale-specific
    decision line, and the persisted blob swaps the stale id for a fresh
    discovery anchor, so the NEXT op rediscovers."""
    from agentworks.sessions.manager import restart_session

    events: list[str] = []
    target = _CodexTarget(events, rollout_present=False)
    db, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        target=target,
        events=events,
        stored_state={"codex": {"session_id": _SID}},
    )

    restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    assert "archived or gone; starting new session s1" in captured["command"]
    refreshed = db.get_session("s1")
    assert refreshed is not None
    namespace = refreshed.harness_integration_state["codex"]
    assert isinstance(namespace, dict)
    assert set(namespace) == {"discovery_marker"}  # stale id gone, anchor stored
    anchor = namespace["discovery_marker"]
    assert isinstance(anchor, str)
    assert anchor in captured["command"]  # the pane touches the stored marker
    db.close()


# -- two stateful harness integrations share one row blob ---------------------


def test_codex_and_claude_code_state_coexist_in_one_blob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first real two-stateful-integration pairing: a row whose blob
    already carries a ``claude-code`` namespace (the template was
    re-pointed to codex) runs a codex op that adopts its own id into the
    ``codex`` namespace, and the claude id is neither read (no inherited
    ``session_id``) nor dropped on persist."""
    from agentworks.sessions.manager import restart_session

    events: list[str] = []
    target = _CodexTarget(events, discovered=f"{_ROLLOUT}\n")
    db, captured = _restart_stubs(
        tmp_path,
        monkeypatch,
        target=target,
        events=events,
        stored_state={
            "claude-code": {"session_id": _CLAUDE_SID},
            "codex": {"discovery_marker": _ANCHOR},
        },
    )

    restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    # codex did NOT inherit the claude id; it adopted its own by discovery.
    assert f"resume {_SID}" in captured["command"]
    refreshed = db.get_session("s1")
    assert refreshed is not None
    assert refreshed.harness_integration_state == {
        "claude-code": {"session_id": _CLAUDE_SID},
        "codex": {"session_id": _SID},
    }
    db.close()


# -- substitution-safety: the generated snippet is not mangled ----------------


def test_substitution_leaves_the_generated_snippet_intact_and_substitutes_extra_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity with the claude-code pin: the relocated template-var
    substitution must not mangle the generated ``sh -c`` skeleton (the
    marker path's ``$HOME`` and quoting included), while an operator
    ``extra_args`` var still substitutes."""
    from agentworks.sessions.manager import create_session

    db = _seed_db(tmp_path)
    events: list[str] = []
    captured: dict[str, str] = {}
    _patch_transport(monkeypatch, _CodexTarget(events))
    _common_stubs(monkeypatch)
    _harness_integration_template(monkeypatch, {"extra_args": ["-p", "profile-{{session_name}}"]})
    _capture_pane_command(monkeypatch, events, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        admin=True,
    )

    command = captured["command"]
    # The generated skeleton survived substitution unmangled, the minted
    # marker path included.
    assert command.startswith("sh -c ")
    assert 'touch "$HOME"/.agentworks/codex/s1-' in command
    assert "exec codex" in command
    # The operator's extra_args var WAS substituted (parity with shell).
    assert "profile-s1" in command
    assert "{{session_name}}" not in command
    db.close()
