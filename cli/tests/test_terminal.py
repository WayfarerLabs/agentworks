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
content and ordering, the stdout gate, the POSIX termios restore, and
the Windows console-mode sequence against a stubbed kernel32. What no
test can prove is that the Win32 calls behave as documented on a real
console, since there is no Windows host here; the manual reproduction
above is what covers that.
"""

from __future__ import annotations

import io
import sys

import pytest

from agentworks.terminal import (
    MOUSE_TRACKING_DISABLE,
    TERMINAL_SANITIZE,
    guarded_terminal,
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
        ("\x1b[>4m", "modifyOtherKeys"),
        ("\x1b[<u", "the kitty keyboard stack"),
        ("\x1b[?1049l", "alternate screen"),
        ("\x1b[?1047l", "legacy clearing alternate screen"),
        ("\x1b[?47l", "legacy alternate screen"),
        ("\x1b[?2026l", "synchronized output"),
        ("\x1b[?1l", "application cursor keys"),
        ("\x1b>", "application keypad"),
        ("\x1b[?6l", "origin mode"),
        ("\x1b[4l", "insert mode"),
        ("\x1b[?25h", "cursor visibility"),
        ("\x1b[0 q", "cursor style"),
        ("\x1b[?7h", "autowrap"),
        ("\x1b[0m", "colors and attributes"),
    ],
)
def test_sanitize_clears_each_mode_a_remote_program_can_leave_set(escape: str, what: str) -> None:
    assert escape in TERMINAL_SANITIZE, f"sanitize payload does not restore {what}"


def test_sanitize_grounds_the_parser_first() -> None:
    """A connection that dies mid-write leaves the local parser inside a
    truncated CSI or OSC, which would eat the head of the payload."""
    assert TERMINAL_SANITIZE.startswith("\x18")


def test_sanitize_leaves_the_alternate_screen_before_resetting_margins() -> None:
    """The margin reset must land on the buffer the operator is left
    looking at, not on an alternate screen we are about to discard."""
    assert TERMINAL_SANITIZE.index("\x1b[?1049l") < TERMINAL_SANITIZE.index("\x1b[r")


def test_sanitize_brackets_the_cursor_homing_resets() -> None:
    """DECSTBM and DECOM each home the cursor as a side effect.

    Both are wrapped in DECSC/DECRC so the cursor ends where it started.
    Without the bracket, exiting any attach that was NOT on the
    alternate screen (``vm shell``, ``agent shell``, a clean tmux
    detach) drops the next shell prompt at row 1, on top of the
    operator's scrollback.
    """
    save = TERMINAL_SANITIZE.index("\x1b7")
    restore = TERMINAL_SANITIZE.index("\x1b8")
    assert save < TERMINAL_SANITIZE.index("\x1b[r") < restore
    assert save < TERMINAL_SANITIZE.index("\x1b[?6l") < restore


def test_sanitize_resets_rendering_after_the_cursor_restore() -> None:
    """DECRC restores the saved charset and SGR along with the position,
    so the rendering resets have to follow it or they are undone."""
    restore = TERMINAL_SANITIZE.index("\x1b8")
    assert restore < TERMINAL_SANITIZE.index("\x1b(B")
    assert restore < TERMINAL_SANITIZE.index("\x1b[0m")


def test_sanitize_does_not_hard_reset() -> None:
    """RIS would clear all of this, but it also clears scrollback in
    most terminals, destroying what the operator most wants to read
    after a dropped attach."""
    assert "\x1bc" not in TERMINAL_SANITIZE


# ---------------------------------------------------------------------------
# guarded_terminal(): the escape half
# ---------------------------------------------------------------------------


def test_guard_sanitizes_on_entry_and_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry pass is what recovers a terminal wrecked by an attach
    this process never saw end (the operator closed the tab, or
    agentworks was killed), so the next attach cleans up the last one's
    mess."""
    fake = _install_stdout(monkeypatch)
    with guarded_terminal():
        fake.write("body")
    assert fake.getvalue() == TERMINAL_SANITIZE + "body" + TERMINAL_SANITIZE


def test_guard_sanitizes_when_the_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped connection does not always come back as a return: the
    transport may raise on the way out, and the terminal still needs
    restoring."""
    fake = _install_stdout(monkeypatch)
    with pytest.raises(RuntimeError), guarded_terminal():
        raise RuntimeError("connection died")
    assert fake.getvalue() == TERMINAL_SANITIZE * 2


def test_guard_writes_nothing_when_stdout_is_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piped and captured output stays byte-plain: no escape sequences
    leak into a redirected stream."""
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
    assert fake.getvalue() == TERMINAL_SANITIZE * 2


# ---------------------------------------------------------------------------
# guarded_terminal(): the POSIX line-discipline half
# ---------------------------------------------------------------------------


class _FakeTermios:
    """Stand-in for the ``termios`` module, recording tcsetattr calls."""

    error = OSError
    TCSAFLUSH = 2
    TCSADRAIN = 1

    def __init__(self, *, attrs: list[int] | None = None, readable: bool = True) -> None:
        self._attrs = attrs if attrs is not None else [1, 2, 3]
        self._readable = readable
        self.restored: list[tuple[int, list[int]]] = []

    def tcgetattr(self, fd: int) -> list[int]:
        del fd
        if not self._readable:
            raise self.error("not a terminal")
        return self._attrs

    def tcsetattr(self, fd: int, when: int, attrs: list[int]) -> None:
        del fd
        self.restored.append((when, attrs))


class _FakeStdin(io.StringIO):
    """A stdin with a usable fileno().

    pytest replaces ``sys.stdin`` with an object whose ``fileno()``
    raises, which would make the line-discipline snapshot bail before it
    ever reaches termios and quietly render these tests vacuous.
    """

    def fileno(self) -> int:
        return 0

    def isatty(self) -> bool:
        return True


def _install_termios(monkeypatch: pytest.MonkeyPatch, fake: _FakeTermios) -> None:
    """Inject a fake ``termios`` for the module's lazy ``import termios``,
    plus a stdin the snapshot can actually read a descriptor from."""
    monkeypatch.setitem(sys.modules, "termios", fake)
    monkeypatch.setattr("sys.stdin", _FakeStdin())


def test_posix_restores_the_snapshotted_line_discipline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that fixes the no-echo symptom: whatever tcgetattr saw
    before the attach is handed back to tcsetattr after it."""
    _install_stdout(monkeypatch)
    fake = _FakeTermios(attrs=[9, 9, 9])
    _install_termios(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert fake.restored == [(fake.TCSAFLUSH, [9, 9, 9])]


def test_posix_restore_discards_queued_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCSAFLUSH, not TCSADRAIN: a dropped attach leaves mouse reports
    and partial escape sequences in the input queue, and the shell would
    otherwise read them as keystrokes the moment echo comes back."""
    _install_stdout(monkeypatch)
    fake = _FakeTermios()
    _install_termios(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert [when for when, _ in fake.restored] == [fake.TCSAFLUSH]


def test_posix_skips_restore_when_stdin_is_not_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing was snapshotted, so nothing is pushed back."""
    _install_stdout(monkeypatch)
    fake = _FakeTermios(readable=False)
    _install_termios(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert fake.restored == []


def test_line_discipline_is_restored_even_when_stdout_is_redirected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two halves have different owners. Echo lives on stdin, so
    ``agw vm shell > out.txt`` from a real console still has to get its
    line discipline back even though no escapes are written."""
    fake_out = _install_stdout(monkeypatch, tty=False)
    fake = _FakeTermios()
    _install_termios(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert fake_out.getvalue() == ""
    assert [attrs for _, attrs in fake.restored] == [[1, 2, 3]]


# ---------------------------------------------------------------------------
# guarded_terminal(): the Windows console-mode half
# ---------------------------------------------------------------------------
#
# The operator's real platform, and the one path that cannot be
# exercised for real here (no Windows host in CI or in development).
# These stub kernel32 and pin the call sequence, so the ordering and the
# bail-outs stay locked even though the Win32 calls themselves are only
# proven by the manual reproduction in this module's docstring.

_IN_HANDLE = 100
_OUT_HANDLE = 200
_VT = 0x0004


class _FakeKernel32:
    """Recording stand-in for the kernel32 console surface.

    ``mode=None`` for a handle models a non-console (redirected) stream,
    which is what makes the real ``GetConsoleMode`` fail.
    """

    def __init__(
        self,
        *,
        in_mode: int | None = 0x1F,
        out_mode: int | None = 0x3,
        set_mode_fails: bool = False,
    ) -> None:
        self._modes: dict[int, int | None] = {_IN_HANDLE: in_mode, _OUT_HANDLE: out_mode}
        self._set_mode_fails = set_mode_fails
        self.calls: list[tuple[str, int, int | None]] = []

    def GetStdHandle(self, selector: int) -> int:  # noqa: N802 # mirrors the Win32 name
        return _IN_HANDLE if selector == 0xFFFFFFF6 else _OUT_HANDLE

    def GetConsoleMode(self, handle: int, out_ref: object) -> int:  # noqa: N802
        mode = self._modes.get(handle)
        if mode is None:
            return 0
        out_ref._obj.value = mode  # type: ignore[attr-defined] # ctypes.byref(x)._obj is x
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:  # noqa: N802
        if self._set_mode_fails:
            return 0
        self.calls.append(("SetConsoleMode", handle, mode))
        self._modes[handle] = mode
        return 1

    def FlushConsoleInputBuffer(self, handle: int) -> int:  # noqa: N802
        self.calls.append(("FlushConsoleInputBuffer", handle, None))
        return 1


def _install_windows(monkeypatch: pytest.MonkeyPatch, fake: _FakeKernel32) -> _FakeStdout:
    out = _FakeStdout()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr("agentworks.terminal._kernel32", lambda: fake)
    return out


def test_windows_enables_vt_around_the_write_and_puts_the_mode_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escapes written to a console without ENABLE_VIRTUAL_TERMINAL_PROCESSING
    render as literal garbage, so the bit is forced on for the write and
    the operator's original mode restored right after."""
    fake = _FakeKernel32(out_mode=0x3)  # VT bit clear
    out = _install_windows(monkeypatch, fake)
    with guarded_terminal():
        pass
    out_calls = [c for c in fake.calls if c[1] == _OUT_HANDLE]
    # The entry pass and the exit pass each enable, then restore.
    assert (
        out_calls
        == [
            ("SetConsoleMode", _OUT_HANDLE, 0x3 | _VT),
            ("SetConsoleMode", _OUT_HANDLE, 0x3),
        ]
        * 2
    )
    assert out.getvalue() == TERMINAL_SANITIZE * 2


def test_windows_leaves_vt_alone_when_already_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows Terminal already has the bit set; touching the mode there
    would be pointless churn on the operator's console."""
    fake = _FakeKernel32(out_mode=0x3 | _VT)
    out = _install_windows(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert [c for c in fake.calls if c[1] == _OUT_HANDLE] == []
    assert out.getvalue() == TERMINAL_SANITIZE * 2


def test_windows_declines_to_write_when_vt_cannot_be_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy console that rejects the bit would render the payload as
    visible garbage, making a bad terminal worse. Nothing interpreted the
    sequences that wrecked it either, so there is nothing to undo."""
    fake = _FakeKernel32(out_mode=0x3, set_mode_fails=True)
    out = _install_windows(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert out.getvalue() == ""


def test_windows_restores_console_input_mode_then_flushes_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The input mode carries ENABLE_ECHO_INPUT and ENABLE_LINE_INPUT,
    whose loss is the no-echo symptom. The flush drops mouse reports
    queued while the console was misconfigured, and has to come after
    the restore so nothing arrives under the wrong discipline between
    the two."""
    fake = _FakeKernel32(in_mode=0x1F)
    _install_windows(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert [c for c in fake.calls if c[1] == _IN_HANDLE] == [
        ("SetConsoleMode", _IN_HANDLE, 0x1F),
        ("FlushConsoleInputBuffer", _IN_HANDLE, None),
    ]


def test_windows_skips_input_restore_when_stdin_is_not_a_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirected stdin has no console mode to snapshot; the escape
    half must still run."""
    fake = _FakeKernel32(in_mode=None)
    out = _install_windows(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert [c for c in fake.calls if c[1] == _IN_HANDLE] == []
    assert out.getvalue() == TERMINAL_SANITIZE * 2


def test_windows_falls_back_to_a_plain_write_without_a_console_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdout claims to be a TTY but there is no Win32 console behind it
    (an MSYS/mintty pty), so the escapes go out unadorned."""
    fake = _FakeKernel32(out_mode=None)
    out = _install_windows(monkeypatch, fake)
    with guarded_terminal():
        pass
    assert out.getvalue() == TERMINAL_SANITIZE * 2
