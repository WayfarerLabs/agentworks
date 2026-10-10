"""Owned POSIX PTY input and borrowed terminal mode restoration.

One retained non-main thread owns the native lifetime. No process or relay is
started here. The caller must stop every borrower before that thread releases the
resource; kernel mode restoration does not sanitize an emulator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from threading import current_thread, main_thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from threading import Thread

type _TerminalMode = list[int | list[bytes | int]]


class AcquisitionCleanupFailure(BaseExceptionGroup):
    """Native acquisition cleanup failed; adapters disclose only its category."""


@dataclass
class PosixTerminal:
    """One owned stdin PTY, borrowing the caller's input and output descriptors."""

    _input_fd: int
    _output_fd: int
    _owner_thread: Thread
    _saved_mode: _TerminalMode | None = field(default=None, init=False)
    _acquire_called: bool = field(default=False, init=False)
    _master_fd: int | None = None
    _slave_fd: int | None = None
    _restore_needed: bool = False
    _release_errors: tuple[BaseException, ...] | None = field(default=None, init=False)

    @property
    def settled(self) -> bool:
        """A passive resource or proved release is settled; uncertainty is retained."""
        return not self._acquire_called or self._release_errors == ()

    def acquire(self) -> None:
        """Acquire on the original worker, retaining partial acquisition in this object.

        The caller retains this passive resource before admitting its worker.
        Preparation owns endpoint pairing and exclusive use. Acquisition does
        not flush bytes already queued at the supplied input terminal.
        """
        self._require_owner()
        if current_thread() is main_thread():
            raise RuntimeError("Terminal acquisition requires a retained non-main worker")
        if self._acquire_called:
            raise RuntimeError("Terminal acquisition is not available")
        self._acquire_called = True
        try:
            if os.name != "posix":
                raise OSError("POSIX terminal resources are unavailable on this host")
            import fcntl
            import termios
            import tty

            if fcntl.fcntl(self._input_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY:
                raise OSError("Terminal input is not readable")
            self._saved_mode = termios.tcgetattr(self._input_fd)
            dimensions = termios.tcgetwinsize(self._output_fd)
            self._master_fd, self._slave_fd = os.openpty()
            termios.tcsetattr(self._slave_fd, termios.TCSANOW, self._saved_mode)
            termios.tcsetwinsize(self._slave_fd, dimensions)
            os.set_blocking(self._master_fd, False)
            raw_mode = list(self._saved_mode)
            tty.cfmakeraw(raw_mode)
            # Mark before the syscall: interruption can follow its native effect.
            self._restore_needed = True
            termios.tcsetattr(self._input_fd, termios.TCSANOW, raw_mode)
        except BaseException as error:
            cleanup_errors = self.release()
            if cleanup_errors:
                cleanup = AcquisitionCleanupFailure("Terminal acquisition cleanup failed", cleanup_errors)
                cleanup.__cause__ = error.__cause__
                raise error from cleanup
            raise

    @property
    def master_fd(self) -> int:
        """The owned nonblocking endpoint for the SSH input relay."""
        if self._master_fd is None:
            raise RuntimeError("Terminal master is released")
        return self._master_fd

    @property
    def slave_fd(self) -> int:
        """The owned terminal endpoint to lend to the future client process owner."""
        if self._slave_fd is None:
            raise RuntimeError("Terminal slave is released")
        return self._slave_fd

    def refresh_dimensions(self) -> bool:
        """Copy a changed output-terminal size; the caller owns client notification."""
        self._require_owner()
        import termios

        slave_fd = self.slave_fd
        dimensions = termios.tcgetwinsize(self._output_fd)
        if termios.tcgetwinsize(slave_fd) == dimensions:
            return False
        termios.tcsetwinsize(slave_fd, dimensions)
        return True

    def release(self) -> tuple[BaseException, ...]:
        """After all activity stops, restore input and close owned fds once.

        Every failure is returned, including interruptions. The caller preserves
        control flow and incorporates these errors into its cleanup evidence.
        Repeated release returns the same evidence without retrying uncertain closes.
        """
        self._require_owner()
        if self._release_errors is not None:
            return self._release_errors
        import termios

        errors: list[BaseException] = []
        if self._restore_needed:
            self._restore_needed = False
            try:
                assert self._saved_mode is not None
                termios.tcsetattr(self._input_fd, termios.TCSANOW, self._saved_mode)
            except BaseException as error:
                errors.append(error)
        master_fd, slave_fd = self._master_fd, self._slave_fd
        self._master_fd = self._slave_fd = None
        for fd in (master_fd, slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except BaseException as error:
                    errors.append(error)
        self._release_errors = tuple(errors)
        return self._release_errors

    def _require_owner(self) -> None:
        if current_thread() is not self._owner_thread:
            raise RuntimeError("Terminal mutation requires its retained native worker")
