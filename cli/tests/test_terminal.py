"""Local-terminal protection around interactive attaches.

Manual reproduction (the bug this module exists for): attach to a
console over SSH, then break the connection at the network level rather
than detaching (suspend the laptop, drop Wi-Fi, ``pkill -9 ssh`` from
another tab). Before this fix the tab is left with mouse reporting on,
so clicking anywhere in it emits ``^[[<0;12;34M`` into the shell, and
often with echo off as well. To verify by hand:

1. ``agw console attach <name>``, then kill the SSH connection from
   another terminal rather than detaching.
2. Click in the tab. No mouse escape sequences should appear, the shell
   prompt should be visible, and typing should echo.

The tests here cover what is checkable without a real TTY: the payload's
content and ordering, and the TTY gate. The Windows console-mode half
needs a real console and is only covered by the manual reproduction
above.
"""

from __future__ import annotations

import io

import pytest

from agentworks.terminal import (
    MOUSE_TRACKING_DISABLE,
    TERMINAL_SANITIZE,
    guarded_terminal,
    sanitize_terminal,
)


class _FakeStdout(io.StringIO):
    """A StringIO that reports itself as a terminal (or not)."""

    def __init__(self, *, tty: bool = True) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _install_stdout(monkeypatch: pytest.MonkeyPatch, *, tty: bool = True) -> _FakeStdout:
    """Swap in a fake stdout and return it.

    Must be called from the test BODY, never from a fixture. pytest's
    capture manager reassigns ``sys.stdout`` to its own object when it
    resumes global capture at the start of the call phase, so a swap
    performed during fixture setup is silently clobbered before the test
    runs and every assertion against the fake trivially sees an empty
    buffer.

    ``sys.platform`` is pinned to a non-Windows value so the POSIX
    restore path is what runs, regardless of the host running the suite.
    """
    fake = _FakeStdout(tty=tty)
    monkeypatch.setattr("sys.stdout", fake)
    monkeypatch.setattr("sys.platform", "linux")
    return fake


# ---------------------------------------------------------------------------
# the sanitize payload
# ---------------------------------------------------------------------------


def test_sanitize_supersets_the_mouse_reset() -> None:
    """The attach-level payload includes the prompt-level mouse reset.

    One definition of "mouse reporting off" (covering modes 1000, 1002,
    1003, 1006 and 1015 in a single DECRST), shared with the confirm
    prompt's guard in ``cli/_typer_output.py``.
    """
    assert MOUSE_TRACKING_DISABLE in TERMINAL_SANITIZE
    assert ";1006" in MOUSE_TRACKING_DISABLE, "SGR mouse mode is the one that garbles a dropped attach"


@pytest.mark.parametrize(
    ("escape", "what"),
    [
        ("\x1b[?2004l", "bracketed paste"),
        ("\x1b[?1004l", "focus reporting"),
        ("\x1b[?1049l", "alternate screen"),
        ("\x1b[?1l", "application cursor keys"),
        ("\x1b>", "application keypad"),
        ("\x1b[?25h", "cursor visibility"),
        ("\x1b[?7h", "autowrap"),
        ("\x1b[0m", "colors and attributes"),
    ],
)
def test_sanitize_clears_each_mode_a_remote_program_can_leave_set(escape: str, what: str) -> None:
    assert escape in TERMINAL_SANITIZE, f"sanitize payload does not restore {what}"


def test_sanitize_resets_scroll_region_before_leaving_alternate_screen() -> None:
    """Ordering matters: DECSTBM homes the cursor as a side effect.

    Leaving the alternate screen afterwards restores the pre-attach
    cursor position and absorbs that side effect. Reversed, the homing
    lands on the primary screen and the next shell prompt draws over the
    operator's scrollback.
    """
    assert TERMINAL_SANITIZE.index("\x1b[r") < TERMINAL_SANITIZE.index("\x1b[?1049l")


def test_sanitize_does_not_hard_reset() -> None:
    """RIS would clear all of this, but it also clears scrollback in
    most terminals, destroying what the operator most wants to read
    after a dropped attach."""
    assert "\x1bc" not in TERMINAL_SANITIZE


# ---------------------------------------------------------------------------
# sanitize_terminal()
# ---------------------------------------------------------------------------


def test_sanitize_terminal_writes_the_payload_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_stdout(monkeypatch)
    sanitize_terminal()
    assert fake.getvalue() == TERMINAL_SANITIZE


def test_sanitize_terminal_is_silent_when_stdout_is_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piped and captured output stays byte-plain: no escape sequences
    leak into a redirected stream."""
    fake = _install_stdout(monkeypatch, tty=False)
    sanitize_terminal()
    assert fake.getvalue() == ""


# ---------------------------------------------------------------------------
# guarded_terminal()
# ---------------------------------------------------------------------------


def test_guard_sanitizes_after_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_stdout(monkeypatch)
    with guarded_terminal():
        fake.write("body")
    assert fake.getvalue() == "body" + TERMINAL_SANITIZE


def test_guard_sanitizes_when_the_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped connection does not always come back as a return: the
    transport may raise on the way out, and the terminal still needs
    restoring."""
    fake = _install_stdout(monkeypatch)
    with pytest.raises(RuntimeError), guarded_terminal():
        raise RuntimeError("connection died")
    assert fake.getvalue() == TERMINAL_SANITIZE


def test_guard_is_a_no_op_when_stdout_is_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_stdout(monkeypatch, tty=False)
    with guarded_terminal():
        pass
    assert fake.getvalue() == ""


def test_guard_survives_a_terminal_that_cannot_be_snapshotted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort, never fatal: a stdin that isn't a real terminal
    (redirected, or captured by a harness) makes the line-discipline
    snapshot impossible, and the attach must still run and still get the
    escape-level cleanup."""
    fake = _install_stdout(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO())
    with guarded_terminal():
        pass
    assert fake.getvalue() == TERMINAL_SANITIZE
