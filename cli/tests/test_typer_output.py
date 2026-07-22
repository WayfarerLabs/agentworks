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

import sys

import pytest
import typer

from agentworks import output
from agentworks.cli._typer_output import TyperHandler


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
