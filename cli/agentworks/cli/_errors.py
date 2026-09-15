"""Error-rendering helpers used by the top-level entrypoint."""

from __future__ import annotations

import datetime
import shlex
import sys
import traceback
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from pathlib import Path


# A wrapping layer sets a hint for its own concern, so the hint that names the
# underlying misconfiguration stays attached to the cause it wrapped. Rendering
# only the outermost hint discards the actionable half of such a failure. Both
# bounds keep a deep or self-referential cause chain from flooding the output.
_MAX_HINT_DEPTH = 8
_MAX_HINTS = 3


def echo_hint(exc: BaseException) -> None:
    """Render the error's hint and those of the causes it wraps, outermost first."""
    hints: list[str] = []
    current: BaseException | None = exc
    for _ in range(_MAX_HINT_DEPTH):
        if current is None or len(hints) >= _MAX_HINTS:
            break
        hint = getattr(current, "hint", None)
        if hint and hint not in hints:
            hints.append(hint)
        current = current.__cause__
    for hint in hints:
        typer.echo(f"  Hint: {hint}", err=True)


def record_unhandled_error(exc: BaseException) -> Path | None:
    """Append the traceback + invocation context to the error log. Best-effort.

    Returns the log path on success, or None if writing failed (the user's
    one-line error message takes precedence over the persisted traceback).

    Operation-level errors land in the per-operation ``SSHLogger`` file via
    ``SSHLogger.close()``'s ``sys.exc_info()`` introspection. This central
    log is the fallback for crashes that happen before any ``SSHLogger`` is
    constructed (config-load failures, CLI argument errors, etc.).

    The log appends forever -- not currently rotated. Crashes outside an
    operation are rare; a few MB takes years to accumulate. Add rotation
    later if it becomes an issue.
    """
    from agentworks.config import CONFIG_DIR

    log_dir = CONFIG_DIR / "logs"
    log_path = log_dir / "error.log"

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    argv = shlex.join(sys.argv)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 72}\n")
            f.write(f"{ts}\n")
            f.write(f"argv: {argv}\n\n")
            f.write(tb)
    except OSError:
        return None
    return log_path
