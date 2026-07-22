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
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

# Re-export the kind-based exception hierarchy from agentworks.errors so existing
# `from agentworks.output import X` users keep working. New code should prefer
# importing from agentworks.errors directly. The __all__ below marks these
# names as explicit re-exports for mypy strict (no_implicit_reexport).
from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    AuthorizationError,
    BackupError,
    BrokenStateError,
    ConfigError,
    ConnectivityError,
    ExternalError,
    NotFoundError,
    ProvisioningError,
    SecretUnavailableError,
    StateError,
    UserAbort,
    ValidationError,
)

__all__ = [
    "AgentworksError",
    "AlreadyExistsError",
    "AuthorizationError",
    "BackupError",
    "BrokenStateError",
    "ConfigError",
    "ConnectivityError",
    "ExternalError",
    "NotFoundError",
    "ProvisioningError",
    "SecretUnavailableError",
    "StateError",
    "UserAbort",
    "ValidationError",
]

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

    def detail(self, message: str, indent: int = 1) -> None:
        """Sub-step or detail message. indent controls nesting depth (1 = 2 spaces, 2 = 4, etc.)."""
        ...

    def warn(self, message: str) -> None:
        """Non-fatal warning."""
        ...

    def confirm(self, message: str, default: bool = False) -> bool:
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

    def detail(self, message: str, indent: int = 1) -> None:
        print(f"{'  ' * indent}{message}")

    def warn(self, message: str) -> None:
        print(f"Warning: {message}", file=sys.stderr)

    def confirm(self, message: str, default: bool = False) -> bool:
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


def phase(title: str) -> None:
    """Emit a delineated phase header (e.g. 'Preflight', 'Provisioning').

    A thin convenience over :func:`info` (a blank line then a bracketed
    title), so a multi-phase command reads as clearly separated
    sections. Follow it with :func:`detail` lines naming the resources
    the phase touches.
    """
    _handler.info("")
    _handler.info(f"=== {title} ===")


def count(n: int, noun: str, plural: str | None = None) -> str:
    """Format a count with a correctly pluralized noun.

    ``count(1, "package") -> "1 package"``; ``count(3, "package") ->
    "3 packages"``. Pass ``plural`` for irregular nouns. Keeps operator
    strings grammatical without the ``(s)`` shortcut.
    """
    word = noun if n == 1 else (plural or f"{noun}s")
    return f"{n} {word}"


def render_table(
    headers: list[str],
    rows: Sequence[Sequence[str]],
    *,
    max_col_width: int = 20,
) -> list[str]:
    """Render a left-justified table into a list of lines.

    Returns the header row, a dashed rule line matching its width, then
    one line per row. Columns are separated by two spaces. Each column
    sizes to its widest cell (header included) but is capped at
    ``max_col_width``; a cell longer than the cap is truncated to
    ``cell[: max_col_width - 3] + "..."`` (so a 21-char cell becomes 20
    chars), while a cell of exactly the cap is left intact. Columns whose
    content all fits under the cap keep their natural, narrower width.

    The caller emits each returned line via :func:`info`.
    """
    columns = list(zip(headers, *rows, strict=True))
    widths = [min(max_col_width, max(len(cell) for cell in column)) for column in columns]

    def _line(cells: Sequence[str]) -> str:
        rendered = (truncate(cell, width).ljust(width) for cell, width in zip(cells, widths, strict=True))
        return "  ".join(rendered).rstrip()

    header_line = _line(headers)
    lines = [header_line, "-" * len(header_line)]
    lines.extend(_line(row) for row in rows)
    return lines


def truncate(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` with a trailing ``...`` when it
    overflows; leave text that already fits untouched. When ``width`` is
    too small to fit the ellipsis (<= 3), hard-truncate to ``width`` so the
    result never exceeds it.

    The shared cell-truncation helper: used by :func:`render_table` and by
    bespoke table renderers (e.g. the secret list view) that cap their own
    columns."""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def info(message: str) -> None:
    """Emit a top-level status message."""
    _handler.info(message)


def detail(message: str, indent: int = 1) -> None:
    """Emit an indented detail/sub-step message. indent controls nesting depth."""
    _handler.detail(message, indent)


def warn(message: str) -> None:
    """Emit a non-fatal warning."""
    _handler.warn(message)


def confirm(message: str, default: bool = False) -> bool:
    """Present a yes/no question. Returns True for yes, False for no."""
    return _handler.confirm(message, default)


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
# Interactivity gate
# ---------------------------------------------------------------------------

_non_interactive: bool = False


def set_non_interactive(value: bool) -> None:
    """Seed the --non-interactive flag for this CLI invocation.

    Called once from the Typer global-options callback at CLI entry. Service-layer
    code reads via ``is_interactive()`` and does not import from ``cli/_app``.
    """
    global _non_interactive  # noqa: PLW0603
    _non_interactive = value


def is_interactive() -> bool:
    """True iff stdin is a TTY and --non-interactive was not passed.

    Service-layer helpers (e.g. the prompt secret provider) consult
    this rather than the cli/_app module to stay Typer-isolated.
    """
    if _non_interactive:
        return False
    return sys.stdin.isatty()


_suppress_deprecations: bool = False


def set_suppress_deprecations(value: bool) -> None:
    """Seed the --no-deprecations flag for this CLI invocation.

    Same pattern as ``set_non_interactive``: set once from the Typer
    global-options callback; service-layer code reads via
    ``deprecations_suppressed()``.
    """
    global _suppress_deprecations  # noqa: PLW0603
    _suppress_deprecations = value


def deprecations_suppressed() -> bool:
    """True iff --no-deprecations was passed for this invocation."""
    return _suppress_deprecations


