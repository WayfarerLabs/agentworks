"""Typer-backed implementation of the agentworks.output handler protocol."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import click
import typer

from agentworks.errors import UserAbort

if TYPE_CHECKING:
    from agentworks.output import Progress


class _TyperProgress:
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
        typer.echo("".join(parts))

    def done(self, message: str | None = None) -> None:
        elapsed = time.monotonic() - self._start
        suffix = f" {message}" if message else ""
        typer.echo(f"  {self._label} done ({elapsed:.0f}s){suffix}")


class TyperHandler:
    def info(self, message: str) -> None:
        typer.echo(message)

    def detail(self, message: str, indent: int = 1) -> None:
        typer.echo(f"{'  ' * indent}{message}")

    def warn(self, message: str) -> None:
        typer.echo(f"Warning: {message}", err=True)

    def confirm(self, message: str, default: bool = False) -> bool:
        try:
            return typer.confirm(message, default=default)
        except click.exceptions.Abort:
            raise UserAbort("interrupted") from None

    def choose(self, message: str, options: list[str]) -> int:
        typer.echo(message)
        for i, option in enumerate(options, 1):
            typer.echo(f"  {i}) {option}")
        while True:
            try:
                choice = int(typer.prompt("Choice", type=int))
                if 1 <= choice <= len(options):
                    return choice - 1
            except click.exceptions.Abort:
                raise UserAbort("interrupted") from None
            except ValueError:
                pass
            typer.echo(f"Invalid choice. Enter 1-{len(options)}.")

    def pause(self, message: str) -> None:
        try:
            input(message)
        except (EOFError, KeyboardInterrupt):
            raise UserAbort("interrupted") from None

    def prompt(self, label: str, default: str | None = None) -> str:
        try:
            # An empty default is a valid answer (e.g. declining the
            # system slug) but "[]" as a rendered default suffix is
            # noise, so suppress it.
            return str(
                typer.prompt(
                    label,
                    default=default or "",
                    show_default=bool(default),
                )
            )
        except click.exceptions.Abort:
            raise UserAbort("interrupted") from None

    def prompt_secret(self, label: str, hint: str | None = None) -> str:
        try:
            if hint:
                typer.echo(f"  {hint}", err=True)
            while True:
                value = str(click.prompt(label, err=True, default="", hide_input=True))
                if value.strip():
                    break
                typer.echo("(empty, try again)", err=True)
            return value
        except click.exceptions.Abort:
            raise UserAbort("interrupted") from None

    def progress(self, label: str, total: int | None = None) -> Progress:
        typer.echo(f"  {label}...")
        return _TyperProgress(label, total)
