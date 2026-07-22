"""Typer-backed implementation of the agentworks.output handler protocol."""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING, TextIO

import click
import typer

from agentworks.errors import UserAbort
from agentworks.output import Role, _pad, _render_header, non_interactive

if TYPE_CHECKING:
    from agentworks.output import Progress

# DECRST reset disabling every common xterm mouse-reporting mode: 1000
# (X11), 1002 (button-event), 1003 (any-motion), 1006 (SGR, the
# ``^[[<..M`` wire form), and 1015 (urxvt). 1003 is included so a
# full-screen TUI that left any-motion tracking on keeps no reports
# flowing after the reset. 1005 (UTF-8 mouse) is intentionally excluded:
# a legacy encoding superseded by 1006. Guards against issue #211: a
# prior interactive step can leave xterm mouse tracking enabled, so the
# confirm prompt's plain input read picks up a stray mouse-event byte
# sequence (the SGR/1006 form) that leaks into the next line of output.
# Written to stdout (the stream ``typer.confirm`` prompts and reads on)
# before the prompt is issued, and only when that stream is a real
# terminal; see LLD sec 10.
_MOUSE_TRACKING_DISABLE = "\x1b[?1000;1002;1003;1006;1015l"


class _TyperProgress:
    # Progress sub-lines render one level deeper than the section body
    # (``pad(level + 1)``), preserving today's 2-space indent at level 0.
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
        typer.echo("".join(parts))

    def done(self, message: str | None = None) -> None:
        elapsed = time.monotonic() - self._start
        suffix = f" {message}" if message else ""
        typer.echo(f"{_pad(self._level + 1)}{self._label} done ({elapsed:.0f}s){suffix}")


class TyperHandler:
    def _color_enabled(self, stream: TextIO) -> bool:
        """True iff color should be applied to output on ``stream``.

        Color is emitted only when the operator has not opted out
        (``NO_COLOR`` unset, honored by presence for any value), the
        target stream is a real terminal, and the invocation is not
        ``--non-interactive``. ``stream.isatty()`` is checked against the
        actual output stream (stdout for BODY/DETAIL/HEADER/RESULT,
        stderr for WARNING/ERROR) rather than stdin, because color
        depends on where the bytes land; see output-model-lld.md sec 9.
        When this is false, the emit branches below bypass ``click.style``
        entirely so output is byte-identical to the no-color rendering.
        """
        return os.environ.get("NO_COLOR") is None and stream.isatty() and not non_interactive()

    def emit(self, role: Role, message: str, level: int) -> None:
        # Only the styling is gated on _color_enabled; indentation,
        # decoration, and stream are identical to the plain handlers.
        if role is Role.WARNING:
            prefix = "Warning:"
            if self._color_enabled(sys.stderr):
                prefix = click.style(prefix, fg="yellow")
            typer.echo(f"{_pad(level)}{prefix} {message}", err=True)
        elif role is Role.ERROR:
            prefix = "Error:"
            if self._color_enabled(sys.stderr):
                prefix = click.style(prefix, fg="red")
            typer.echo(f"{_pad(level)}{prefix} {message}", err=True)
        elif role is Role.HEADER:
            if level in (0, 1):
                typer.echo("")
            header = _render_header(message, level)
            if self._color_enabled(sys.stdout):
                header = click.style(header, bold=True)
            typer.echo(f"{_pad(level)}{header}")
        elif role is Role.DETAIL:
            text = click.style(message, dim=True) if self._color_enabled(sys.stdout) else message
            typer.echo(f"{_pad(level + 1)}{text}")
        elif role is Role.RESULT:
            text = click.style(message, fg="green", dim=True) if self._color_enabled(sys.stdout) else message
            typer.echo(f"{_pad(0)}{text}")
        else:
            # BODY renders as a plain, default-colored body line. Reserved
            # roles fall through here for now: wiring STATUS (the deferred
            # fast-follow) must add its own explicit branch above, not
            # lean on this BODY fall-through.
            typer.echo(f"{_pad(level)}{message}")

    def confirm(self, message: str, level: int, default: bool = False) -> bool:
        # stdout is the stream typer.confirm() prompts and reads on (its
        # default err=False), so that is the stream that must be a real
        # terminal for the reset to make sense; stream.isatty() is used
        # rather than output.is_interactive() because that helper
        # inspects stdin, not the stream the escape is written to (see
        # the color gate in output-model-lld.md sec 9 for the same
        # reasoning). non_interactive() additionally suppresses the
        # reset under --non-interactive even if stdout happens to be a
        # TTY, keeping piped/non-interactive output byte-plain.
        if sys.stdout.isatty() and not non_interactive():
            typer.echo(_MOUSE_TRACKING_DISABLE, nl=False)
        try:
            return typer.confirm(f"{_pad(level)}{message}", default=default)
        except click.exceptions.Abort:
            raise UserAbort("interrupted") from None

    def choose(self, message: str, options: list[str], level: int) -> int:
        typer.echo(f"{_pad(level)}{message}")
        for i, option in enumerate(options, 1):
            typer.echo(f"{_pad(level + 1)}{i}) {option}")
        while True:
            try:
                choice = int(typer.prompt(f"{_pad(level)}Choice", type=int))
                if 1 <= choice <= len(options):
                    return choice - 1
            except click.exceptions.Abort:
                raise UserAbort("interrupted") from None
            except ValueError:
                pass
            typer.echo(f"{_pad(level)}Invalid choice. Enter 1-{len(options)}.")

    def pause(self, message: str, level: int) -> None:
        try:
            input(f"{_pad(level)}{message}")
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def prompt(self, label: str, level: int, default: str | None = None) -> str:
        try:
            # An empty default is a valid answer (e.g. declining the
            # system slug) but "[]" as a rendered default suffix is
            # noise, so suppress it.
            return str(
                typer.prompt(
                    f"{_pad(level)}{label}",
                    default=default or "",
                    show_default=bool(default),
                )
            )
        except click.exceptions.Abort:
            raise UserAbort("interrupted") from None

    def prompt_secret(self, label: str, level: int, hint: str | None = None) -> str:
        try:
            if hint:
                # Hint renders one level deeper than the label so today's
                # 2-space indent is preserved at level 0.
                typer.echo(f"{_pad(level + 1)}{hint}", err=True)
            while True:
                value = str(click.prompt(f"{_pad(level)}{label}", err=True, default="", hide_input=True))
                if value.strip():
                    break
                typer.echo("(empty, try again)", err=True)
            return value
        except click.exceptions.Abort:
            raise UserAbort("interrupted") from None

    def progress(self, label: str, level: int, total: int | None = None) -> Progress:
        typer.echo(f"{_pad(level + 1)}{label}...")
        return _TyperProgress(label, level, total)
