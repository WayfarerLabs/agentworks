"""Tests for the aw-session-vertical layout and the session-pane focus
behavior added in the same change. Carved out of test_consoles.py to
keep that file navigable; shared seed helpers / stub Config classes /
the autouse Registry-stub fixture now live in
``tests/_consoles_support.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import Database
from agentworks.sessions.multi_console import (
    add_shell,
    create_console,
    restore_session,
)
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


class _StubVerticalLayoutConfig(_StubConfig):
    """Stub config whose named_console layout selects aw-session-vertical.

    Inherits the empty vm/agent/workspace/session_templates + admin + resolver
    defaults from ``_StubConfig`` so console env-resolution code doesn't crash;
    overrides the named-console layout.
    """

    class _NC:
        tmux_layout: str = "aw-session-vertical"

    # _NC mirrors _StubNamedConsoleConfig's surface (just ``tmux_layout``)
    # but isn't structurally identical to mypy. Tests pass _StubConfig
    # subclasses to the SUT via ducktyping; the real Config type isn't
    # involved here.
    named_console = _NC()  # type: ignore[assignment]


def test_apply_layout_preset_emits_simple_select_layout(
    fake_target: _FakeTarget,
) -> None:
    """Tmux preset layout names go straight to `select-layout`."""
    from agentworks.sessions.multi_console_layout import _apply_layout

    _apply_layout(fake_target, "aw-console-con", "alpha", "tiled")  # type: ignore[arg-type]
    assert fake_target.commands == [
        "tmux select-layout -t aw-console-con:alpha tiled",
    ]


def test_apply_layout_aw_session_vertical_queries_then_applies_string(
    fake_target: _FakeTarget,
) -> None:
    """`aw-session-vertical` queries window geometry + pane IDs, builds a
    custom tmux layout string Python-side, and applies it via
    `select-layout`. Two SSH calls: query then apply."""
    from agentworks.sessions.multi_console_layout import _apply_layout

    # First call returns geometry + pane ids; the helper builds a layout
    # string from this and applies it via the second call.
    fake_target.responses["display-message -t aw-console-con:alpha"] = _FakeResult(
        returncode=0, stdout="80x36\n0 %31\n1 %32\n2 %33\n"
    )

    _apply_layout(
        fake_target,  # type: ignore[arg-type]
        "aw-console-con",
        "alpha",
        "aw-session-vertical",
    )

    # Expect two commands: the geometry/pane query and the select-layout apply.
    assert len(fake_target.commands) == 2
    query_cmd, apply_cmd = fake_target.commands
    assert "display-message -t aw-console-con:alpha" in query_cmd
    assert "list-panes -t aw-console-con:alpha" in query_cmd
    # The apply command holds the computed string with a tmux checksum prefix.
    # For 80x36 with 3 panes: session=18, shells=8+8, full geometry inside [].
    assert (
        "tmux select-layout -t aw-console-con:alpha '3b1b,80x36,0,0[80x18,0,0,31,80x8,0,19,32,80x8,0,28,33]'"
    ) in apply_cmd


def test_tmux_layout_checksum_matches_reference() -> None:
    """The 16-bit checksum implementation must agree with tmux's own
    output. Reference string + expected hash from a known tmux 3.3a
    layout (verified live)."""
    from agentworks.sessions.multi_console_layout import _tmux_layout_checksum

    body = "269x36,0,0[269x18,0,0,28,269x4,0,19,29,269x12,0,24,30]"
    assert f"{_tmux_layout_checksum(body):04x}" == "e088"


def test_build_aw_session_vertical_layout_string_geometry() -> None:
    """Builder produces exact geometry: session at H/2, shells share the
    rest equally with per-pane border accounting."""
    from agentworks.sessions.multi_console_layout import (
        _build_aw_session_vertical_layout_string,
    )

    # 1 shell: session 18, shell 17 (no internal shell borders).
    s1 = _build_aw_session_vertical_layout_string("80x36\n0 %1\n1 %2\n")
    assert s1 is not None
    assert "80x18,0,0,1" in s1
    assert "80x17,0,19,2" in s1

    # 2 shells: session 18, shells 8 + 8 (1 border between them).
    s2 = _build_aw_session_vertical_layout_string("80x36\n0 %1\n1 %2\n2 %3\n")
    assert s2 is not None
    assert "80x18,0,0,1" in s2
    assert "80x8,0,19,2" in s2
    assert "80x8,0,28,3" in s2

    # 3 shells: session 18, shells 5 + 5 + 5 (2 borders).
    s3 = _build_aw_session_vertical_layout_string("80x36\n0 %1\n1 %2\n2 %3\n3 %4\n")
    assert s3 is not None
    assert "80x18,0,0,1" in s3
    assert "80x5,0,19,2" in s3
    assert "80x5,0,25,3" in s3
    assert "80x5,0,31,4" in s3


def test_build_aw_session_vertical_layout_string_edge_cases() -> None:
    """Builder returns None on parse failures, empty input, and
    too-small windows (rare in practice but easy to handle cleanly)."""
    from agentworks.sessions.multi_console_layout import (
        _build_aw_session_vertical_layout_string,
    )

    # Empty / malformed input.
    assert _build_aw_session_vertical_layout_string("") is None
    assert _build_aw_session_vertical_layout_string("garbage") is None
    assert _build_aw_session_vertical_layout_string("80x36\n") is None
    # Window dimensions unparseable.
    assert _build_aw_session_vertical_layout_string("nox\n0 %1\n1 %2\n") is None
    # Single pane: no layout to apply.
    assert _build_aw_session_vertical_layout_string("80x36\n0 %1\n") is None
    # Window too small for the geometry (4 panes can't fit in H=4).
    too_small = _build_aw_session_vertical_layout_string("80x4\n0 %1\n1 %2\n2 %3\n3 %4\n")
    assert too_small is None
    # Non-contiguous pane indices (missing pane 0): can't assign a session
    # slot, so we refuse to build rather than put a shell in the top slot.
    assert _build_aw_session_vertical_layout_string("80x36\n1 %1\n2 %2\n") is None


def test_build_aw_session_vertical_layout_string_sorts_panes_by_index() -> None:
    """list-panes output isn't trusted to be in pane_index order; the
    builder must sort and use the index-0 pane as the session pane."""
    from agentworks.sessions.multi_console_layout import (
        _build_aw_session_vertical_layout_string,
    )

    # Reverse-order input; pane index 0 is at the bottom of the output.
    reversed_in = _build_aw_session_vertical_layout_string("80x36\n2 %33\n1 %32\n0 %31\n")
    sorted_in = _build_aw_session_vertical_layout_string("80x36\n0 %31\n1 %32\n2 %33\n")
    assert reversed_in == sorted_in


def test_apply_layout_aw_session_vertical_silent_on_single_pane(
    fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """A session window with zero configured shells has just the session
    pane; nothing to lay out. The apply helper must NOT warn -- this is
    normal, not an error."""
    from agentworks.sessions.multi_console_layout import (
        _apply_aw_session_vertical_layout,
    )

    fake_target.responses["display-message -t aw-console-con:alpha"] = _FakeResult(returncode=0, stdout="80x36\n0 %1\n")

    _apply_aw_session_vertical_layout(
        fake_target,
        "aw-console-con",
        "alpha",  # type: ignore[arg-type]
    )

    # The query happens, but no select-layout call and no warning.
    assert not any("select-layout" in c for c in fake_target.commands)
    assert not any("could not build aw-session-vertical" in w for w in captured_output.warnings)


def test_apply_layout_aw_session_vertical_warns_on_genuine_failure(
    fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Too-small geometry (or anything else the builder refuses) should
    still produce a warning so an operator sees a breadcrumb."""
    from agentworks.sessions.multi_console_layout import (
        _apply_aw_session_vertical_layout,
    )

    fake_target.responses["display-message -t aw-console-con:alpha"] = _FakeResult(
        returncode=0, stdout="80x4\n0 %1\n1 %2\n2 %3\n3 %4\n"
    )

    _apply_aw_session_vertical_layout(
        fake_target,
        "aw-console-con",
        "alpha",  # type: ignore[arg-type]
    )

    assert any("could not build aw-session-vertical" in w for w in captured_output.warnings)


def test_focus_session_pane_emits_select_pane(fake_target: _FakeTarget) -> None:
    from agentworks.sessions.multi_console_layout import _focus_session_pane

    _focus_session_pane(fake_target, "aw-console-con", "alpha")  # type: ignore[arg-type]
    assert fake_target.commands == [
        "tmux select-pane -t aw-console-con:alpha.0",
    ]


def test_attach_console_focuses_session_pane_per_window(db: Database, fake_target: _FakeTarget) -> None:
    """First-attach builds windows; each window ends with select-pane on
    pane 0 so the operator lands on the session output, not the
    most-recently-created shell pane."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha", "beta"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha+1", "beta"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\nalpha\nbeta\n"
    )

    attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    selects = [c for c in fake_target.commands if "select-pane" in c]
    assert "tmux select-pane -t aw-console-con:alpha.0" in selects
    assert "tmux select-pane -t aw-console-con:beta.0" in selects


def test_restore_session_focuses_session_pane(db: Database, fake_target: _FakeTarget) -> None:
    """restore-session repair ends with the session pane focused."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+2"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="a\n")
    # One shell pane present (config_index=0), the second is missing.
    fake_target.responses["list-panes -t aw-console-con:a"] = _FakeResult(returncode=0, stdout="%5|0|\n%6|1|0\n")
    # split-window must return a fresh pane id so the tag step succeeds.
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(stdout="%9\n")

    restore_session(db, _StubConfig(), console_name="con", session_name="a")

    assert "tmux select-pane -t aw-console-con:a.0" in fake_target.commands


def test_add_shell_does_not_focus_session_pane(db: Database, fake_target: _FakeTarget) -> None:
    """add-shell is invoked mid-attach; pulling focus away from the
    operator's current pane would be jarring. Layout still re-applies."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    assert not any("select-pane" in c for c in fake_target.commands)
    # But the layout still re-applies for the new pane count.
    assert any("select-layout -t aw-console-con:a tiled" in c for c in fake_target.commands)


def test_attach_console_aw_session_vertical_layout(db: Database, fake_target: _FakeTarget) -> None:
    """When the config picks aw-session-vertical, the build queries window
    geometry + pane IDs, then applies a hand-computed layout string."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha+1"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(returncode=0, stdout="_PLACEHOLDER\nalpha\n")
    fake_target.responses["display-message -t aw-console-con:alpha"] = _FakeResult(
        returncode=0, stdout="80x36\n0 %1\n1 %2\n"
    )

    attach_console(
        db,
        _StubVerticalLayoutConfig(),
        name="con",
        allow_nesting=True,  # type: ignore[arg-type]
    )

    # One select-layout with the hand-computed string for 1-shell case
    # (session 18, shell 17).
    layout_cmds = [c for c in fake_target.commands if "select-layout" in c]
    assert any(
        "tmux select-layout -t aw-console-con:alpha '67a5,80x36,0,0[80x18,0,0,1,80x17,0,19,2]'" in c
        for c in layout_cmds
    ), layout_cmds
