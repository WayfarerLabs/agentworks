"""Typer-backed implementation of the agentworks.output handler protocol."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import click
import typer

from agentworks.errors import UserAbort
from agentworks.output import Role, _pad, _render_header

if TYPE_CHECKING:
    from agentworks.output import Progress


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
    def emit(self, role: Role, message: str, level: int) -> None:
        if role is Role.WARNING:
            typer.echo(f"{_pad(level)}Warning: {message}", err=True)
        elif role is Role.ERROR:
            typer.echo(f"{_pad(level)}Error: {message}", err=True)
        elif role is Role.HEADER:
            if level in (0, 1):
                typer.echo("")
            typer.echo(f"{_pad(level)}{_render_header(message, level)}")
        elif role is Role.DETAIL:
            typer.echo(f"{_pad(level + 1)}{message}")
        elif role is Role.RESULT:
            typer.echo(f"{_pad(0)}{message}")
        else:
            # BODY renders as a plain body line. Reserved roles fall
            # through here for now: wiring STATUS (the deferred
            # fast-follow) or ERROR (Phase 5) must add its own explicit
            # branch above, not lean on this BODY fall-through.
            typer.echo(f"{_pad(level)}{message}")

    def confirm(self, message: str, level: int, default: bool = False) -> bool:
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
