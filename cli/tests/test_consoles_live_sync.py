"""Tests for live tmux sync as the console DB mutates a running console.

Split out of `test_consoles.py` (see `.claude/rules/code-style.md` on file-size
targets). Covers add/remove/reorder session live-sync (including the
admin-shell-fixed, stray-window, and duplicate-name edge cases),
`list_consoles_for_session`, the `kill_session_windows` unit tests, and the
integration tests proving `delete_session`/`delete_workspace`/`delete_agent`
dispatch to `kill_session_windows` correctly. Shared seed helpers and stub
Config classes live in `tests/_consoles_support.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import ConnectivityError
from agentworks.sessions.multi_console import (
    add_sessions,
    create_console,
    remove_sessions,
    reorder_sessions,
)
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


def test_add_session_live_sync_skipped_when_console_absent(db: Database, fake_target: _FakeTarget) -> None:
    """If the console's tmux session isn't alive, no new-window command runs."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b"])

    assert not any("new-window" in c for c in fake_target.commands)


def test_add_session_live_sync_adds_window_when_alive(db: Database, fake_target: _FakeTarget) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b+1"])

    new_window = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    assert len(new_window) == 1
    assert "-n b" in new_window[0]
    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:b" in c]
    assert len(splits) == 1


def test_add_session_live_sync_adds_window_for_bare_spec(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Regression: a bare spec (``add-sessions con b`` -- shells=0) on a
    live console must still add the window. The eager-resolve block is
    skipped entirely for bare specs, and the values dict it would have
    produced must not be left undefined for the live-attach path
    (previously an UnboundLocalError swallowed into a live-sync
    warning)."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b"])

    assert not any("live console sync failed" in w for w in captured_output.warnings)
    new_window = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    assert len(new_window) == 1
    assert "-n b" in new_window[0]
    # Bare spec: a window but no shell panes.
    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:b" in c]
    assert splits == []


def test_remove_session_live_sync_kills_window(db: Database, fake_target: _FakeTarget) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    remove_sessions(db, _StubConfig(), console_name="con", session_names=["b"])

    kill_windows = [c for c in fake_target.commands if "kill-window -t aw-console-con:b" in c]
    assert len(kill_windows) == 1


def test_reorder_sessions_live_sync_swaps_windows_no_admin_shell(db: Database, fake_target: _FakeTarget) -> None:
    """With no admin-shell window, the desired session order maps onto
    every live window index. The helper issues one swap-window per
    out-of-place slot, tracking indices in memory so the second iteration
    sees the new layout without another list-windows call."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="0|a\n1|b\n2|c\n")

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["c", "a"])

    # Desired order: [c, a, b]. Starting layout [a, b, c]:
    # - i=0 wants c at idx 0; c is at 2 -> swap 2 <-> 0 -> [c, b, a]
    # - i=1 wants a at idx 1; a is at 2 (after the swap, our tracker knows
    #   this without re-listing) -> swap 2 <-> 1 -> [c, a, b]
    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == [
        "tmux swap-window -s aw-console-con:2 -t aw-console-con:0",
        "tmux swap-window -s aw-console-con:2 -t aw-console-con:1",
    ]


def test_reorder_sessions_live_sync_holds_admin_shell_fixed(db: Database, fake_target: _FakeTarget) -> None:
    """Permutable slots are derived positively from the session set, so the
    --admin-- window (whose name is not in the desired list) is excluded."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    # Build the console with admin_shell=True so the live layout will have
    # '--admin--' at index 0 and sessions at 1+.
    db.insert_console("con", "vm1", admin_shell=True)
    for n in ["a", "b", "c"]:
        db.add_console_session("con", n, [])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="0|--admin--\n1|a\n2|b\n3|c\n"
    )

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["c"])

    # Desired order: [c, a, b]. Session slots = [1, 2, 3] ('--admin--' at
    # 0 is not in the session set, so it isn't a slot). Starting [--admin--,
    # a, b, c]:
    #   - i=0 wants c at idx 1; c is at 3 -> swap 3<->1 -> [..., c, b, a]
    #   - i=1 wants a at idx 2; a is now at 3 -> swap 3<->2 -> [..., c, a, b]
    # The second swap is the unavoidable cost of placing one displaced
    # window: bumping c to the front pushed a out of position.
    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == [
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:1",
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:2",
    ]
    # --admin-- window itself was never moved.
    assert not any("swap-window" in c and "--admin--" in c for c in fake_target.commands)


def test_reorder_sessions_live_sync_ignores_stray_window(db: Database, fake_target: _FakeTarget) -> None:
    """A window with no matching session row (operator-created via raw
    `tmux new-window`, leftover from a rename, etc.) is not a permutable
    slot. The reorder operates only on windows whose names are in the
    session set; the stray stays put."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Operator opened an extra window named 'scratch' at index 2; sessions
    # live at 0, 1, 3.
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="0|a\n1|b\n2|scratch\n3|c\n"
    )

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["c"])

    # Session slots = [0, 1, 3] (scratch at 2 is excluded). Desired order
    # [c, a, b]:
    #   - i=0 wants c at slot 0 (idx 0); c is at 3 -> swap 3<->0 -> [c, b, scratch, a]
    #   - i=1 wants a at slot 1 (idx 1); a is now at 3 -> swap 3<->1 -> [c, a, scratch, b]
    # scratch is never touched; it remains at index 2.
    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == [
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:0",
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:1",
    ]
    assert not any("swap-window" in c and "scratch" in c for c in fake_target.commands)


def test_reorder_sessions_live_sync_skipped_when_console_absent(db: Database, fake_target: _FakeTarget) -> None:
    """If the console's tmux session isn't alive, no swap-window calls run.
    DB still updates."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["b"])

    assert not any("swap-window" in c for c in fake_target.commands)
    # DB still reflects the new order.
    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["b", "a"]


def test_reorder_sessions_live_sync_compacts_when_window_missing(db: Database, fake_target: _FakeTarget) -> None:
    """If the operator killed a session window manually, the surviving
    windows compact toward the front instead of getting stranded at
    later slots. Without this, desired = [c, a, b] with live = [a, b]
    (c missing) would land 'a' at slot 1 and produce [b, a] -- wrong."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # 'c' window is missing -- operator hit Ctrl-B & by mistake, or it
    # exited before the wrapper-loop could catch the restart.
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="0|a\n1|b\n")

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["c"])

    # Desired order: [c, a, b]. present_desired = [a, b] (c filtered out).
    # session_slots = [0, 1]. Map a->0 (already there, skip), b->1 (already
    # there, skip). No swaps needed; layout stays [a, b].
    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == []
    # DB still reflects the new order (DB doesn't care about tmux state).
    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["c", "a", "b"]


def test_reorder_sessions_live_sync_bails_on_duplicate_window_names(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If two windows share a name that's in the session set, we can't
    disambiguate which one to swap. Warn with a --recreate hint and skip
    tmux work; DB is already updated."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Two windows both named 'a' (operator renamed window 2 by accident).
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="0|a\n1|b\n2|a\n")

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["b"])

    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == []
    assert any("duplicate window name" in w and "--recreate" in w for w in captured_output.warnings)
    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["b", "a"]


def test_list_consoles_for_session_returns_members(db: Database) -> None:
    """Snapshot of which consoles list a given session as a member, before
    the FK cascade fires on session delete."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    db.insert_console("alpha", "vm1")
    db.insert_console("beta", "vm1")
    db.insert_console("gamma", "vm1")
    db.add_console_session("alpha", "a", [])
    db.add_console_session("alpha", "b", [])
    db.add_console_session("beta", "a", [])
    # gamma has no members.

    assert [c.name for c in db.list_consoles_for_session("a")] == ["alpha", "beta"]
    assert [c.name for c in db.list_consoles_for_session("b")] == ["alpha"]
    assert db.list_consoles_for_session("nope") == []


def test_kill_session_windows_kills_live_only(db: Database, fake_target: _FakeTarget) -> None:
    """Pairs are grouped by console; kill-window runs only where the console's
    tmux session is alive."""
    from agentworks.sessions.multi_console import kill_session_windows

    fake_target.responses["has-session -t aw-console-alive"] = _FakeResult(returncode=0)
    fake_target.responses["has-session -t aw-console-dead"] = _FakeResult(returncode=1)

    kill_session_windows(
        fake_target,  # type: ignore[arg-type]
        pairs=[("alive", "s"), ("dead", "s")],
    )

    kill_windows = [c for c in fake_target.commands if "kill-window" in c]
    assert kill_windows == ["tmux kill-window -t aw-console-alive:s"]


def test_kill_session_windows_empty_is_noop(
    fake_target: _FakeTarget,
) -> None:
    """No pairs -> no SSH probes, no kill-window calls."""
    from agentworks.sessions.multi_console import kill_session_windows

    kill_session_windows(
        fake_target,  # type: ignore[arg-type]
        pairs=[],
    )
    assert fake_target.commands == []


def test_kill_session_windows_groups_by_console(
    fake_target: _FakeTarget,
) -> None:
    """Multiple sessions in one console -> single has-session probe, one
    kill-window per session."""
    from agentworks.sessions.multi_console import kill_session_windows

    fake_target.responses["has-session -t aw-console-c"] = _FakeResult(returncode=0)

    kill_session_windows(
        fake_target,  # type: ignore[arg-type]
        pairs=[("c", "a"), ("c", "b"), ("c", "d")],
    )

    has_session = [c for c in fake_target.commands if "has-session" in c]
    kill_windows = [c for c in fake_target.commands if "kill-window" in c]
    assert len(has_session) == 1
    assert kill_windows == [
        "tmux kill-window -t aw-console-c:a",
        "tmux kill-window -t aw-console-c:b",
        "tmux kill-window -t aw-console-c:d",
    ]


def test_kill_session_windows_transport_failure_warns(
    fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """A raised non-Agentworks exception (transport surprise) is swallowed
    with a warning that names the affected consoles."""
    from agentworks.sessions.multi_console import kill_session_windows

    def boom(command: str, **kwargs: object) -> _FakeResult:
        raise RuntimeError("ssh blew up")

    fake_target.run = boom  # type: ignore[assignment]

    kill_session_windows(
        fake_target,  # type: ignore[arg-type]
        pairs=[("alpha", "s"), ("beta", "s")],
    )

    assert any(
        "live console window cleanup failed" in w and "alpha" in w and "beta" in w for w in captured_output.warnings
    )


def test_kill_session_windows_agentworks_error_propagates(
    fake_target: _FakeTarget,
) -> None:
    """AgentworksError is not swallowed by the helper -- callers see it."""
    from agentworks.sessions.multi_console import kill_session_windows

    def boom(command: str, **kwargs: object) -> _FakeResult:
        raise ConnectivityError("vm unreachable", entity_kind="vm", entity_name="vm1")

    fake_target.run = boom  # type: ignore[assignment]

    with pytest.raises(ConnectivityError):
        kill_session_windows(
            fake_target,  # type: ignore[arg-type]
            pairs=[("alpha", "s")],
        )


# -- Integration: delete paths invoke kill_session_windows correctly -------


def test_delete_session_kills_console_windows(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """``manager.delete_session`` must snapshot console memberships *before*
    the DB delete (FK cascade clears the join) and then dispatch to
    ``kill_session_windows`` with one pair per member console."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s", "other"])
    # Mark 's' STOPPED so check_session_status short-circuits with no SSH.
    db.update_session_pid("s", PID_STOPPED)
    create_console(db, name="alpha", vm_name="vm1", session_specs=["s", "other"])
    create_console(db, name="beta", vm_name="vm1", session_specs=["s"])
    create_console(db, name="gamma", vm_name="vm1", session_specs=["other"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)

    captured: list[list[tuple[str, str]]] = []

    def spy(target: object, *, pairs: list[tuple[str, str]]) -> None:
        captured.append(pairs)

    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", spy)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=True)

    # The DB row is gone, and only the consoles that listed 's' get kills.
    assert db.get_session("s") is None
    assert len(captured) == 1
    assert sorted(captured[0]) == [("alpha", "s"), ("beta", "s")]


def test_delete_session_skips_kill_when_no_member_consoles(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """No console membership -> no kill_session_windows call. Guards against
    a future regression that unconditionally invokes the helper."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["lonely"])
    db.update_session_pid("lonely", PID_STOPPED)

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)

    called = False

    def spy(target: object, *, pairs: list[tuple[str, str]]) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", spy)

    manager_mod.delete_session(db, _StubConfig(), name="lonely", yes=True)

    assert called is False


def test_delete_workspace_kills_console_windows(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    tmp_path: Path,
) -> None:
    """``delete_workspace --force`` must clean up windows for every deleted
    session across every console that listed them."""
    from agentworks.db import PID_STOPPED
    from agentworks.workspaces import manager as ws_manager

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s1", "s2"])
    db.update_session_pid("s1", PID_STOPPED)
    db.update_session_pid("s2", PID_STOPPED)
    create_console(db, name="con1", vm_name="vm1", session_specs=["s1", "s2"])
    create_console(db, name="con2", vm_name="vm1", session_specs=["s1"])

    # delete_workspace shells out to delete_vm_workspace + tmuxinator regen;
    # stub both so we don't need a live VM filesystem.
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.delete_vm_workspace",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agentworks.agents.grants.revoke_workspace_grants",
        lambda *a, **k: None,
    )

    captured: list[list[tuple[str, str]]] = []

    def spy(target: object, *, pairs: list[tuple[str, str]]) -> None:
        captured.append(pairs)

    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", spy)

    # delete_workspace touches config.paths.vscode_workspaces to remove the
    # .code-workspace file; point it at a tmp dir so the unlink is a no-op.
    cfg = _StubConfig()
    cfg.paths = type("P", (), {"vscode_workspaces": tmp_path})()  # type: ignore[attr-defined]
    ws_manager.delete_workspace(db, cfg, "ws-vm1", force=True, yes=True)

    assert db.get_workspace("ws-vm1") is None
    assert db.get_session("s1") is None
    assert db.get_session("s2") is None
    assert len(captured) == 1
    assert sorted(captured[0]) == [
        ("con1", "s1"),
        ("con1", "s2"),
        ("con2", "s1"),
    ]


def test_delete_agent_kills_console_windows(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """``delete_agent --force`` runs the same cleanup over its agent's
    sessions."""
    from agentworks.agents import manager as agent_manager
    from agentworks.db import PID_STOPPED

    _seed_vm(db, with_tailscale=True)
    db._conn.execute("INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')")
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path, pid) "
        "VALUES ('s1', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s1.sock', ?), "
        "('s2', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s2.sock', ?)",
        (PID_STOPPED, PID_STOPPED),
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s1", "s2"])

    monkeypatch.setattr(
        "agentworks.agents.grants.remove_from_workspace_group",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agentworks.agents.initializer.delete_agent_on_vm",
        lambda *a, **k: None,
    )
    # delete_agent now refreshes operator SSH config; stub it out since the
    # test's _StubConfig lacks the operator.* attributes the real path needs.
    monkeypatch.setattr(
        "agentworks.ssh_config.sync_ssh_config",
        lambda *a, **k: None,
    )

    captured: list[list[tuple[str, str]]] = []

    def spy(target: object, *, pairs: list[tuple[str, str]]) -> None:
        captured.append(pairs)

    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", spy)

    agent_manager.delete_agent(db, _StubConfig(), name="bot", force=True, yes=True)

    assert db.get_agent("bot") is None
    assert len(captured) == 1
    assert sorted(captured[0]) == [("con", "s1"), ("con", "s2")]
