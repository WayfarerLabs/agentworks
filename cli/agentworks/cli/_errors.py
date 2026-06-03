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


def echo_hint(exc: BaseException) -> None:
    """Render an AgentworksError's hint attribute on a second line if set."""
    hint = getattr(exc, "hint", None)
    if hint:
        typer.echo(f"  Hint: {hint}", err=True)


def record_unhandled_error(exc: BaseException) -> Path | None:
    """Append the traceback + invocation context to the error log. Best-effort.

    Returns the log path on success, or None if writing failed (the user's
    one-line error message takes precedence over the persisted traceback).

    The log appends forever -- not currently rotated. Errors are rare; a few
    MB takes years to accumulate. Add rotation later if it becomes an issue.
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
