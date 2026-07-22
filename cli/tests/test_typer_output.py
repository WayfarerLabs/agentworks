"""Unit tests for the Typer-backed output handler.

Manual reproduction (issue #211, mouse-tracking leak): a genuine live-TTY
repro is not exercisable in an automated suite, since it depends on a real
terminal's mouse-report byte stream. To verify by hand on a real terminal:

1. Enable mouse tracking directly, mimicking whatever earlier interactive
   step (e.g. a TUI session) left it on: ``printf '\\e[?1000;1006h'``.
2. Run a flow that hits ``TyperHandler.confirm`` (any ``[y/N]`` prompt).
3. Click anywhere in the terminal before answering the prompt, then answer
   it. Before this fix, the click's SGR mouse report (``^[[<..M``) leaks
   into the input and reappears in the next line of output. After this
   fix, the DECRST reset (``_MOUSE_TRACKING_DISABLE``) is written before
   the prompt is read, so mouse reporting is off and no report leaks.
"""

from __future__ import annotations

import re
import sys

import click
import pytest
import typer

from agentworks import output
from agentworks.cli._typer_output import TyperHandler
from agentworks.output import Role, StatusStyle

# Strip SGR color escapes so a rendered line can be compared against its
# byte-plain form (mirrors tests/test_session_agent_filter.py:_plain).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make both captured streams report as terminals with color allowed."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)


# --- Color on a TTY: each colorable role carries its palette entry --------


def test_header_role_is_bold_on_a_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _tty(monkeypatch)
    TyperHandler().emit(Role.HEADER, "Preflight", 0)
    out = capsys.readouterr().out
    # The `=== t ===` rule text is preserved; bold is the only TTY add.
    assert click.style("=== Preflight ===", bold=True) in out
    assert _plain(out) == "\n=== Preflight ===\n"


def test_warning_role_prefix_is_yellow_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tty(monkeypatch)
    TyperHandler().emit(Role.WARNING, "careful now", 0)
    err = capsys.readouterr().err
    # Only the prefix is colored; the message stays default.
    assert err == f"{click.style('Warning:', fg='yellow')} careful now\n"
    assert _plain(err) == "Warning: careful now\n"


def test_error_role_prefix_is_red_on_a_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _tty(monkeypatch)
    TyperHandler().emit(Role.ERROR, "it broke", 0)
    err = capsys.readouterr().err
    assert err == f"{click.style('Error:', fg='red')} it broke\n"
    assert _plain(err) == "Error: it broke\n"


def test_result_role_is_dim_green_on_a_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _tty(monkeypatch)
    TyperHandler().emit(Role.RESULT, "VM 'box' deleted", 0)
    out = capsys.readouterr().out
    assert out == f"{click.style("VM 'box' deleted", fg='green', dim=True)}\n"
    assert _plain(out) == "VM 'box' deleted\n"


def test_detail_role_is_dim_on_a_tty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _tty(monkeypatch)
    TyperHandler().emit(Role.DETAIL, "OK: acl package", 0)
    out = capsys.readouterr().out
    # DETAIL renders one level deeper than a sibling BODY line.
    assert out == f"  {click.style('OK: acl package', dim=True)}\n"
    assert _plain(out) == "  OK: acl package\n"


def test_body_role_is_never_colored_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tty(monkeypatch)
    TyperHandler().emit(Role.BODY, "Creating workspace", 0)
    out = capsys.readouterr().out
    assert out == "Creating workspace\n"
    assert _ANSI_RE.search(out) is None


# --- style_status: token styling (the STATUS role's realized form) -------


def test_style_status_good_is_green_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch)
    assert TyperHandler().style_status("[ok]", StatusStyle.GOOD) == click.style("[ok]", fg="green")


def test_style_status_warn_is_yellow_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch)
    assert TyperHandler().style_status("[warn]", StatusStyle.WARN) == click.style("[warn]", fg="yellow")


def test_style_status_bad_is_red_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch)
    assert TyperHandler().style_status("[FAIL]", StatusStyle.BAD) == click.style("[FAIL]", fg="red")


def test_style_status_neutral_is_unstyled_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _tty(monkeypatch)
    text = TyperHandler().style_status("[info]", StatusStyle.NEUTRAL)
    assert text == "[info]"
    assert _ANSI_RE.search(text) is None


def test_style_status_returns_plain_text_off_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert TyperHandler().style_status("[ok]", StatusStyle.GOOD) == "[ok]"


def test_style_status_returns_plain_text_under_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "")
    assert TyperHandler().style_status("[ok]", StatusStyle.GOOD) == "[ok]"


def test_style_status_returns_plain_text_under_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    output.set_non_interactive(True)
    try:
        assert TyperHandler().style_status("[ok]", StatusStyle.GOOD) == "[ok]"
    finally:
        output.set_non_interactive(False)


# --- Plain fallbacks: NO_COLOR, non-TTY, and --non-interactive ------------


def _assert_all_roles_byte_plain(capsys: pytest.CaptureFixture[str]) -> None:
    handler = TyperHandler()
    handler.emit(Role.HEADER, "Preflight", 0)
    handler.emit(Role.RESULT, "done", 0)
    handler.emit(Role.DETAIL, "note", 0)
    handler.emit(Role.BODY, "step", 0)
    handler.emit(Role.WARNING, "careful", 0)
    handler.emit(Role.ERROR, "broke", 0)
    captured = capsys.readouterr()
    assert captured.out == "\n=== Preflight ===\ndone\n  note\nstep\n"
    assert captured.err == "Warning: careful\nError: broke\n"
    assert _ANSI_RE.search(captured.out) is None
    assert _ANSI_RE.search(captured.err) is None


def test_no_color_env_forces_byte_plain_even_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    # NO_COLOR is honored by presence, any value (here the empty string).
    monkeypatch.setenv("NO_COLOR", "")
    _assert_all_roles_byte_plain(capsys)


def test_non_tty_stream_is_byte_plain(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    _assert_all_roles_byte_plain(capsys)


def test_non_interactive_forces_byte_plain_even_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    output.set_non_interactive(True)
    try:
        _assert_all_roles_byte_plain(capsys)
    finally:
        output.set_non_interactive(False)


def test_confirm_resets_mouse_tracking_before_prompting_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    handler = TyperHandler()
    assert handler.confirm("Proceed?", level=0) is True

    # Exact bytes: only the DECRST reset, nothing else (the stubbed
    # typer.confirm never touches stdout, so any other byte here would
    # mean the reset picked up extra content).
    assert capsys.readouterr().out == "\x1b[?1000;1002;1003;1006;1015l"


def test_confirm_emits_no_escape_when_stream_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)

    handler = TyperHandler()
    assert handler.confirm("Proceed?", level=0) is True

    assert capsys.readouterr().out == ""


def test_confirm_emits_no_escape_under_non_interactive_even_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A stray mouse report can only reach the confirm read if a real
    # terminal is on the other end, but --non-interactive should still
    # suppress the reset: it signals no prompt should meaningfully occur,
    # and output must stay byte-plain regardless of what stdout is
    # attached to.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    output.set_non_interactive(True)
    try:
        handler = TyperHandler()
        assert handler.confirm("Proceed?", level=0) is True
        assert capsys.readouterr().out == ""
    finally:
        output.set_non_interactive(False)
