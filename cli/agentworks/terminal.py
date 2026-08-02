"""Local terminal protection around interactive attaches.

An interactive attach hands the operator's terminal to a remote
full-screen program (tmux, most often), which reconfigures that
terminal by writing DECSET escape sequences down the connection:
alternate screen, mouse reporting, bracketed paste, focus reporting,
application cursor keys, a scroll region. Those modes live in the
operator's local terminal emulator, not on the VM, and the remote
program only undoes them when its client detaches cleanly. A
connection that dies mid-attach (laptop sleeps, lid closes, network
drops) never delivers the reset bytes, so the local terminal is left
holding them. Mouse reporting is the loudest survivor: with `?1006h`
still set, every subsequent click emits an SGR mouse report
(`^[[<0;12;34M`) into whatever now reads stdin.

Separately, the SSH client puts the local terminal into raw mode (echo
off, no line buffering) for the duration of the attach and restores it
on exit. That restore is best-effort and does not reliably land when
the connection dies inside a blocking read, which leaves the terminal
not echoing what the operator types. Escape sequences cannot fix this
half: it is the terminal's line discipline (termios on POSIX, console
mode on Windows), not emulator state.

`guarded_terminal()` handles both. It snapshots the line discipline
before the attach and restores it afterward, then writes the sanitize
sequence that clears the emulator modes a remote program can leave
behind. Both halves run unconditionally on the way out, including when
the attach raises: a clean detach has already reset everything, and
every sequence here is idempotent, so there is nothing to gain from
trying to distinguish a clean exit from a dropped one.

The guard only helps when this process regains control. An attach the
operator kills at the window-manager level (closing the tab, killing
the agentworks process) leaves nothing running to clean up, which is
why the sanitize sequence is also exposed on its own as
`sanitize_terminal()`.
"""

from __future__ import annotations

import contextlib
import io
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

# DECRST reset disabling every common xterm mouse-reporting mode: 1000
# (X11), 1002 (button-event), 1003 (any-motion), 1006 (SGR, the
# ``^[[<..M`` wire form), and 1015 (urxvt). 1003 is included so a
# full-screen TUI that left any-motion tracking on keeps no reports
# flowing after the reset. 1005 (UTF-8 mouse) is intentionally
# excluded: a legacy encoding superseded by 1006.
MOUSE_TRACKING_DISABLE = "\x1b[?1000;1002;1003;1006;1015l"

# Everything a remote full-screen program can leave set on the local
# terminal, turned back off.
#
# Ordering is deliberate in one place: the scroll-region reset (DECSTBM)
# homes the cursor as a side effect, so it runs BEFORE leaving the
# alternate screen. `?1049l` then restores the pre-attach cursor
# position along with the primary screen buffer, absorbing that side
# effect. Reversed, the homing would land on the primary screen and the
# next shell prompt would draw over the operator's scrollback.
#
# RIS (`\x1bc`) is deliberately not used. A hard reset does clear all
# of this, but it also clears scrollback in most terminals (Windows
# Terminal included), destroying the context the operator most wants
# to keep after a dropped attach.
TERMINAL_SANITIZE = (
    MOUSE_TRACKING_DISABLE
    + "\x1b[?2004l"  # bracketed paste off
    + "\x1b[?1004l"  # focus reporting off
    + "\x1b[?1l"  # normal (not application) cursor keys
    + "\x1b>"  # numeric (not application) keypad
    + "\x1b[r"  # scroll region back to the full screen
    + "\x1b[?1049l"  # leave the alternate screen buffer
    + "\x1b[?7h"  # autowrap on
    + "\x1b[?25h"  # cursor visible
    + "\x1b(B"  # G0 charset back to ASCII
    + "\x0f"  # shift in (select G0), undoing DEC line drawing
    + "\x1b[0m"  # colors and attributes back to default
)

# GetStdHandle selectors, as unsigned DWORDs. The Win32 headers spell
# these -10 and -11; ctypes will not narrow a negative int into a DWORD,
# so the two's-complement values are written out directly.
_STD_INPUT_HANDLE = 0xFFFFFFF6
_STD_OUTPUT_HANDLE = 0xFFFFFFF5

# Console output mode bit that makes the console interpret VT escape
# sequences rather than printing them literally. Windows Terminal has it
# on by default, but ssh.exe manipulates console modes during an attach,
# so the restore path sets it explicitly before writing the sanitize
# sequence. Writing escapes to a console without it would print visible
# garbage, making a bad terminal worse.
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

# Snapshot/restore failures are never worth failing an attach over: the
# guard is a best-effort cleanup wrapped around the operator's real
# work. These are the ways the platform calls can fail when stdio is
# redirected, captured by a test harness, or not a console at all.
_TTY_ERRORS = (OSError, ValueError, AttributeError, io.UnsupportedOperation)


def sanitize_terminal() -> None:
    """Write `TERMINAL_SANITIZE` to stdout when stdout is a terminal.

    Safe to call at any time: every sequence in the payload is
    idempotent, and the whole thing is skipped when stdout is not a
    TTY so piped and captured output stays byte-plain.
    """
    if not _stdout_is_tty():
        return
    if sys.platform == "win32":
        _windows_write_sanitize()
        return
    _write_sanitize()


@contextlib.contextmanager
def guarded_terminal() -> Iterator[None]:
    """Snapshot local terminal state, run the body, then restore.

    A no-op when stdout is not a terminal. The restore runs in a
    `finally`, so an attach that raises is cleaned up the same as one
    that returns.
    """
    if not _stdout_is_tty():
        yield
        return

    snapshot = _snapshot()
    try:
        yield
    finally:
        _restore(snapshot)


# -- internals ---------------------------------------------------------------


def _stdout_is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except _TTY_ERRORS:
        return False


def _write_sanitize() -> None:
    try:
        sys.stdout.write(TERMINAL_SANITIZE)
        sys.stdout.flush()
    except _TTY_ERRORS:
        pass


def _snapshot() -> Any | None:
    """Capture the local terminal's line discipline, or None if unavailable."""
    if sys.platform == "win32":
        return _windows_snapshot()
    return _posix_snapshot()


def _restore(snapshot: Any | None) -> None:
    """Restore the line discipline, then clear leftover emulator modes."""
    if sys.platform == "win32":
        _windows_restore(snapshot)
        return
    _posix_restore(snapshot)
    _write_sanitize()


# -- POSIX -------------------------------------------------------------------


def _posix_snapshot() -> Any | None:
    try:
        import termios
    except ImportError:
        return None
    # termios.error is its own class rather than an OSError subclass, so
    # it has to be named alongside the shared stdio failures.
    try:
        return termios.tcgetattr(sys.stdin.fileno())
    except (*_TTY_ERRORS, termios.error):
        return None


def _posix_restore(snapshot: Any | None) -> None:
    if snapshot is None:
        return
    try:
        import termios
    except ImportError:
        return
    try:
        # TCSADRAIN, not TCSANOW: let any output the dying attach already
        # queued drain before the mode flips, so the restore doesn't
        # interleave with it.
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, snapshot)
    except (*_TTY_ERRORS, termios.error):
        pass


# -- Windows -----------------------------------------------------------------


def _kernel32() -> Any | None:
    """Bind the kernel32 console-mode calls, or None if unavailable.

    Explicit `restype`/`argtypes` are required, not cosmetic: ctypes
    defaults `GetStdHandle` to returning a C `int`, which truncates a
    64-bit HANDLE and yields an invalid handle on every 64-bit Windows.
    """
    # Narrows for the type checker as well as guarding at runtime:
    # `ctypes.WinDLL` and `ctypes.wintypes` only exist on Windows.
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
    except (ImportError, AttributeError, OSError):
        return None
    return kernel32


def _windows_snapshot() -> tuple[Any, int, Any, int] | None:
    """Capture (stdin handle, stdin mode, stdout handle, stdout mode).

    Returns None unless BOTH handles are real consoles: a redirected
    stream makes `GetConsoleMode` fail, and there is nothing to restore.
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        in_handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        out_handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        in_mode = wintypes.DWORD()
        out_mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(in_handle, ctypes.byref(in_mode)):
            return None
        if not kernel32.GetConsoleMode(out_handle, ctypes.byref(out_mode)):
            return None
    except (*_TTY_ERRORS, ImportError):
        return None
    return in_handle, in_mode.value, out_handle, out_mode.value


def _windows_restore(snapshot: tuple[Any, int, Any, int] | None) -> None:
    """Put the console modes back, sanitizing on the way.

    The write is sandwiched between enabling VT processing and putting
    the saved output mode back, so the escape sequence is interpreted
    even if the attach left VT processing off. Input mode is restored
    last: that is the one carrying `ENABLE_ECHO_INPUT` and
    `ENABLE_LINE_INPUT`, whose loss is the no-echo symptom.
    """
    if snapshot is None:
        # No console to restore, but stdout claimed to be a TTY (a
        # mintty/MSYS pty, say), so the escapes are still worth writing.
        _write_sanitize()
        return

    kernel32 = _kernel32()
    if kernel32 is None:
        _write_sanitize()
        return

    in_handle, in_mode, out_handle, out_mode = snapshot
    try:
        kernel32.SetConsoleMode(out_handle, out_mode | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        _write_sanitize()
        kernel32.SetConsoleMode(out_handle, out_mode)
        kernel32.SetConsoleMode(in_handle, in_mode)
    except (*_TTY_ERRORS, ImportError):
        pass


def _windows_write_sanitize() -> None:
    """Standalone sanitize on Windows: enable VT, write, put the mode back."""
    kernel32 = _kernel32()
    if kernel32 is None:
        _write_sanitize()
        return
    try:
        import ctypes
        from ctypes import wintypes

        out_handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        out_mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(out_handle, ctypes.byref(out_mode)):
            _write_sanitize()
            return
        kernel32.SetConsoleMode(out_handle, out_mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        _write_sanitize()
        kernel32.SetConsoleMode(out_handle, out_mode.value)
    except (*_TTY_ERRORS, ImportError):
        _write_sanitize()
