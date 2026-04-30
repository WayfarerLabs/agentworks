"""Output contract between business logic and the presentation layer.

Business logic reports data through the handler (info, detail, warn, progress)
and signals errors by raising exceptions from the hierarchy below. The
presentation layer (CLI, web, test) sets the handler implementation and
catches exceptions.

Business logic must never import typer, call sys.exit, or format output.
"""

from __future__ import annotations

import sys
import time
from typing import Protocol

# ---------------------------------------------------------------------------
# Progress handle
# ---------------------------------------------------------------------------


class Progress(Protocol):
    """Handle returned by OutputHandler.progress() for tracking a long operation."""

    def update(self, current: int | None = None, message: str | None = None) -> None:
        """Report progress. current is meaningful when total was provided."""
        ...

    def done(self, message: str | None = None) -> None:
        """Mark the operation as complete."""
        ...


# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


class OutputHandler(Protocol):
    """Contract for all user-facing output from business logic.

    Implementations decide rendering: terminal, web, test capture, etc.
    """

    def info(self, message: str) -> None:
        """One-shot status message (top-level)."""
        ...

    def detail(self, message: str) -> None:
        """Sub-step or detail message (indented under a prior info/progress)."""
        ...

    def warn(self, message: str) -> None:
        """Non-fatal warning."""
        ...

    def prompt_bool(self, message: str, default: bool = False) -> bool:
        """Present a yes/no question. Returns True for yes, False for no."""
        ...

    def choose(self, message: str, options: list[str]) -> int:
        """Present a list of options. Returns the index of the selected option."""
        ...

    def pause(self, message: str) -> None:
        """Wait for user acknowledgment (press Enter)."""
        ...

    def prompt(self, label: str, default: str | None = None) -> str:
        """Collect a string value. If default is provided and user enters nothing, returns default."""
        ...

    def prompt_secret(self, label: str, hint: str | None = None) -> str:
        """Collect a secret value with masked input. Rejects empty values."""
        ...

    def progress(self, label: str, total: int | None = None) -> Progress:
        """Start a tracked operation. Returns a Progress handle.

        If total is provided, the operation is determinate (percentage-based).
        Otherwise it is indeterminate (elapsed time only).
        """
        ...


# ---------------------------------------------------------------------------
# Default handler (plain print, no terminal magic)
# ---------------------------------------------------------------------------


class _DefaultProgress:
    def __init__(self, label: str, total: int | None = None) -> None:
        self._label = label
        self._total = total
        self._start = time.monotonic()

    def update(self, current: int | None = None, message: str | None = None) -> None:
        parts = [f"  {self._label}..."]
        if current is not None and self._total is not None and self._total > 0:
            pct = current / self._total * 100
            parts.append(f" {pct:.0f}% ({current}/{self._total})")
        if message:
            parts.append(f" {message}")
        print("".join(parts))

    def done(self, message: str | None = None) -> None:
        elapsed = time.monotonic() - self._start
        suffix = f" {message}" if message else ""
        print(f"  {self._label} done ({elapsed:.0f}s){suffix}")


class _DefaultHandler:
    def info(self, message: str) -> None:
        print(message)

    def detail(self, message: str) -> None:
        print(f"  {message}")

    def warn(self, message: str) -> None:
        print(f"Warning: {message}", file=sys.stderr)

    def prompt_bool(self, message: str, default: bool = False) -> bool:
        try:
            suffix = " [Y/n]" if default else " [y/N]"
            response = input(message + suffix + " ").strip().lower()
            if not response:
                return default
            return response in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def choose(self, message: str, options: list[str]) -> int:
        try:
            print(message)
            for i, option in enumerate(options, 1):
                print(f"  {i}) {option}")
            while True:
                try:
                    choice = int(input("Choice: "))
                    if 1 <= choice <= len(options):
                        return choice - 1
                except ValueError:
                    pass
                print(f"Invalid choice. Enter 1-{len(options)}.")
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def pause(self, message: str) -> None:
        try:
            input(message)
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def prompt(self, label: str, default: str | None = None) -> str:
        try:
            suffix = f" [{default}]" if default else ""
            value = input(f"{label}{suffix}: ").strip()
            return value if value else (default or "")
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def prompt_secret(self, label: str, hint: str | None = None) -> str:
        import getpass

        try:
            if hint:
                print(f"  {hint}", file=sys.stderr)
            while True:
                value = getpass.getpass(f"{label}: ")
                if value.strip():
                    return value
                print("(empty, try again)", file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def progress(self, label: str, total: int | None = None) -> Progress:
        print(f"  {label}...")
        return _DefaultProgress(label, total)


# ---------------------------------------------------------------------------
# Module API
# ---------------------------------------------------------------------------

_handler: OutputHandler = _DefaultHandler()


def info(message: str) -> None:
    """Emit a top-level status message."""
    _handler.info(message)


def detail(message: str) -> None:
    """Emit an indented detail/sub-step message."""
    _handler.detail(message)


def warn(message: str) -> None:
    """Emit a non-fatal warning."""
    _handler.warn(message)


def prompt_bool(message: str, default: bool = False) -> bool:
    """Present a yes/no question. Returns True for yes, False for no."""
    return _handler.prompt_bool(message, default)


def choose(message: str, options: list[str]) -> int:
    """Present a list of options. Returns the index of the selected option."""
    return _handler.choose(message, options)


def pause(message: str) -> None:
    """Wait for user acknowledgment (press Enter)."""
    _handler.pause(message)


def prompt(label: str, default: str | None = None) -> str:
    """Collect a string value. Returns default if user enters nothing."""
    return _handler.prompt(label, default)


def prompt_secret(label: str, hint: str | None = None) -> str:
    """Collect a secret value with masked input. Rejects empty values."""
    return _handler.prompt_secret(label, hint)


def progress(label: str, total: int | None = None) -> Progress:
    """Start a tracked operation. Returns a Progress handle."""
    return _handler.progress(label, total)


def set_handler(handler: OutputHandler) -> None:
    """Replace the global output handler.

    Call from the application entrypoint to route output through the appropriate
    mechanism (typer.echo for CLI, websocket for web, list collector for tests).
    """
    global _handler
    _handler = handler


def get_handler() -> OutputHandler:
    """Return the current output handler."""
    return _handler


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class AgentworksError(Exception):
    """Base exception for all agentworks business logic errors.

    The presentation layer catches this (and subclasses) at the entrypoint
    and decides how to render the error.
    """


class VMError(AgentworksError):
    """Error related to VM operations."""


class WorkspaceError(AgentworksError):
    """Error related to workspace operations."""


class AgentError(AgentworksError):
    """Error related to agent operations."""


class SessionError(AgentworksError):
    """Error related to session operations."""


class ConnectivityError(AgentworksError):
    """Error related to network, SSH, or Tailscale connectivity."""


class BackupError(AgentworksError):
    """Error related to backup-specific failures."""


class ValidationError(AgentworksError):
    """Invalid user input (name, argument, etc.)."""


class UserAbort(AgentworksError):
    """Raised when the user declines a confirmation prompt."""


