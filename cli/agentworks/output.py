"""User-facing output primitives.

Provides a decoupled warning mechanism that can be used at any depth in the
code without importing typer or knowing about the presentation layer. The
default handler prints to stderr. The CLI entrypoint (or a test harness, or
a future server) can replace it via ``set_warn_handler``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

WarnHandler = Callable[[str], None]


def _default_warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


_warn_handler: WarnHandler = _default_warn


def warn(message: str) -> None:
    """Emit a user-facing warning via the current handler."""
    _warn_handler(message)


def get_warn_handler() -> WarnHandler:
    """Return the current warning handler."""
    return _warn_handler


def set_warn_handler(handler: WarnHandler) -> None:
    """Replace the global warning handler.

    Call this from the application entrypoint to route warnings through the
    appropriate output mechanism (e.g., typer.echo for a CLI, a log stream
    for a server, or a list collector for tests).
    """
    global _warn_handler
    _warn_handler = handler
