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
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.multi_console import restore_session
from agentworks.sessions.multi_console_layout import SHELL_INDEX_OPTION
from tests._consoles_support import (  # noqa: F401
    _seed_sessions,
    _seed_vm,
    _stub_build_registry,
    _StubConfig,
    _StubVerticalLayoutConfig,
)
from tests._tmux_model import TmuxModel
from tests.conftest import _FakeResult, _FakeTarget
from tests.console_helpers import create_console_record as create_console

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput

CON = "aw-console-con"


# -- restore-session: argument and live-state validation -------------------


def test_restore_session_errors_when_console_missing(db: Database) -> None:
    """restore-session refuses unknown console name with NotFoundError."""
    _seed_vm(db, with_tailscale=False)
    with pytest.raises(NotFoundError, match="console 'nope' not found"):
        restore_session(
            db, _StubConfig(), console_name="nope", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )


def test_restore_session_errors_when_session_not_member(db: Database, fake_target: _FakeTarget) -> None:
    """Session must already be a member of the console; restore-session is
    purely additive against the configured list, not a way to add sessions."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    with pytest.raises(NotFoundError, match="is not a member of console"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="b", interaction=TtyInteractionPolicy.REFUSE
        )


def test_restore_session_errors_when_tmux_not_running(db: Database, fake_target: _FakeTarget) -> None:
    """restore-session only repairs a live console; if tmux isn't running it
    instructs the user to attach (which builds the console from scratch)."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    # has-session returns nonzero (default _FakeResult is ok, so override).
    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=1)
    with pytest.raises(StateError, match="has no live tmux session"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )


# -- restore-session: strict failure paths ---------------------------------


def test_restore_session_strict_on_untagged_pane(db: Database, fake_target: _FakeTarget) -> None:
    """A window with shell panes lacking the @agentworks-shell-index tag
    cannot be reasoned about; restore-session refuses and points at
    `console restart` to rebuild from scratch."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Two shell panes (pidx 1, 2), neither tagged.
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|\n%3|2|\n")

    with pytest.raises(StateError, match="no agentworks tag"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )


def test_restore_session_strict_on_out_of_range_tag(db: Database, fake_target: _FakeTarget) -> None:
    """A pane tagged with a config index past the current configured range
    (e.g., config shrank or DB was edited) is unsafe to repair; restore-session
    surfaces the inconsistency and points at `restart`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    # Two configured shells (valid indices: 0, 1).
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Three live shell panes tagged 0, 1, 2; tag 2 is out-of-range.
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|1\n%4|3|2\n")

    with pytest.raises(
        StateError,
        match=r"tags \[2\] point past the configured range",
    ):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )


def test_restore_session_strict_on_duplicate_tags(db: Database, fake_target: _FakeTarget) -> None:
    """Two panes claiming the same config index can't both be the canonical
    pane for that shell; surface the inconsistency rather than guessing."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Two live shell panes both tagged 0 (a duplicate).
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|0\n")

    with pytest.raises(StateError, match=r"duplicate tags \[0\]"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )


def test_restore_session_strict_message_when_configured_zero(db: Database, fake_target: _FakeTarget) -> None:
    """A session with zero configured shells can still have live shell panes
    (e.g. operator ran `tmux split-window` manually then tagged via DB edit).
    The out-of-range error message must not render the empty range as
    `(0..-1)`; instead say the session has no configured shells."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    # Session 'a' with zero shells (no `+N`).
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane + one tagged shell pane (config index 0, but config has 0 shells).
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n")

    with pytest.raises(StateError, match="no configured shells") as excinfo:
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )
    assert "0..-1" not in str(excinfo.value)


def test_restore_session_noop_when_live_matches_config(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Live == configured: no tmux splits or swaps are issued."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|1\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    assert not any("split-window" in c for c in fake_target.commands)
    assert not any("swap-pane" in c for c in fake_target.commands)
    assert any("already matches config" in m for m in captured_output.info)
    # Post-restore landing focus on the session pane is the same regardless
    # of whether repairs were needed.
    assert "tmux select-pane -t =aw-console-con:a.0" in fake_target.commands


# -- restore-session: window-missing rebuild -------------------------------


def test_restore_session_rebuilds_missing_window(db: Database, fake_target: _FakeTarget) -> None:
    """If the session's window is absent from live tmux, restore-session
    rebuilds it via the standard _add_session_window path."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    # No 'a' in the listed windows; only a placeholder name.
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="other\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    new_windows = [c for c in fake_target.commands if "new-window -t =aw-console-con" in c]
    assert len(new_windows) == 1


def test_restore_session_rebuild_focuses_session_pane_under_pane_base_index_one(
    db: Database, fake_target: _FakeTarget
) -> None:
    """On the rebuild path, the session pane's index comes from the
    `new-window -P -F '#{pane_index}'` capture, not the live pane listing the
    repair path uses. Under an inherited `pane-base-index 1` server the fresh
    window's only pane reports index 1, so focus must target `.1`, not the
    literal `.0` (which does not exist under base index 1 and would leave the
    operator on whatever pane tmux last selected). Pins the one base-index-1
    surface the repair-path tests do not reach."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    # Window absent, so restore-session takes the rebuild path.
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="other\n")
    # The freshly built window's pane reports index 1 (pane-base-index 1).
    fake_target.responses["new-window -t =aw-console-con"] = _FakeResult(stdout="1\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    select_panes = [c for c in fake_target.commands if "select-pane -t =aw-console-con:a" in c]
    assert select_panes, "expected the rebuild path to focus the session pane"
    assert all(".1" in c and ".0" not in c for c in select_panes)


def test_restore_session_rebuild_raises_when_new_window_fails(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """_add_session_window warns and skips rather than raising when
    `tmux new-window` fails, so restore-session must check that the window was
    actually built before claiming it rebuilt one."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="other\n")
    fake_target.responses["new-window -t =aw-console-con"] = _FakeResult(returncode=1, stderr="no space for window")

    with pytest.raises(ExternalError, match="failed to rebuild window 'a'"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )

    # RESULT lines mirror into .info; the success line must not be among them.
    assert not any("Rebuilt window" in m for m in captured_output.info)


def test_restore_session_rebuild_raises_when_shell_split_fails(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """The rebuild path must escalate symmetrically with the additive path: if
    a shell pane fails to split while rebuilding a missing window, it raises
    (with a restart hint) rather than reporting a clean rebuild. Otherwise a
    transient split failure would silently produce a partial window."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="other\n")
    # new-window succeeds (default ok); split-window succeeds but prints no
    # pane id, so _split_shell_pane can't tag the pane and returns None.
    fake_target.responses["split-window -t =aw-console-con:a"] = _FakeResult(stdout="")

    with pytest.raises(ExternalError):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )

    # No clean-rebuild success line on a partial rebuild.
    assert not any("Rebuilt window" in m for m in captured_output.info)


def test_restore_session_rebuild_raises_when_shell_tag_fails(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """The tag-failure sub-case is the one that bites: a rebuilt window left
    with an untagged shell pane makes the NEXT restore-session hit the
    untagged-pane refusal. So a split that succeeds but whose set-option tag
    fails must also escalate during rebuild."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="other\n")
    # The pane splits and reports its id, but tagging it fails, so
    # _split_shell_pane returns None (the pane is live but untagged).
    fake_target.responses["split-window -t =aw-console-con:a"] = _FakeResult(stdout="%9\n")
    fake_target.responses["set-option -p"] = _FakeResult(returncode=1, stderr="tmux refused set-option")

    with pytest.raises(ExternalError):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )

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

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    # Window 'a' is absent, so this run would take the rebuild path.
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="other\n")

    fake_target.commands.clear()
    with pytest.raises(StateError, match="no longer exists in the database"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )

    assert not any("new-window" in c for c in fake_target.commands)


# -- restore-session: refuses rather than destroying live state ------------


def test_restore_session_refuses_when_session_pane_killed(db: Database, fake_target: _FakeTarget) -> None:
    """If the operator kills the session-attach pane, tmux renumbers a tagged
    shell down into its slot, so the lowest-indexed pane carries a shell tag
    instead of being untagged. Repairing that means recreating the window,
    which would destroy the operator's live shell panes (and, for a
    single-member console, the console itself), so restore-session refuses and
    points at `console restart`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane gone: the one configured shell (tag 0) has moved into slot 0,
    # so the lowest-indexed pane is tagged.
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|0\n")

    fake_target.commands.clear()
    with pytest.raises(StateError):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )

    # Read-only probes only: the window and its live panes are left exactly as
    # they were, so the operator decides whether to recreate.
    assert fake_target.commands == [
        "tmux has-session -t =aw-console-con 2>/dev/null",
        "tmux list-windows -t =aw-console-con -F '#{window_name}'",
        "tmux list-panes -t =aw-console-con:a -F '#{pane_id}|#{pane_index}|#{@agentworks-shell-index}'",
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

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Two panes, both tagged 0 (no untagged session pane): the observed bug.
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|0\n%2|1|0\n")

    with pytest.raises(StateError, match="lost its session-attach pane"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )


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
        # pane-base-index 1: session pane at 1, shells above it.
        pytest.param("a+1", "a\n", "%1|1|\n%2|2|0\n", id="healthy-base-1"),
        pytest.param("a+2", "a\n", "%1|1|\n%2|2|0\n", id="shell-pane-missing-base-1"),
        pytest.param("a+1", "a\n", "%1|1|0\n", id="session-pane-killed-base-1"),
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

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout=windows)
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout=panes)
    # Any pane the repair paths do open comes back with a pane id to tag.
    fake_target.responses["split-window -t =aw-console-con:a"] = _FakeResult(stdout="%9\n")

    fake_target.commands.clear()
    # Every corrupt state raises; the point here is what was NOT run, so accept
    # either outcome and assert on the commands.
    with contextlib.suppress(StateError, ExternalError):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )

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

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane at index 1 (untagged), shells at 2 and 3.
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|1|\n%2|2|0\n%3|3|1\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    assert any("already matches config" in m for m in captured_output.info)
    assert not any("split-window" in c for c in fake_target.commands)
    # Focus lands on the session pane at its actual index (1 here), not a
    # literal .0 that tmux would reject under `pane-base-index 1`.
    assert "tmux select-pane -t =aw-console-con:a.1" in fake_target.commands
    assert not any("select-pane -t =aw-console-con:a.0" in c for c in fake_target.commands)


def test_restore_session_refuses_killed_session_pane_under_pane_base_index_one(
    db: Database, fake_target: _FakeTarget
) -> None:
    """The mirror of the healthy case: under `pane-base-index 1` a killed
    session pane leaves a tagged shell as the lowest-indexed pane, and that is
    still the refusal."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane killed: the shells renumbered down to 1 and 2.
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%2|1|0\n%3|2|1\n")

    with pytest.raises(StateError, match=r"pane 1 is a shell pane"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )


def test_restore_session_reorders_relative_to_the_session_pane(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Shell config index N belongs one slot after the session pane plus N, so
    under `pane-base-index 1` (session pane at index 1) config index 1 belongs
    at pane 3, not pane 2. The repair also re-applies the default layout, and
    under base index 1 the aw-session-vertical builder must accept the 1..N pane
    run and apply a real select-layout (not warn and skip), then focus the
    session pane at its actual index."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Session pane at index 1; config index 0 is missing, so the surviving
    # shell (config index 1) sits at index 2 and has to move up to index 3.
    # The fake replays this same listing to the reorder pass, which is enough
    # to pin which slot a tagged pane is aimed at. Key on the tagged -F format
    # so this response does not also answer the layout query's list-panes call
    # (which uses a different -F and must fall through to display-message).
    fake_target.responses["-F '#{pane_id}|#{pane_index}|#{@agentworks-shell-index}'"] = _FakeResult(
        stdout="%1|1|\n%2|2|1\n"
    )
    fake_target.responses["split-window -t =aw-console-con:a"] = _FakeResult(stdout="%9\n")
    # aw-session-vertical layout query: geometry + pane list indexed from 1
    # (base index 1). session %1 at 1, new pane %9 at 2, surviving %2 at 3.
    fake_target.responses["display-message -t =aw-console-con:a"] = _FakeResult(stdout="80x36\n1 %1\n2 %9\n3 %2\n")

    fake_target.commands.clear()
    restore_session(
        db, _StubVerticalLayoutConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
    )

    swaps = [c for c in fake_target.commands if "swap-pane" in c]
    assert swaps == ["tmux swap-pane -s %2 -t =aw-console-con:a.3"]
    # The vertical layout applied for real (a computed select-layout string),
    # with no "too small or unparseable" warning under base index 1.
    assert any("select-layout -t =aw-console-con:a" in c for c in fake_target.commands)
    assert not any("could not build aw-session-vertical" in w for w in captured_output.warnings)
    # Focus lands on the session pane at index 1, not a literal .0.
    assert "tmux select-pane -t =aw-console-con:a.1" in fake_target.commands


# -- restore-session: additive shell-pane repair ---------------------------


def test_restore_session_raises_when_split_returns_no_pane_id(db: Database, fake_target: _FakeTarget) -> None:
    """If tmux split-window succeeds but doesn't print a pane id, the pane
    is created but untagged. restore-session must surface this as an error
    so the operator doesn't see exit-0 while a window is left incomplete."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+3"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|2\n")
    # split-window succeeds but returns no pane id; _split_shell_pane warns
    # and returns None, which restore_session must escalate.
    fake_target.responses["split-window -t =aw-console-con:a"] = _FakeResult(stdout="")

    with pytest.raises(ExternalError, match=r"failed to create/tag config indices \[1\]"):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )


def test_restore_session_splits_missing_config_indices_and_tags_them(db: Database, fake_target: _FakeTarget) -> None:
    """Live < configured: restore-session identifies missing config indices
    by tag diff, splits each one back in with the correct tag, and applies
    select-layout to redistribute geometry."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    # Three shells configured; index 1 ("fish") is missing live.
    create_console(db, name="con", vm_name="vm1", session_specs=["a+3"])

    fake_target.responses["has-session -t =aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t =aw-console-con"] = _FakeResult(stdout="a\n")
    # Live: session pane (pidx 0), tagged shells for indices 0 and 2; 1 is gone.
    fake_target.responses["list-panes -t =aw-console-con:a"] = _FakeResult(stdout="%1|0|\n%2|1|0\n%3|2|2\n")
    # split-window returns a fresh pane id so the tag step has a target.
    fake_target.responses["split-window -t =aw-console-con:a"] = _FakeResult(stdout="%9\n")

    fake_target.commands.clear()
    restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    splits = [c for c in fake_target.commands if "split-window -t =aw-console-con:a" in c]
    assert len(splits) == 1
    set_options = [c for c in fake_target.commands if "set-option -p" in c and SHELL_INDEX_OPTION in c]
    # The new pane gets tagged with config index 1 (the missing one).
    assert any(f"-t %9 {SHELL_INDEX_OPTION} 1" in c for c in set_options)
    layouts = [c for c in fake_target.commands if "select-layout -t =aw-console-con:a tiled" in c]
    assert len(layouts) == 1


# -- restore-session: prove-by-consequence against a stateful tmux model ----
#
# The tests above pin restore-session's behavior against the stateless
# `_FakeTarget` (a fixed substring -> response map). That fake cannot
# represent tmux STATE: a kill-window has no effect on a later
# list-windows, so the destructive-remedy bug the command's contract
# guards against slipped through green once (see
# `test_stateful_model_catches_kill_window_then_new_window_destruction`).
# The tests below drive restore-session against a `TmuxModel` seeded into
# each state and assert on the RESULTING MODEL STATE (session still alive,
# window still present, panes intact/correct) in addition to the old
# `kill-*` string-absence pin (kept as a cheap secondary check).
#
# How much the model-state assertion adds depends on the path:
#   - Refusal cases (`test_restore_session_refuses_and_leaves_the_model_intact`)
#     take no mutating action, so `pane_rows == before` holds trivially
#     whether or not any mutator is correct. There these tests prove exactly
#     no-mutation-and-no-destructive-command, no more: the string-absence pin
#     and the unchanged-state assertion are two readings of the same fact.
#   - The mutation-dependent proofs are the ones that DO drive mutators
#     through the model: `test_restore_session_additively_repairs_a_missing_shell_pane`
#     (the split/tag/reorder repair must land the panes in the right slots)
#     and `test_stateful_model_catches_kill_window_then_new_window_destruction`
#     (the destructive remedy leaves the model with the session destroyed,
#     which the stateless fake could not represent). Those are where asserting
#     on state is genuinely stronger than a string check.


def _make_console(db: Database, session_spec: str) -> None:
    """Seed the VM, session, and console DB rows a restore-session run reads."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=[session_spec])


def _destructive(commands: list[str]) -> list[str]:
    return [c for c in commands if "kill-window" in c or "kill-pane" in c or "kill-session" in c]


@pytest.mark.parametrize(
    ("base", "pane_tags", "session_spec", "match"),
    [
        # Killed attach pane: the one shell compacted into the lowest slot, so
        # the lowest-indexed pane is tagged. Single-window console, so a
        # recreate here would take the console's last window (and the whole
        # tmux session) with it. This is the positive counterpart: the CURRENT
        # restore refuses and the console survives untouched.
        pytest.param(0, (0,), "a+1", "lost its session-attach pane", id="killed-attach-pane"),
        # The stuck duplicate-shell state seen in the wild: session pane killed,
        # a prior buggy restore added a duplicate shell, so both panes are
        # tagged 0. Still reported as the missing session pane it is.
        pytest.param(0, (0, 0), "a+1", "lost its session-attach pane", id="stuck-duplicate-shell"),
        # Same physics under an inherited pane-base-index 1.
        pytest.param(1, (0,), "a+2", r"pane 1 is a shell pane", id="killed-attach-pane-base-1"),
    ],
)
def test_restore_session_refuses_and_leaves_the_model_intact(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
    base: int,
    pane_tags: tuple[int | None, ...],
    session_spec: str,
    match: str,
) -> None:
    """In each corrupted state restore-session cannot safely repair, it raises
    and leaves live tmux exactly as it found it: the session stays alive, the
    window stays present, and its panes are byte-for-byte unchanged."""
    _make_console(db, session_spec)
    model = TmuxModel(pane_base_index=base)
    model.seed_session(CON, "a", pane_tags=pane_tags)
    before = model.pane_rows(CON, "a")
    target = console_target_factory(model)

    with pytest.raises(StateError, match=match):
        restore_session(
            db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE
        )

    # The console survives and nothing about the window changed.
    assert model.has_session(CON)
    assert "a" in model.window_names(CON)
    assert model.pane_rows(CON, "a") == before
    assert _destructive(target.commands) == []


@pytest.mark.parametrize("base", [0, 1])
def test_restore_session_is_a_noop_on_a_healthy_model(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
    captured_output: CapturedOutput,
    base: int,
) -> None:
    """A window whose live panes already match config is left untouched: no
    splits, no kills, and the model still holds exactly the session pane plus
    the one configured shell."""
    _make_console(db, "a+1")
    model = TmuxModel(pane_base_index=base)
    model.seed_session(CON, "a", pane_tags=(None, 0))
    target = console_target_factory(model)

    restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    assert any("already matches config" in m for m in captured_output.info)
    assert not any("split-window" in c for c in target.commands)
    assert _destructive(target.commands) == []
    assert model.has_session(CON)
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    assert [tag for _pid, _pidx, tag in rows] == [None, 0]


def test_restore_session_additively_repairs_a_missing_shell_pane(
    db: Database,
    console_target_factory: Callable[..., _FakeTarget],
) -> None:
    """The additive path, proved by consequence: with one of two configured
    shells live, restore-session splits the missing shell back in and tags it,
    leaving the model with the session pane plus both shells in config order,
    while never destroying the pane that was already there."""
    _make_console(db, "a+2")
    model = TmuxModel()
    # Session pane (untagged) plus config-index-0 shell live; index 1 missing.
    model.seed_session(CON, "a", pane_tags=(None, 0))
    surviving_shell_id = model.pane_rows(CON, "a")[1][0]  # type: ignore[index]
    target = console_target_factory(model)

    restore_session(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    assert model.has_session(CON)
    rows = model.pane_rows(CON, "a")
    assert rows is not None
    # Session pane still lowest and untagged; both shells present, in order.
    assert [pidx for _pid, pidx, _tag in rows] == [0, 1, 2]
    assert [tag for _pid, _pidx, tag in rows] == [None, 0, 1]
    # The pre-existing shell pane was never destroyed, just kept in place.
    assert any(pid == surviving_shell_id for pid, _pidx, _tag in rows)
    assert _destructive(target.commands) == []


def test_stateful_model_catches_kill_window_then_new_window_destruction() -> None:
    """Regression proof that the stateful harness catches the original
    console-destroying remedy.

    The bug the restore-session contract guards against was a repair that did
    `kill-window` on a window and then `new-window` to rebuild it. For a
    single-window console that is fatal: tmux requires at least one window, so
    killing the only window destroys the whole tmux session, and the follow-up
    `new-window` then has no session to attach to. Against the OLD stateless
    fake this sequence looked green (the post-kill list-windows still replayed
    its seed and new-window still returned default-OK), which is precisely how
    the bug reached main. The destructive code is no longer in the tree, so we
    drive the model through that exact sequence directly and show the model
    now reports the console destroyed and the new-window failing.
    """
    model = TmuxModel()
    model.new_session(CON, "a")  # single-window console
    assert model.has_session(CON)

    # The old destructive remedy: kill the window, then rebuild it.
    assert model.dispatch(f"tmux kill-window -t {CON}:a").ok
    # tmux destroys the session with its last window: has-session now fails...
    assert not model.dispatch(f"tmux has-session -t {CON} 2>/dev/null").ok
    assert not model.has_session(CON)
    # ...and the follow-up new-window against the dead session fails, exactly
    # as it would against real tmux (and exactly what the stateless fake could
    # not represent, since it returned default-OK regardless of state).
    assert not model.dispatch(f"tmux new-window -t {CON} -n a -P -F '#{{pane_index}}'").ok
