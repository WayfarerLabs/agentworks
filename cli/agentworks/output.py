"""Output contract between business logic and the presentation layer.

Business logic describes *what* it is saying; the handler decides *how* it
looks. Every line carries a semantic **role**, never baked-in presentation:

- ``info`` -> body (a normal step/status line)
- ``detail`` -> de-emphasized / secondary body (a supporting aside)
- ``warn`` / ``error`` -> a warning / a failed-outcome line (both on stderr)
- ``result`` -> a command's terminal outcome line
- ``section(title)`` -> a header that groups the lines emitted inside its
  ``with`` block

See :class:`Role` for the full vocabulary. Business logic never formats
output: it puts no indentation, no ``=== ... ===`` decoration, and no ANSI
color into a message string, and it must not import typer or call sys.exit.
Errors are signalled by raising from the exception hierarchy below; the CLI
entry point catches them and renders them through the ERROR role.

A caller that has already composed a formatted line and just needs one
*token* inside it colored (a status label like `agw doctor`'s ``[ok]``/
``[warn]``, not a whole line) calls :func:`style_status` with a
:class:`StatusStyle` instead of going through a :class:`Role`; it is a
narrower, string-in-string-out sibling of :func:`emit`.

**Section level is ambient.** ``section()`` opens a section whose header
renders at the current depth and whose body renders one level deeper; the
depth is carried in a ``contextvars`` variable, so a callee emitting output
while a caller holds an open section nests automatically, with no output
handle threaded through the call tree. The level is per-flow (each thread /
task reads its own); the active handler is a module global. ``result()``
always renders at level 0 so a command's closing line stays flush-left even
inside nested sections.

**The handler owns all presentation.** Indentation, header decoration, and
color are the handler's job, so the same output stream renders as a colored,
indented terminal transcript (``TyperHandler``), as plain text
(``_DefaultHandler``), as structured test capture (the test handler), or, in
future, as web markup, with the business logic unchanged. Set the handler
once at the entry point via :func:`set_handler`. Color lives only in the
terminal handler and only on an interactive TTY (suppressed under
``NO_COLOR``, on a pipe/redirect, and under ``--non-interactive``).
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

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
# Roles and rendering helpers
# ---------------------------------------------------------------------------


class Role(Enum):
    """The semantic intent of a one-shot output line.

    The handler maps role + section level to concrete indentation and
    decoration (the free functions never pre-render either). ERROR is
    emitted by :func:`error` (only from the CLI entry-point catch).
    STATUS is realized for inline status-token styling via
    :func:`style_status` (e.g. the ``[ok]``/``[warn]`` labels in `agw
    doctor`); it has no public whole-line ``emit`` free function of its
    own; a caller that ever needs to emit a whole line under this role
    can add one without disturbing the token styler.
    """

    BODY = auto()  # info(): a normal body line / step
    DETAIL = auto()  # detail(): de-emphasized / secondary body
    WARNING = auto()  # warn(): non-fatal warning, stderr
    ERROR = auto()  # error(): failed terminal outcome, stderr (entry catch)
    HEADER = auto()  # section() header
    RESULT = auto()  # result(): terminal outcome line, always level 0
    STATUS = auto()  # realized via style_status(); no whole-line emit yet


_INDENT_UNIT = "  "


def _pad(level: int) -> str:
    """Indentation prefix for a section ``level`` (2 spaces per level)."""
    return _INDENT_UNIT * level


def _render_header(title: str, level: int) -> str:
    """Decorate a section header by depth: ``=== t ===`` at level 0,
    ``--- t ---`` at level 1, plain ``t`` at level 2+.

    The rule characters are literal text (not ANSI), so they survive a
    pipe, matching today's ``phase()`` output. This helper is shared by
    the terminal handlers so their decoration cannot drift apart.
    """
    if level == 0:
        return f"=== {title} ==="
    if level == 1:
        return f"--- {title} ---"
    return title


class StatusStyle(Enum):
    """The semantic color of a status token: the realized form of the
    STATUS role, which has no whole-line ``emit`` rendering of its own.

    A *token* styler: it colors a short label (e.g. the ``[ok]`` in
    ``agw doctor``'s ``  [ok]   name: message``) that the caller has
    already composed into a formatted line, as opposed to :func:`Role`
    values, which each map to a whole rendered line. Kept business-logic
    neutral (GOOD/NEUTRAL/WARN/BAD rather than OK/INFO/WARN/FAIL) so it
    can style any status vocabulary, not just doctor's.
    """

    GOOD = auto()
    NEUTRAL = auto()
    WARN = auto()
    BAD = auto()


def style_status(text: str, style: StatusStyle) -> str:
    """Style a status token per ``style``, via the current handler.

    Returns ``text`` unchanged when color is disabled (or the handler
    doesn't colorize, e.g. in tests). Distinct from :func:`emit`: this
    colors a token composed into a caller-formatted line rather than
    rendering a whole line itself.
    """
    return _handler.style_status(text, style)


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
    A single :meth:`emit` carries every one-shot line, tagged with its
    :class:`Role`; the interactive and progress methods keep distinct
    signatures. Every method receives the ambient section ``level`` so
    the handler owns all indentation and decoration.
    """

    def emit(self, role: Role, message: str, level: int) -> None:
        """Render a one-shot line for ``role`` at section ``level``."""
        ...

    def style_status(self, text: str, style: StatusStyle) -> str:
        """Style a status token per ``style``. Returns ``text`` unchanged
        when the handler doesn't colorize (or color is disabled)."""
        ...

    def confirm(self, message: str, level: int, default: bool = False) -> bool:
        """Present a yes/no question. Returns True for yes, False for no."""
        ...

    def choose(self, message: str, options: list[str], level: int) -> int:
        """Present a list of options. Returns the index of the selected option."""
        ...

    def pause(self, message: str, level: int) -> None:
        """Wait for user acknowledgment (press Enter)."""
        ...

    def prompt(self, label: str, level: int, default: str | None = None) -> str:
        """Collect a string value. If default is provided and user enters nothing, returns default."""
        ...

    def prompt_secret(self, label: str, level: int, hint: str | None = None) -> str:
        """Collect a secret value with masked input. Rejects empty values."""
        ...

    def progress(self, label: str, level: int, total: int | None = None) -> Progress:
        """Start a tracked operation. Returns a Progress handle.

        If total is provided, the operation is determinate (percentage-based).
        Otherwise it is indeterminate (elapsed time only).
        """
        ...


# ---------------------------------------------------------------------------
# Default handler (plain print, no terminal magic)
# ---------------------------------------------------------------------------


class _DefaultProgress:
    # Progress sub-lines render one level deeper than the section body
    # (``pad(level + 1)``), which preserves today's hardcoded 2-space
    # indent at level 0 (byte-identical) while tracking section depth.
    def __init__(self, label: str, level: int, total: int | None = None) -> None:
        self._label = label
        self._level = level
        self._total = total
        self._start = time.monotonic()

    def update(self, current: int | None = None, message: str | None = None) -> None:
        parts = [f"{_pad(self._level + 1)}{self._label}..."]
        if current is not None and self._total is not None and self._total > 0:
            pct = current / self._total * 100
            parts.append(f" {pct:.0f}% ({current}/{self._total})")
        if message:
            parts.append(f" {message}")
        print("".join(parts))

    def done(self, message: str | None = None) -> None:
        elapsed = time.monotonic() - self._start
        suffix = f" {message}" if message else ""
        print(f"{_pad(self._level + 1)}{self._label} done ({elapsed:.0f}s){suffix}")


class _DefaultHandler:
    def emit(self, role: Role, message: str, level: int) -> None:
        if role is Role.WARNING:
            print(f"{_pad(level)}Warning: {message}", file=sys.stderr)
        elif role is Role.ERROR:
            print(f"{_pad(level)}Error: {message}", file=sys.stderr)
        elif role is Role.HEADER:
            if level in (0, 1):
                print("")
            print(f"{_pad(level)}{_render_header(message, level)}")
        elif role is Role.DETAIL:
            print(f"{_pad(level + 1)}{message}")
        elif role is Role.RESULT:
            print(f"{_pad(0)}{message}")
        else:
            # BODY renders as a plain body line. Reserved roles fall
            # through here for now: wiring STATUS (the deferred
            # fast-follow) or ERROR (Phase 5) must add its own explicit
            # branch above, not lean on this BODY fall-through.
            print(f"{_pad(level)}{message}")

    def style_status(self, text: str, style: StatusStyle) -> str:
        return text

    def confirm(self, message: str, level: int, default: bool = False) -> bool:
        try:
            suffix = " [Y/n]" if default else " [y/N]"
            response = input(f"{_pad(level)}{message}{suffix} ").strip().lower()
            if not response:
                return default
            return response in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def choose(self, message: str, options: list[str], level: int) -> int:
        try:
            print(f"{_pad(level)}{message}")
            for i, option in enumerate(options, 1):
                print(f"{_pad(level + 1)}{i}) {option}")
            while True:
                try:
                    choice = int(input(f"{_pad(level)}Choice: "))
                    if 1 <= choice <= len(options):
                        return choice - 1
                except ValueError:
                    pass
                print(f"{_pad(level)}Invalid choice. Enter 1-{len(options)}.")
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def pause(self, message: str, level: int) -> None:
        try:
            input(f"{_pad(level)}{message}")
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def prompt(self, label: str, level: int, default: str | None = None) -> str:
        try:
            suffix = f" [{default}]" if default else ""
            value = input(f"{_pad(level)}{label}{suffix}: ").strip()
            return value if value else (default or "")
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def prompt_secret(self, label: str, level: int, hint: str | None = None) -> str:
        import getpass

        try:
            if hint:
                # Hint renders one level deeper than the label so today's
                # 2-space indent is preserved at level 0.
                print(f"{_pad(level + 1)}{hint}", file=sys.stderr)
            while True:
                value = getpass.getpass(f"{_pad(level)}{label}: ")
                if value.strip():
                    return value
                print("(empty, try again)", file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def progress(self, label: str, level: int, total: int | None = None) -> Progress:
        print(f"{_pad(level + 1)}{label}...")
        return _DefaultProgress(label, level, total)


# ---------------------------------------------------------------------------
# Module API
# ---------------------------------------------------------------------------

# Only the section level is per-flow; the handler stays a module global
# (as today) so output emitted from an existing worker thread still sees
# the installed handler. See output-model-lld.md sec 1 for the rationale.
_level: ContextVar[int] = ContextVar("_output_level", default=0)
_handler: OutputHandler = _DefaultHandler()


def _current_level() -> int:
    """The ambient section level for the current flow (0 = top level)."""
    return _level.get()


@contextmanager
def section(title: str | None = None) -> Iterator[None]:
    """Open a section: emit an optional header at the current level, then
    render the body one level deeper until the block exits.

    Pass ``title`` for a ``=== / --- / plain`` header (decorated by
    depth); pass ``None`` (or omit it) for a headerless indented
    sub-block. The level is restored on exit, including when the body
    raises, so a section can never strand the ambient level.
    """
    level = _current_level()
    if title is not None:
        _handler.emit(Role.HEADER, title, level)
    token = _level.set(level + 1)
    try:
        yield
    finally:
        _level.reset(token)


def phase(title: str) -> None:
    """Emit a delineated phase header (e.g. 'Preflight', 'Provisioning').

    .. deprecated::
        Use :func:`section` instead. ``phase`` emits a bare header at the
        current level and cannot scope a body (it does not push a level),
        which is exactly what ``section()`` adds. Kept as a thin
        compatibility wrapper until its call sites are converted.
    """
    _handler.emit(Role.HEADER, title, _current_level())


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
    _handler.emit(Role.BODY, message, _current_level())


def detail(message: str) -> None:
    """Emit a de-emphasized detail / secondary body line.

    The ``DETAIL`` role renders one notch deeper than a sibling
    :func:`info` line at the ambient section level. To nest a group of
    detail lines further (the old ``indent=`` argument's job), wrap them
    in a :func:`section` block; a headerless ``section()`` pushes a level
    with no header line.
    """
    _handler.emit(Role.DETAIL, message, _current_level())


def warn(message: str) -> None:
    """Emit a non-fatal warning."""
    _handler.emit(Role.WARNING, message, _current_level())


def result(message: str) -> None:
    """Emit a terminal outcome line for the command.

    Always renders at level 0, regardless of the ambient section, so the
    closing line of a command stays flush-left even inside nested
    sections.
    """
    _handler.emit(Role.RESULT, message, 0)


def error(message: str) -> None:
    """Emit an error line: a command's failed terminal outcome.

    Always renders at level 0 on stderr. The handler owns the ``Error:``
    prefix (colored red on a TTY), so callers pass the message text
    without it, exactly mirroring how :func:`warn` owns ``Warning:``.
    Emitted only from the CLI entry-point catch (``cli/_entry.py``),
    which is the sole ``ERROR``-role site.
    """
    _handler.emit(Role.ERROR, message, 0)


def confirm(message: str, default: bool = False) -> bool:
    """Present a yes/no question. Returns True for yes, False for no."""
    return _handler.confirm(message, _current_level(), default)


def choose(message: str, options: list[str]) -> int:
    """Present a list of options. Returns the index of the selected option."""
    return _handler.choose(message, options, _current_level())


def pause(message: str) -> None:
    """Wait for user acknowledgment (press Enter)."""
    _handler.pause(message, _current_level())


def prompt(label: str, default: str | None = None) -> str:
    """Collect a string value. Returns default if user enters nothing."""
    return _handler.prompt(label, _current_level(), default)


def prompt_secret(label: str, hint: str | None = None) -> str:
    """Collect a secret value with masked input. Rejects empty values."""
    return _handler.prompt_secret(label, _current_level(), hint)


def progress(label: str, total: int | None = None) -> Progress:
    """Start a tracked operation. Returns a Progress handle."""
    return _handler.progress(label, _current_level(), total)


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


def non_interactive() -> bool:
    """True iff --non-interactive was passed for this invocation.

    Public accessor for the presentation layer (mirrors
    :func:`deprecations_suppressed`), so a handler can gate color on the
    flag without reaching the private module global across the package
    boundary. Distinct from ``is_interactive()``, which also inspects
    stdin; color depends only on the flag and the output stream.
    """
    return _non_interactive


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
