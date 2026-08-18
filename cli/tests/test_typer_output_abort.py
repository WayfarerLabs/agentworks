"""Ctrl-C at an interactive prompt converts to UserAbort (issue #300).

typer.confirm/typer.prompt are typer's own vendored functions and on
Ctrl-C raise typer's vendored Abort (``typer.Abort``, module
``typer._click.exceptions``), which is a different class from the
top-level ``click.exceptions.Abort`` (``typer.Abort is
click.exceptions.Abort`` is False, and neither subclasses the other).
These tests drive the real TyperHandler methods with the prompt
functions monkeypatched to raise, and assert every site catches both the
vendored and the real Abort and converts them to UserAbort rather than
letting the raw Abort escape. They mirror the monkeypatch harness in
test_typer_output.py (stub the prompt function, no stdin/pty).
"""

from __future__ import annotations

import sys

import click
import pytest
import typer

from agentworks.cli._typer_output import TyperHandler
from agentworks.errors import UserAbort


def test_typer_and_click_abort_are_distinct_classes() -> None:
    # The premise of the fix: the two Abort classes are unrelated, so
    # ``except click.exceptions.Abort`` alone cannot catch typer's. The
    # operands are widened to type[BaseException] so the identity check is
    # a runtime assertion, not one mypy folds away as non-overlapping.
    vendored: type[BaseException] = typer.Abort
    real: type[BaseException] = click.exceptions.Abort
    assert vendored is not real
    assert not issubclass(vendored, real)
    assert not issubclass(real, vendored)


def test_confirm_converts_vendored_abort_to_user_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    # stdout non-TTY so the mouse-tracking reset branch is skipped.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    def _raise(*_a: object, **_k: object) -> bool:
        raise typer.Abort()

    monkeypatch.setattr(typer, "confirm", _raise)
    with pytest.raises(UserAbort):
        TyperHandler().confirm("Proceed?", level=0)


def test_choose_converts_vendored_abort_to_user_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> str:
        raise typer.Abort()

    monkeypatch.setattr(typer, "prompt", _raise)
    with pytest.raises(UserAbort):
        TyperHandler().choose("Pick one", ["a", "b"], level=0)


def test_prompt_converts_vendored_abort_to_user_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> str:
        raise typer.Abort()

    monkeypatch.setattr(typer, "prompt", _raise)
    with pytest.raises(UserAbort):
        TyperHandler().prompt("Name", level=0)


def test_confirm_also_converts_real_click_abort_to_user_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    # The widened tuple also catches a REAL click Abort at this typer-vendored
    # site, so narrowing the catch back to typer.Abort alone would fail here.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    def _raise(*_a: object, **_k: object) -> bool:
        raise click.exceptions.Abort()

    monkeypatch.setattr(typer, "confirm", _raise)
    with pytest.raises(UserAbort):
        TyperHandler().confirm("Proceed?", level=0)


def test_choose_also_converts_real_click_abort_to_user_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> str:
        raise click.exceptions.Abort()

    monkeypatch.setattr(typer, "prompt", _raise)
    with pytest.raises(UserAbort):
        TyperHandler().choose("Pick one", ["a", "b"], level=0)


def test_prompt_also_converts_real_click_abort_to_user_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> str:
        raise click.exceptions.Abort()

    monkeypatch.setattr(typer, "prompt", _raise)
    with pytest.raises(UserAbort):
        TyperHandler().prompt("Name", level=0)


def test_prompt_secret_converts_real_click_abort_to_user_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    # prompt_secret uses the REAL click.prompt, so a real Ctrl-C raises
    # the real click.exceptions.Abort there; this guards that site.
    def _raise(*_a: object, **_k: object) -> str:
        raise click.exceptions.Abort()

    monkeypatch.setattr(click, "prompt", _raise)
    with pytest.raises(UserAbort) as caught:
        TyperHandler().prompt_secret("Token", level=0)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_prompt_secret_also_converts_vendored_abort_to_user_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    # The widened tuple additionally covers typer's vendored Abort here,
    # robust if this site's underlying click ever changes.
    def _raise(*_a: object, **_k: object) -> str:
        raise typer.Abort()

    monkeypatch.setattr(click, "prompt", _raise)
    with pytest.raises(UserAbort) as caught:
        TyperHandler().prompt_secret("Token", level=0)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_prompt_secret_refuses_without_a_terminal_instead_of_reading_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without a terminal, click.prompt(hide_input=True) falls back to
    # reading stdin directly, which hangs on an open, silent pipe. This is
    # the last-resort backstop: the resolver already skips a terminal-channel
    # source when no terminal is available, so reaching this site means a
    # caller prompted anyway, and it must refuse before any prompt function
    # runs rather than risk the hang.
    monkeypatch.setattr("agentworks.output.terminal_prompt_available", lambda: False)
    monkeypatch.setattr(click, "prompt", lambda *_a, **_k: pytest.fail("must not prompt without a terminal"))
    with pytest.raises(UserAbort) as caught:
        TyperHandler().prompt_secret("Token", level=0)
    assert caught.value.__cause__ is None
