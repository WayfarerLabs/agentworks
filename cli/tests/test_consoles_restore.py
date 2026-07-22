"""Tests for ``agw console restore-session`` -- argument and live-state
validation, strict failure paths, and happy paths. Carved out of
test_consoles.py to keep that file under the project's file-length
guidance; helpers / fixtures are imported from the original location
and from conftest."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import ExternalError, NotFoundError, StateError
from agentworks.sessions.multi_console import (
    create_console,
    restore_session,
)
from agentworks.sessions.multi_console_layout import SHELL_INDEX_OPTION
from tests.conftest import _FakeResult, _FakeTarget, stub_build_registry
from tests.test_consoles import (
    _seed_sessions,
    _seed_vm,
    _StubConfig,
)

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


# -- restore-session: argument and live-state validation -------------------


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve Registry reads from the module's namespace configs."""
    stub_build_registry(monkeypatch)


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


# -- restore-session: happy paths ------------------------------------------


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
