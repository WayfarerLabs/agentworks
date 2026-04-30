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


