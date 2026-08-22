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
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.multi_console import (
    add_sessions,
    create_console,
    remove_sessions,
    reorder_sessions,
)
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput

# tmux session name for console "con"; see tmux_session_name.
CON = "aw-console-con"


def test_add_session_live_sync_skipped_when_console_absent(db: Database, fake_target: _FakeTarget) -> None:
    """If the console's tmux session isn't alive, no new-window command runs."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b"], interaction=TtyInteractionPolicy.REFUSE)

    assert not any("new-window" in c for c in fake_target.commands)


def test_add_session_live_sync_adds_window_when_alive(db: Database, fake_target: _FakeTarget) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b+1"], interaction=TtyInteractionPolicy.REFUSE)

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
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b"], interaction=TtyInteractionPolicy.REFUSE)

    assert not any("live console sync failed" in w for w in captured_output.warnings)
    new_window = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    assert len(new_window) == 1
    assert "-n b" in new_window[0]
    # Bare spec: a window but no shell panes.
    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:b" in c]
    assert splits == []


def test_add_sessions_appends_new_window_last_under_renumber_off(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
) -> None:
    """Regression for issue #246 (b): with the placeholder retired (index 0
    free) under the stock ``renumber-windows off``, add-sessions must land the
    new window LAST, not in the reclaimed low slot, so live window order matches
    DB order.

    Driven against the stateful tmux model so the reclaimed-slot physics is
    real: a bare ``new-window`` fills index 0, while the fix targets an explicit
    index past the last window and then reorders to config order."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    # Reproduce the post-attach live layout with the placeholder retired: build
    # placeholder(0), a(1), b(2), then kill the placeholder so index 0 is free
    # (renumber-windows off leaves the gap rather than compacting).
    model = TmuxModel()
    model.new_session(CON, "_PLACEHOLDER")  # index 0
    model.new_window(CON, "a")  # index 1
    model.new_window(CON, "b")  # index 2
    assert model.kill_window(CON, "_PLACEHOLDER")  # free index 0
    assert model.windows_with_index(CON) == [(1, "a"), (2, "b")]
    target = console_target_factory(model)

    add_sessions(db, _StubConfig(), console_name="con", session_specs=["c"], interaction=TtyInteractionPolicy.REFUSE)

    # The new window landed at the tail (index 3), not the reclaimed slot 0.
    assert model.windows_with_index(CON) == [(1, "a"), (2, "b"), (3, "c")]
    # Live window order matches DB order.
    live_order = [name for _idx, name in model.windows_with_index(CON)]
    db_order = [m.session_name for m in db.list_console_sessions("con")]
    assert live_order == db_order == ["a", "b", "c"]
    # The append targeted an explicit index one past the last window.
    assert any("new-window -t aw-console-con:3 " in cmd for cmd in target.commands)


def test_add_sessions_appends_multiple_new_windows_in_order_at_the_tail(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
) -> None:
    """Regression for issue #246 (b), multi-add: adding two or more sessions at
    once must land every new window in order at the tail, not in reclaimed low
    slots. Each `_add_session_window` re-probes `_last_window_index`, so member
    N targets one past member N-1's freshly created window; this pins that
    per-iteration incremental targeting under the stock `renumber-windows off`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c", "d"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    # Post-attach layout with the placeholder retired: a(1), b(2), index 0 free.
    model = TmuxModel()
    model.new_session(CON, "_PLACEHOLDER")  # index 0
    model.new_window(CON, "a")  # index 1
    model.new_window(CON, "b")  # index 2
    assert model.kill_window(CON, "_PLACEHOLDER")  # free index 0
    assert model.windows_with_index(CON) == [(1, "a"), (2, "b")]
    # Installs the model-backed transport seam (side effect); no handle needed.
    console_target_factory(model)

    add_sessions(
        db, _StubConfig(), console_name="con", session_specs=["c", "d"], interaction=TtyInteractionPolicy.REFUSE
    )

    # Both new windows landed at the tail in argument order (3, 4), not in the
    # reclaimed slot 0, and live order matches DB order.
    assert model.windows_with_index(CON) == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]
    live_order = [name for _idx, name in model.windows_with_index(CON)]
    db_order = [m.session_name for m in db.list_console_sessions("con")]
    assert live_order == db_order == ["a", "b", "c", "d"]


def test_add_sessions_reorders_to_config_order_when_new_window_lands_low(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
) -> None:
    """Even if the append lands the new window in a low slot (e.g. the reorder
    is the only thing that can save order because a prior op left drift), the
    final reorder pass settles live order to config order. Here the console is
    seeded already out of order (b before a) with the placeholder gap; after
    add-sessions of c, live order must match DB order [a, b, c]."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    # Live tmux is out of order relative to the DB: b at the low slot, a above.
    model = TmuxModel()
    model.new_session(CON, "b")  # index 0
    model.new_window(CON, "a")  # index 1
    assert model.windows_with_index(CON) == [(0, "b"), (1, "a")]
    # Installs the model-backed transport seam (side effect); no handle needed.
    console_target_factory(model)

    add_sessions(db, _StubConfig(), console_name="con", session_specs=["c"], interaction=TtyInteractionPolicy.REFUSE)

    # The reorder pass settles everything to DB order regardless of where the
    # append or the pre-existing windows sat.
    live_order = [name for _idx, name in model.windows_with_index(CON)]
    db_order = [m.session_name for m in db.list_console_sessions("con")]
    assert live_order == db_order == ["a", "b", "c"]


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

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=True, interaction=TtyInteractionPolicy.REFUSE)

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

    manager_mod.delete_session(db, _StubConfig(), name="lonely", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert called is False


# -- Issue #248: operator signal for the session -> console cascade ----------


def test_delete_session_reports_affected_consoles(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Deleting a session names every console that referenced it, so the
    cascade is no longer silent (issue #248)."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s", "other"])
    db.update_session_pid("s", PID_STOPPED)
    # alpha keeps 'other' after the cascade; beta is emptied.
    create_console(db, name="alpha", vm_name="vm1", session_specs=["s", "other"])
    create_console(db, name="beta", vm_name="vm1", session_specs=["s"])
    create_console(db, name="gamma", vm_name="vm1", session_specs=["other"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", lambda *a, **k: None)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    # The affected consoles (alpha, beta) are named in c.name order; the
    # unrelated console (gamma) is not. Two consoles -> plural noun.
    report = [m for m in captured_output.info if m.startswith("Removed 's' from console")]
    assert report == ["Removed 's' from consoles: alpha, beta"]


def test_delete_session_offers_and_deletes_now_empty_console(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A console emptied by the cascade is offered for deletion; an accepted
    offer deletes it, while a console that still has members survives."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s", "other"])
    db.update_session_pid("s", PID_STOPPED)
    create_console(db, name="alpha", vm_name="vm1", session_specs=["s", "other"])
    create_console(db, name="beta", vm_name="vm1", session_specs=["s"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", lambda *a, **k: None)

    # Route the two interactive confirms (the session delete itself, then the
    # empty-console offer): accept both. Recording the messages lets us prove
    # the empty-console offer was actually presented.
    prompts: list[str] = []

    def _confirm(message: str, default: bool = False) -> bool:
        prompts.append(message)
        return True

    # The empty-console offer only prompts in an interactive context; declare
    # one so this exercises the offer path rather than report-but-keep.
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=False, interaction=TtyInteractionPolicy.REFUSE)

    assert any("beta" in p and "no configured sessions left" in p for p in prompts)
    # Accepted offer: the emptied console is gone; the still-populated one stays.
    assert db.get_console("beta") is None
    assert db.get_console("alpha") is not None
    assert [m.session_name for m in db.list_console_sessions("alpha")] == ["other"]


def test_delete_session_declined_offer_keeps_empty_console(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Declining the empty-console offer leaves the console in place (empty)."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s"])
    db.update_session_pid("s", PID_STOPPED)
    create_console(db, name="beta", vm_name="vm1", session_specs=["s"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", lambda *a, **k: None)

    # Accept only the session-delete confirm; decline the empty-console offer
    # (the behavior under test) and the now-empty-workspace offer (issue #266,
    # out of scope here: deleting 's' empties ws-vm1).
    def _confirm(message: str, default: bool = False) -> bool:
        return message.startswith("Delete session")

    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=False, interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_session("s") is None
    assert db.get_console("beta") is not None
    assert db.list_console_sessions("beta") == []


def test_delete_session_does_not_offer_console_with_remaining_sessions(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A console that still has other members after the cascade is neither
    offered for deletion nor removed."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s", "other"])
    db.update_session_pid("s", PID_STOPPED)
    create_console(db, name="alpha", vm_name="vm1", session_specs=["s", "other"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", lambda *a, **k: None)

    prompts: list[str] = []

    def _confirm(message: str, default: bool = False) -> bool:
        prompts.append(message)
        return True

    # Interactive, so a truly empty console WOULD be offered; proving no offer
    # appears here shows it is the remaining member (not non-interactivity) that
    # suppresses it.
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=False, interaction=TtyInteractionPolicy.REFUSE)

    # No empty-console offer was presented, and alpha still holds 'other'.
    assert not any("no configured sessions left" in p for p in prompts)
    assert db.get_console("alpha") is not None
    assert [m.session_name for m in db.list_console_sessions("alpha")] == ["other"]


def test_delete_session_leaves_no_dangling_console_reference(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """The FK cascade still holds: no console lists the deleted session after
    ``session delete`` (the guarantee this change must not regress)."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s", "other"])
    db.update_session_pid("s", PID_STOPPED)
    create_console(db, name="alpha", vm_name="vm1", session_specs=["s", "other"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", lambda *a, **k: None)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    assert db.list_consoles_for_session("s") == []
    dangling = db._conn.execute("SELECT COUNT(*) FROM console_sessions WHERE session_name = 's'").fetchone()[0]
    assert dangling == 0


def test_delete_session_yes_reports_but_keeps_empty_console(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Under --yes, a now-empty console is reported (a warning naming the
    console-delete command) but NOT auto-deleted: unlike a
    session-created workspace/agent, a console is an operator-authored view
    this session never owned."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s"])
    db.update_session_pid("s", PID_STOPPED)
    create_console(db, name="beta", vm_name="vm1", session_specs=["s"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", lambda *a, **k: None)

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=True, interaction=TtyInteractionPolicy.REFUSE)

    # A single affected console uses the singular noun.
    assert "Removed 's' from console: beta" in captured_output.info
    # Left in place but empty, with a warning that points at the manual delete.
    assert db.get_console("beta") is not None
    assert db.list_console_sessions("beta") == []
    assert any(
        "beta" in w and "no configured sessions" in w and "agw console delete beta" in w
        for w in captured_output.warnings
    )


def test_delete_session_warns_when_offered_console_delete_raises(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """If the confirmed empty-console delete raises an AgentworksError (e.g. VM
    unreachable in its teardown), the session is still reported deleted, a
    warning names the console, and the command completes without propagating."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s"])
    db.update_session_pid("s", PID_STOPPED)
    create_console(db, name="beta", vm_name="vm1", session_specs=["s"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.sessions.multi_console.kill_session_windows", lambda *a, **k: None)

    def _boom(*a: object, **k: object) -> None:
        raise ConnectivityError("vm unreachable", entity_kind="vm", entity_name="vm1")

    monkeypatch.setattr("agentworks.sessions.multi_console.delete_console", _boom)

    # Accept the session delete and the empty-console offer (whose delete is
    # stubbed to raise); decline the now-empty-workspace offer (issue #266,
    # out of scope here: deleting 's' empties ws-vm1).
    def _confirm(message: str, default: bool = False) -> bool:
        return "now has no sessions" not in message

    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    # The AgentworksError from the console teardown is swallowed with a warning;
    # it must not propagate out of delete_session.
    manager_mod.delete_session(db, _StubConfig(), name="s", yes=False, interaction=TtyInteractionPolicy.REFUSE)

    assert db.get_session("s") is None
    assert "Session 's' deleted" in captured_output.info
    assert any(
        "Could not delete empty console 'beta'" in w and "agw console delete beta" in w
        for w in captured_output.warnings
    )


# -- Issue #265: remove-sessions offers to delete a now-empty console --------
#
# The empty-console offer/report path is the SAME shared helper the
# session-delete cascade above exercises (#248/#261); these tests prove
# ``remove_sessions`` wires into it. The VM is seeded without Tailscale so
# ``_live_target`` returns None and the offer runs with no SSH work; that is
# also the repro's shape (a console emptied on a non-live VM). The offer only
# prompts in an interactive context, so tests exercising the prompt declare
# ``is_interactive`` (the report-but-keep path when non-interactive is covered
# by its own test below).


def test_remove_sessions_empties_console_offers_and_deletes(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Removing the last members offers to delete the emptied console; an
    accepted offer deletes it. The removed sessions themselves survive."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    prompts: list[str] = []

    def _confirm(message: str, default: bool = False) -> bool:
        prompts.append(message)
        return True

    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a", "b"])

    assert any("con" in p and "no configured sessions left" in p for p in prompts)
    assert db.get_console("con") is None
    # Only the console membership was removed; the sessions are untouched.
    assert db.get_session("a") is not None
    assert db.get_session("b") is not None


def test_remove_sessions_declined_offer_keeps_empty_console(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Declining the offer leaves the console in place, empty."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", lambda *a, **k: False)

    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])

    assert db.get_console("con") is not None
    assert db.list_console_sessions("con") == []


def test_remove_sessions_yes_reports_but_keeps_empty_console(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Under --yes the emptied console is reported (a warning naming the
    manual delete command) but NOT auto-deleted, and no prompt is presented."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    def _confirm(message: str, default: bool = False) -> bool:
        raise AssertionError("no prompt should be presented under --yes")

    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"], yes=True)

    assert db.get_console("con") is not None
    assert db.list_console_sessions("con") == []
    assert any(
        "con" in w and "no configured sessions" in w and "agw console delete con" in w for w in captured_output.warnings
    )


def test_remove_sessions_with_remaining_not_offered(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Removing only some members leaves the console non-empty, so no offer is
    presented and the console (with its remaining member) is untouched."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    prompts: list[str] = []

    def _confirm(message: str, default: bool = False) -> bool:
        prompts.append(message)
        return True

    # Interactive, so a truly empty console WOULD be offered; proving no offer
    # appears here shows it is the remaining member that suppresses it.
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])

    assert not any("no configured sessions left" in p for p in prompts)
    assert db.get_console("con") is not None
    assert [m.session_name for m in db.list_console_sessions("con")] == ["b"]


def test_remove_sessions_warns_when_offered_console_delete_raises(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """If the confirmed empty-console delete raises an AgentworksError, the
    remove-sessions operation is still reported done, a warning names the
    console, and the error does not propagate."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    def _boom(*a: object, **k: object) -> None:
        raise ConnectivityError("vm unreachable", entity_kind="vm", entity_name="vm1")

    monkeypatch.setattr("agentworks.sessions.multi_console.delete_console", _boom)
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.confirm", lambda *a, **k: True)

    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])

    assert "Removed 1 session(s) from console 'con'." in captured_output.info
    assert any(
        "Could not delete empty console 'con'" in w and "agw console delete con" in w for w in captured_output.warnings
    )


def test_remove_sessions_non_interactive_without_yes_reports_but_keeps(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Regression guard: a scripted ``console remove-sessions`` (no TTY) without
    ``--yes`` that empties a console must NOT prompt (which would EOF into a
    UserAbort after the removal already committed). It reports-but-keeps and the
    call returns normally, so the command exits 0 as it did before issue #265."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    # Non-interactive: the offer must never reach the prompt.
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: False)

    def _confirm(message: str, default: bool = False) -> bool:
        raise AssertionError("must not prompt in a non-interactive context")

    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    # No exception (no UserAbort): the call completes normally.
    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])

    assert db.get_console("con") is not None
    assert db.list_console_sessions("con") == []
    assert any(
        "con" in w and "no configured sessions" in w and "agw console delete con" in w for w in captured_output.warnings
    )


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
    ws_manager.delete_workspace(db, cfg, "ws-vm1", force=True, yes=True, interaction=TtyInteractionPolicy.REFUSE)

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

    agent_manager.delete_agent(
        db, _StubConfig(), name="bot", force=True, yes=True, interaction=TtyInteractionPolicy.REFUSE
    )

    assert db.get_agent("bot") is None
    assert len(captured) == 1
    assert sorted(captured[0]) == [("con", "s1"), ("con", "s2")]
