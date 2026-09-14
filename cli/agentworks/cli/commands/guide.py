"""The Markdown-only ``agw guide`` command group."""

from __future__ import annotations

import os
import sys
from typing import Annotated, Literal, cast

import typer

from agentworks.cli._app import app
from agentworks.guide.agent_mode import GuideMode, select_guide_mode
from agentworks.guide.service import list_guide_topics, render_guide

guide_app = typer.Typer(
    name="guide",
    help="Show static, package-owned Agentworks guidance.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(guide_app)

GuideModeOption = Annotated[
    bool | None,
    typer.Option(
        "--agent/--human",
        help="Render for an agent or human, overriding automatic mode selection.",
    ),
]
GuideListModeOption = Annotated[
    bool | None,
    typer.Option(
        "--agent/--human",
        help="Accepted for command symmetry; does not affect topic-list output.",
    ),
]


def _guide_mode(agent: bool | None) -> GuideMode:
    explicit: Literal["agent", "human"] | None = None if agent is None else ("agent" if agent else "human")
    return select_guide_mode(explicit, os.environ, sys.stdout.isatty())


@guide_app.callback()
def guide(
    context: typer.Context,
    agent: GuideModeOption = None,
) -> None:
    """Render the guide index when no subcommand is selected."""
    context.obj = _guide_mode(agent)
    if context.invoked_subcommand is not None:
        return
    typer.echo(render_guide(None, cast("GuideMode", context.obj)).markdown, nl=False)


@guide_app.command("list")
def guide_list(agent: GuideListModeOption = None) -> None:
    """Emit every available topic name, one per line."""
    typer.echo(list_guide_topics().markdown, nl=False)


@guide_app.command("show")
def guide_show(
    context: typer.Context,
    topic: Annotated[str, typer.Argument(help="One exact guide topic name.")],
    agent: GuideModeOption = None,
) -> None:
    """Render one exact guide topic."""
    mode = _guide_mode(agent) if agent is not None else cast("GuideMode", context.obj)
    typer.echo(render_guide(topic, mode).markdown, nl=False)
