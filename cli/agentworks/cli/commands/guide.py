"""The Markdown-only ``agw guide`` command."""

from __future__ import annotations

import os
import sys
from typing import Annotated, Literal

import typer

from agentworks.cli._app import app
from agentworks.guide.agent_mode import select_guide_mode
from agentworks.guide.service import render_guide


@app.command("guide")
def guide(
    topics: Annotated[
        list[str] | None,
        typer.Argument(help="One or more exact guide topic names."),
    ] = None,
    agent: bool | None = typer.Option(
        None,
        "--agent/--human",
        help="Render for an agent or human, overriding automatic mode selection.",
    ),
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help="Emit one available topic name per line with no formatting.",
    ),
) -> None:
    """Show guide destinations or render selected authored guidance."""
    explicit: Literal["agent", "human"] | None = None if agent is None else ("agent" if agent else "human")
    mode = select_guide_mode(explicit, os.environ, sys.stdout.isatty())
    response = render_guide(tuple(topics or ()), mode, names_only=names_only)
    typer.echo(response.markdown, nl=False)
    if response.exit_code:
        raise typer.Exit(response.exit_code)
