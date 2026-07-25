"""Tests for ``agw console restore-session`` -- argument and live-state
validation, strict failure paths, and happy paths. Carved out of
test_consoles.py to keep that file under the project's file-length
guidance; shared seed helpers / stub Config classes / the autouse
Registry-stub fixture now live in ``tests/_consoles_support.py``."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import ExternalError, NotFoundError, StateError
from agentworks.sessions.multi_console import (
    create_console,
    restore_session,
)
from agentworks.sessions.multi_console_layout import SHELL_INDEX_OPTION
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


# -- restore-session: argument and live-state validation -------------------


def test_restore_session_errors_when_console_missing(db: Database) -> None:
    """restore-session refuses unknown console name with NotFoundError."""
    _seed_vm(db, with_tailscale=False)
    with pytest.raises(NotFoundError, match="console 'nope' not found"):
        restore_session(db, _StubConfig(), console_name="nope", session_name="a")


def test_restore_session_errors_when_session_not_member(db: Database, fake_target: _FakeTarget) -> None:
    """Session must already be a member of the console; restore-session is
    purely additive against the configured list, not a way to add sessions."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    with pytest.raises(NotFoundError, match="is not a member of console"):
        restore_session(db, _StubConfig(), console_name="con", session_name="b")


def test_restore_session_errors_when_tmux_not_running(db: Database, fake_target: _FakeTarget) -> None:
    """restore-session only repairs a live console; if tmux isn't running it
    instructs the user to attach (which builds the console from scratch)."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    # has-session returns nonzero (default _FakeResult is ok, so override).
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    with pytest.raises(StateError, match="has no live tmux session"):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")


# -- restore-session: strict failure paths ---------------------------------


def test_restore_session_strict_on_untagged_pane(db: Database, fake_target: _FakeTarget) -> None:
    """A window with shell panes lacking the @agentworks-shell-index tag
    cannot be reasoned about; restore-session refuses and points at
    `attach --recreate` to rebuild from scratch."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Two shell panes (pidx 1, 2), neither tagged.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|\n%3|2|\n")

    with pytest.raises(StateError, match="no agentworks tag"):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")


def test_restore_session_strict_on_out_of_range_tag(db: Database, fake_target: _FakeTarget) -> None:
    """A pane tagged with a config index past the current configured range
    (e.g., config shrank or DB was edited) is unsafe to repair; restore-session
    surfaces the inconsistency and points at `--recreate`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    # Two configured shells (valid indices: 0, 1).
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Three live shell panes tagged 0, 1, 2; tag 2 is out-of-range.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|1\n%4|3|2\n")

    with pytest.raises(
        StateError,
        match=r"tags \[2\] point past the configured range",
    ):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")


def test_restore_session_strict_on_duplicate_tags(db: Database, fake_target: _FakeTarget) -> None:
    """Two panes claiming the same config index can't both be the canonical
    pane for that shell; surface the inconsistency rather than guessing."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Two live shell panes both tagged 0 (a duplicate).
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|0\n")

    with pytest.raises(StateError, match=r"duplicate tags \[0\]"):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")


def test_restore_session_strict_message_when_configured_zero(db: Database, fake_target: _FakeTarget) -> None:
    """A session with zero configured shells can still have live shell panes
    (e.g. operator ran `tmux split-window` manually then tagged via DB edit).
    The out-of-range error message must not render the empty range as
    `(0..-1)`; instead say the session has no configured shells."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    # Session 'a' with zero shells (no `+N`).
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane + one tagged shell pane (config index 0, but config has 0 shells).
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n")

    with pytest.raises(StateError, match="no configured shells") as excinfo:
        restore_session(db, _StubConfig(), console_name="con", session_name="a")
    assert "0..-1" not in str(excinfo.value)


def test_restore_session_noop_when_live_matches_config(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Live == configured: no tmux splits or swaps are issued."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|1\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a")

    assert not any("split-window" in c for c in fake_target.commands)
    assert not any("swap-pane" in c for c in fake_target.commands)
    assert any("already matches config" in m for m in captured_output.info)
    # Post-restore landing focus on the session pane is the same regardless
    # of whether repairs were needed.
    assert "tmux select-pane -t aw-console-con:a.0" in fake_target.commands


# -- restore-session: window-missing rebuild -------------------------------


def test_restore_session_rebuilds_missing_window(db: Database, fake_target: _FakeTarget) -> None:
    """If the session's window is absent from live tmux, restore-session
    rebuilds it via the standard _add_session_window path."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # No 'a' in the listed windows; only a placeholder name.
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="other\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a")

    new_windows = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    assert len(new_windows) == 1


def test_restore_session_rebuild_raises_when_new_window_fails(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """_add_session_window warns and skips rather than raising when
    `tmux new-window` fails, so restore-session must check that the window was
    actually built before claiming it rebuilt one."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="other\n")
    fake_target.responses["new-window -t aw-console-con"] = _FakeResult(returncode=1, stderr="no space for window")

    with pytest.raises(ExternalError, match="failed to rebuild window 'a'"):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")

    # RESULT lines mirror into .info; the success line must not be among them.
    assert not any("Rebuilt window" in m for m in captured_output.info)


def test_restore_session_rebuild_refuses_when_session_row_gone(db: Database, fake_target: _FakeTarget) -> None:
    """The window-missing path rebuilds from config, but _add_session_window
    only warns and skips when the session row is gone. Check the session up
    front so the operator gets the specific reason rather than a generic
    rebuild failure."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])

    # Corrupt state: the session row is gone but the console member survives.
    # ON DELETE CASCADE would normally remove the member with the session, so
    # disable FKs to reproduce the orphaned-member state the guard defends against.
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM sessions WHERE name = 'a'")
    db._conn.commit()

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Window 'a' is absent, so this run would take the rebuild path.
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="other\n")

    fake_target.commands.clear()
    with pytest.raises(StateError, match="no longer exists in the database"):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")

    assert not any("new-window" in c for c in fake_target.commands)


# -- restore-session: refuses rather than destroying live state ------------


def test_restore_session_refuses_when_session_pane_killed(db: Database, fake_target: _FakeTarget) -> None:
    """If the operator kills the session-attach pane, tmux renumbers a tagged
    shell down into its slot, so the lowest-indexed pane carries a shell tag
    instead of being untagged. Repairing that means recreating the window,
    which would destroy the operator's live shell panes (and, for a
    single-member console, the console itself), so restore-session refuses and
    points at `attach --recreate`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane gone: the one configured shell (tag 0) has moved into slot 0,
    # so the lowest-indexed pane is tagged.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|0\n")

    fake_target.commands.clear()
    with pytest.raises(StateError, match="lost its session-attach pane") as excinfo:
        restore_session(db, _StubConfig(), console_name="con", session_name="a")

    assert "agw console attach con --recreate" in (excinfo.value.hint or "")
    # Read-only probes only: the window and its live panes are left exactly as
    # they were, so the operator decides whether to recreate.
    assert fake_target.commands == [
        "tmux has-session -t aw-console-con 2>/dev/null",
        "tmux list-windows -t aw-console-con -F '#{window_name}'",
        "tmux list-panes -t aw-console-con:a -F '#{pane_id}|#{pane_index}|#{@agentworks-shell-index}'",
    ]


def test_restore_session_refuses_when_session_pane_killed_leaves_duplicate(
    db: Database, fake_target: _FakeTarget
) -> None:
    """The stuck state seen in the wild: the session pane was killed and a prior
    restore-session (which skipped pane_index 0 unconditionally) added a
    duplicate shell, so both live panes are tagged 0. The lowest-indexed pane is
    still tagged, so this is reported as the missing session pane it is, not as
    "already matches config" and not as a duplicate-tags corruption."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Two panes, both tagged 0 (no untagged session pane): the observed bug.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|0\n%2|1|0\n")

    with pytest.raises(StateError, match="lost its session-attach pane"):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")


@pytest.mark.parametrize(
    ("session_spec", "windows", "panes"),
    [
        pytest.param("a+1", "other\n", "", id="window-missing"),
        pytest.param("a+1", "a\n", "%1|0|\n%2|1|0\n", id="healthy"),
        pytest.param("a+2", "a\n", "%1|0|\n%2|1|0\n", id="shell-pane-missing"),
        pytest.param("a+1", "a\n", "%1|0|0\n", id="session-pane-killed"),
        pytest.param("a+1", "a\n", "%1|0|0\n%2|1|0\n", id="session-pane-killed-duplicate"),
        pytest.param("a+2", "a\n", "%1|0|\n%2|1|\n%3|2|\n", id="untagged-shells"),
        pytest.param("a+2", "a\n", "%1|0|\n%2|1|0\n%3|2|0\n", id="duplicate-tags"),
        pytest.param("a+1", "a\n", "%1|0|\n%2|1|0\n%3|2|1\n", id="out-of-range-tag"),
        pytest.param("a+1", "a\n", "", id="unparseable-pane-list"),
    ],
)
def test_restore_session_never_destroys_live_tmux_state(
    db: Database,
    fake_target: _FakeTarget,
    session_spec: str,
    windows: str,
    panes: str,
) -> None:
    """restore-session is additive on every path: whatever it finds live, it
    never issues a destructive tmux command. A window it killed to rebuild
    would take the operator's running shells with it, and for a single-member
    console tmux would destroy the whole console session along with its last
    window. Pins the property the command's contract rests on."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=[session_spec])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout=windows)
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout=panes)
    # Any pane the repair paths do open comes back with a pane id to tag.
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(stdout="%9\n")

    fake_target.commands.clear()
    # Every corrupt state raises; the point here is what was NOT run, so accept
    # either outcome and assert on the commands.
    with contextlib.suppress(StateError, ExternalError):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")

    destructive = [c for c in fake_target.commands if "kill-window" in c or "kill-pane" in c or "kill-session" in c]
    assert destructive == []


# -- restore-session: pane-base-index ---------------------------------------


def test_restore_session_healthy_under_pane_base_index_one(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """The console tmux server inherits the admin user's ~/.tmux.conf, so
    `set -g pane-base-index 1` makes the session-attach pane report index 1 and
    no pane ever report index 0. The session pane is the lowest-indexed pane,
    not literally pane 0, so this window is healthy."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane at index 1 (untagged), shells at 2 and 3.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|1|\n%2|2|0\n%3|3|1\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a")

    assert any("already matches config" in m for m in captured_output.info)
    assert not any("split-window" in c for c in fake_target.commands)


def test_restore_session_refuses_killed_session_pane_under_pane_base_index_one(
    db: Database, fake_target: _FakeTarget
) -> None:
    """The mirror of the healthy case: under `pane-base-index 1` a killed
    session pane leaves a tagged shell as the lowest-indexed pane, and that is
    still the refusal."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane killed: the shells renumbered down to 1 and 2.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%2|1|0\n%3|2|1\n")

    with pytest.raises(StateError, match=r"pane 1 is a shell pane"):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")


def test_restore_session_reorders_relative_to_the_session_pane(db: Database, fake_target: _FakeTarget) -> None:
    """Shell config index N belongs one slot after the session pane plus N, so
    under `pane-base-index 1` (session pane at index 1) config index 1 belongs
    at pane 3, not pane 2."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane at index 1; config index 0 is missing, so the surviving
    # shell (config index 1) sits at index 2 and has to move up to index 3.
    # The fake replays this same listing to the reorder pass, which is enough
    # to pin which slot a tagged pane is aimed at.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|1|\n%2|2|1\n")
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(stdout="%9\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a")

    swaps = [c for c in fake_target.commands if "swap-pane" in c]
    assert swaps == ["tmux swap-pane -s %2 -t aw-console-con:a.3"]


# -- restore-session: additive shell-pane repair ---------------------------


def test_restore_session_raises_when_split_returns_no_pane_id(db: Database, fake_target: _FakeTarget) -> None:
    """If tmux split-window succeeds but doesn't print a pane id, the pane
    is created but untagged. restore-session must surface this as an error
    so the operator doesn't see exit-0 while a window is left incomplete."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+3"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|2\n")
    # split-window succeeds but returns no pane id; _split_shell_pane warns
    # and returns None, which restore_session must escalate.
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(stdout="")

    with pytest.raises(ExternalError, match=r"failed to create/tag config indices \[1\]"):
        restore_session(db, _StubConfig(), console_name="con", session_name="a")


def test_restore_session_splits_missing_config_indices_and_tags_them(db: Database, fake_target: _FakeTarget) -> None:
    """Live < configured: restore-session identifies missing config indices
    by tag diff, splits each one back in with the correct tag, and applies
    select-layout to redistribute geometry."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    # Three shells configured; index 1 ("fish") is missing live.
    create_console(db, name="con", vm_name="vm1", session_specs=["a+3"])

    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(stdout="a\n")
    # Live: session pane (pidx 0), tagged shells for indices 0 and 2; 1 is gone.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|2\n")
    # split-window returns a fresh pane id so the tag step has a target.
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(stdout="%9\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a")

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:a" in c]
    assert len(splits) == 1
    set_options = [c for c in fake_target.commands if "set-option -p" in c and SHELL_INDEX_OPTION in c]
    # The new pane gets tagged with config index 1 (the missing one).
    assert any(f"-t %9 {SHELL_INDEX_OPTION} 1" in c for c in set_options)
    layouts = [c for c in fake_target.commands if "select-layout -t aw-console-con:a tiled" in c]
    assert len(layouts) == 1
