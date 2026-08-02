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
mode on Windows), not emulator state. The two halves also have
different owners, so they are gated separately: the line discipline
belongs to stdin, the escape sequences to stdout.

`guarded_terminal()` handles both, and sanitizes on the way IN as well
as on the way out. The entry pass is what recovers a terminal wrecked
by an attach this process never saw end (the operator closed the tab,
or the agentworks process was killed outright, so no exit pass ran):
the next attach cleans up the mess left by the last one. The exit pass
runs unconditionally, including when the attach raises, because a clean
detach has already reset everything and every sequence here is
idempotent, so there is nothing to gain from trying to distinguish a
clean exit from a dropped one.

Scope note: the other places agentworks hands the terminal to a
full-screen program are `agw config edit` and `agw resource edit`,
which spawn a local editor. Those are deliberately not guarded. A local
editor does not die from a network drop, and when it does die the
shell's own job control restores the terminal.
"""

from __future__ import annotations

import contextlib
import io
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    # POSIX snapshots are the termios attribute list; Windows snapshots
    # are (console input handle, console input mode). Both are opaque to
    # everything but the platform pair that produced them.
    LineDiscipline = list[Any] | tuple[Any, int]

# DECRST reset disabling every common xterm mouse-reporting mode: 1000
# (X11), 1002 (button-event), 1003 (any-motion), 1006 (SGR, the
# ``^[[<..M`` wire form), and 1015 (urxvt). 1003 is included so a
# full-screen TUI that left any-motion tracking on keeps no reports
# flowing after the reset. 1005 (UTF-8 mouse) is intentionally
# excluded: a legacy encoding superseded by 1006.
MOUSE_TRACKING_DISABLE = "\x1b[?1000;1002;1003;1006;1015l"

# Everything a remote full-screen program can leave set on the local
# terminal, turned back off. Modelled on what tmux itself emits when a
# client detaches cleanly, which is exactly the payload that goes
# missing when the connection dies instead.
#
# RIS (`\x1bc`) would clear all of this in one byte pair and is
# deliberately not used: a hard reset also clears scrollback in most
# terminals (Windows Terminal included), destroying the context the
# operator most wants to keep after a dropped attach.
TERMINAL_SANITIZE = (
    # Ground the parser first. A connection that died mid-write can
    # leave the local terminal parked inside a truncated CSI or OSC,
    # which would otherwise consume the head of this payload as
    # parameters or string content. CAN is a no-op in ground state.
    "\x18"
    # -- Input reporting. Each of these turns ordinary user input into
    # escape sequences delivered to whatever reads stdin next, which is
    # what makes a wrecked tab feel possessed.
    + MOUSE_TRACKING_DISABLE
    + "\x1b[?2004l"  # bracketed paste off
    + "\x1b[?1004l"  # focus reporting off
    + "\x1b[>4m"  # modifyOtherKeys off (else plain keys arrive as CSI 27;..~)
    + "\x1b[<u"  # pop the kitty keyboard-protocol stack
    + "\x1b[?1l"  # normal (not application) cursor keys
    + "\x1b>"  # numeric (not application) keypad
    # -- Screen buffer. Leaving the alternate screen comes before the
    # margin reset below so that reset lands on the buffer the operator
    # is actually left looking at.
    + "\x1b[?2026l"  # synchronized output off (a mid-frame drop otherwise freezes the display)
    + "\x1b[?47l"  # legacy alternate screen off
    + "\x1b[?1047l"  # legacy alternate screen off (clearing variant)
    + "\x1b[?1049l"  # alternate screen off
    # -- Margins. DECSTBM and DECOM each home the cursor as a side
    # effect. On the primary screen that would drop the next shell
    # prompt at row 1, on top of the operator's scrollback, so the pair
    # is bracketed in DECSC/DECRC and the cursor ends where it started.
    # This is correct on either buffer, which matters because plenty of
    # guarded paths (`vm shell`, `agent shell`, any clean tmux detach)
    # are on the primary screen by the time we get here.
    + "\x1b7"  # save cursor
    + "\x1b[r"  # scroll region back to the full screen
    + "\x1b[?6l"  # origin mode off
    + "\x1b8"  # restore cursor
    # -- Rendering. These follow the DECRC above, which restores the
    # saved charset and attributes along with the position and would
    # otherwise undo them.
    + "\x1b[?7h"  # autowrap on
    + "\x1b[4l"  # replace (not insert) mode
    + "\x1b[?25h"  # cursor visible
    + "\x1b[0 q"  # cursor style back to the terminal default
    + "\x1b]112\x07"  # cursor color back to the terminal default
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
# so the emit path sets it explicitly and declines to write at all if it
# cannot: escapes written to a console without this bit render as
# visible `<-[?1000l` garbage, which would make a bad terminal worse.
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

# Snapshot/restore failures are never worth failing an attach over: this
# is a best-effort cleanup wrapped around the operator's real work, and
# the exit half runs inside a `finally` where an escaping exception
# would mask the attach's actual outcome. These are the ways the
# platform calls fail when stdio is redirected, captured by a test
# harness, or not a terminal at all.
_TTY_ERRORS = (OSError, ValueError, AttributeError, io.UnsupportedOperation)


@contextlib.contextmanager
def guarded_terminal() -> Iterator[None]:
    """Protect local terminal state across an interactive attach.

    Sanitizes on entry (recovering a terminal a previous attach could
    not clean up after), snapshots the line discipline, and on the way
    out restores that discipline and sanitizes again. Every step is
    individually best-effort and independently gated, so a redirected
    stdout still gets its stdin discipline restored and vice versa.
    """
    _emit_sanitize()
    snapshot = _snapshot_line_discipline()
    try:
        yield
    finally:
        _restore_line_discipline(snapshot)
        _emit_sanitize()


# -- escape-sequence half (gated on stdout) ----------------------------------


def _emit_sanitize() -> None:
    """Write `TERMINAL_SANITIZE` to stdout when stdout is a terminal.

    Not gated on ``output.non_interactive()``, unlike the confirm
    prompt's narrower mouse reset in ``cli/_typer_output.py``. That gate
    exists to keep scripted output byte-plain; here the write is already
    conditioned on stdout being a real terminal, and a real terminal
    that just hosted an attach needs restoring no matter which flags the
    invocation carried.
    """
    if not _stdout_is_tty():
        return
    if sys.platform == "win32":
        _windows_emit_sanitize()
        return
    _write_sanitize()


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


# -- line-discipline half (gated on stdin) -----------------------------------


def _snapshot_line_discipline() -> LineDiscipline | None:
    """Capture stdin's line discipline, or None if it is not a terminal.

    No explicit ``isatty`` gate: the platform call is the gate, failing
    exactly when there is nothing to restore.
    """
    if sys.platform == "win32":
        return _windows_snapshot()
    return _posix_snapshot()


def _restore_line_discipline(snapshot: LineDiscipline | None) -> None:
    if snapshot is None:
        return
    if sys.platform == "win32":
        _windows_restore(snapshot)
        return
    _posix_restore(snapshot)


def _posix_snapshot() -> LineDiscipline | None:
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


def _posix_restore(snapshot: LineDiscipline) -> None:
    if not isinstance(snapshot, list):
        return  # a Windows snapshot; wrong platform pair, nothing to apply
    try:
        import termios
    except ImportError:
        return
    try:
        # TCSAFLUSH rather than TCSADRAIN: both let queued output drain,
        # but TCSAFLUSH additionally discards pending input. That is
        # what we want here, because a dropped attach typically leaves
        # mouse reports and half-finished escape sequences sitting in
        # the input queue, and the shell would otherwise read them as
        # keystrokes the moment echo comes back.
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, snapshot)
    except (*_TTY_ERRORS, termios.error):
        pass


# -- Windows -----------------------------------------------------------------


def _kernel32() -> Any | None:
    """Bind the kernel32 console calls, or None if unavailable.

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

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        kernel32.FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]
        kernel32.FlushConsoleInputBuffer.restype = wintypes.BOOL
    except (ImportError, AttributeError, OSError):
        return None
    return kernel32


def _windows_snapshot() -> LineDiscipline | None:
    """Capture (console input handle, console input mode).

    The input mode is the one carrying ENABLE_ECHO_INPUT and
    ENABLE_LINE_INPUT, whose loss is the no-echo symptom. Output mode is
    not snapshotted here: the only reason to touch it is the VT dance in
    `_windows_emit_sanitize`, which saves and restores it itself.
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
    except (*_TTY_ERRORS, ImportError, _ctypes_argument_error()):
        return None
    return handle, mode.value


def _windows_restore(snapshot: LineDiscipline) -> None:
    if not isinstance(snapshot, tuple):
        return  # a POSIX snapshot; wrong platform pair, nothing to apply
    kernel32 = _kernel32()
    if kernel32 is None:
        return
    handle, mode = snapshot
    try:
        kernel32.SetConsoleMode(handle, mode)
        # Drop input queued while the terminal was misconfigured (stray
        # mouse reports, partial escape sequences), matching the
        # TCSAFLUSH discard on the POSIX side.
        kernel32.FlushConsoleInputBuffer(handle)
    except (*_TTY_ERRORS, _ctypes_argument_error()):
        pass


def _windows_emit_sanitize() -> None:
    """Write the payload with VT processing guaranteed on, then restore.

    Declines to write at all if VT processing is off and cannot be
    turned on: on such a console the escapes would render as literal
    garbage, and nothing would have interpreted the sequences that
    wrecked the terminal in the first place either, so there is nothing
    to clean up.
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        # stdout claims to be a TTY but there is no Win32 console behind
        # it (an MSYS/mintty pty, say), so write plainly.
        _write_sanitize()
        return

    try:
        import ctypes
        from ctypes import wintypes

        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            _write_sanitize()  # not a console handle; see above
            return

        original = mode.value
        already_on = bool(original & _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        if not already_on and not kernel32.SetConsoleMode(handle, original | _ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            return  # legacy console: writing escapes would only add garbage
        try:
            _write_sanitize()
        finally:
            if not already_on:
                kernel32.SetConsoleMode(handle, original)
    except (*_TTY_ERRORS, ImportError, _ctypes_argument_error()):
        pass


def _ctypes_argument_error() -> type[BaseException]:
    """``ctypes.ArgumentError`` is neither an OSError nor a ValueError.

    A bad handle or an argtypes mismatch would otherwise escape a
    best-effort cleanup running inside a ``finally`` and mask the
    attach's real outcome. Resolved lazily so this module still imports
    on a build without ctypes.
    """
    try:
        import ctypes
    except ImportError:  # pragma: no cover - ctypes ships with CPython
        return _Unraisable
    return ctypes.ArgumentError


class _Unraisable(BaseException):
    """Placeholder that is never raised, for the no-ctypes fallback."""
