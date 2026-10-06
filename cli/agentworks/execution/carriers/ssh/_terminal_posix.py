"""Owned POSIX PTY input and borrowed terminal mode restoration.

No process or relay is started here. The caller must stop every user of these
descriptors before release; kernel mode restoration does not sanitize an emulator.
"""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass, field

type _TerminalMode = list[int | list[bytes | int]]


@dataclass
class PosixTerminal:
    """One owned stdin PTY, borrowing the caller's input and output descriptors."""

    _input_fd: int
    _output_fd: int
    _saved_mode: _TerminalMode
    _master_fd: int | None = None
    _slave_fd: int | None = None
    _restore_needed: bool = False
    _release_errors: tuple[BaseException, ...] | None = field(default=None, init=False)

    @classmethod
    def acquire(cls, input_fd: int, output_fd: int) -> PosixTerminal:
        """Admit supplied native fds and make input raw without flushing queued bytes.

        Preparation owns endpoint pairing and exclusive use. Admission checks the
        actual supplied descriptors, never process-global standard streams.
        """
        if os.name != "posix":
            raise OSError("POSIX terminal resources are unavailable on this host")
        import fcntl
        import termios
        import tty

        if fcntl.fcntl(input_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY:
            raise OSError("Terminal input is not readable")
        saved_mode: _TerminalMode = termios.tcgetattr(input_fd)
        termios.tcgetattr(output_fd)
        dimensions = termios.tcgetwinsize(output_fd)
        terminal = cls(input_fd, output_fd, saved_mode)
        try:
            terminal._master_fd, terminal._slave_fd = os.openpty()
            termios.tcsetattr(terminal._slave_fd, termios.TCSANOW, saved_mode)
            termios.tcsetwinsize(terminal._slave_fd, dimensions)
            os.set_blocking(terminal._master_fd, False)
            raw_mode = deepcopy(saved_mode)
            tty.cfmakeraw(raw_mode)
            # Mark before the syscall: interruption can follow its native effect.
            terminal._restore_needed = True
            termios.tcsetattr(input_fd, termios.TCSANOW, raw_mode)
        except BaseException as error:
            cleanup_errors = terminal.release()
            if cleanup_errors:
                cleanup = BaseExceptionGroup("Terminal acquisition cleanup failed", cleanup_errors)
                cleanup.__cause__ = error.__cause__
                raise error from cleanup
            raise
        return terminal

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
        if self._release_errors is not None:
            return self._release_errors
        import termios

        errors: list[BaseException] = []
        if self._restore_needed:
            self._restore_needed = False
            try:
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
