"""``agentworks graph`` resource relationship queries."""

from __future__ import annotations

from typing import Annotated

import click
import typer

from agentworks.cli._app import app
from agentworks.machine_output import OutputFormat

graph_app = typer.Typer(
    name="graph",
    help="Query declared and live resource relationships.",
    no_args_is_help=True,
)
app.add_typer(graph_app)

_DIRECTION_CHOICES = click.Choice(["dependencies", "dependents", "both"])


def parse_graph_depth(value: str) -> int | None:
    """Parse the graph depth option at the CLI boundary."""
    if value == "all":
        return None
    try:
        depth = int(value, 10)
    except ValueError:
        depth = 0
    if depth <= 0:
        raise typer.BadParameter("must be a positive integer or all", param_hint="--depth")
    return depth


@graph_app.command("show")
def show(
    focus: Annotated[
        str,
        typer.Argument(help="Resource as KIND/NAME."),
    ],
    direction: Annotated[
        str,
        typer.Option(
            "--direction",
            help="Relationship traversal direction. Default: both.",
            click_type=_DIRECTION_CHOICES,
        ),
    ] = "both",
    depth: Annotated[
        str,
        typer.Option(
            "--depth",
            help="Maximum relationship distance, or all. Default: 1.",
        ),
    ] = "1",
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            help="Output format: human or json. Default: human.",
        ),
    ] = OutputFormat.HUMAN,
) -> None:
    """Show the resource graph reachable from one resource."""
    from agentworks import db
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.resources.access import parse_resource_identity
    from agentworks.resources.graph_query import (
        DatabaseLiveSource,
        GraphDirection,
        graph_result_data,
        show_graph,
    )

    identity = parse_resource_identity(focus)
    depth_limit = parse_graph_depth(depth)
    graph_direction = GraphDirection(direction)
    # Same rationale as `resource kinds`/`resource list`/`resource show`:
    # a graph query displays declared and live relationships, not
    # operator identity, so a placeholder or missing SSH key doesn't
    # block it (`load_request_registry` below also runs with host
    # readiness probing off, so no platform preflight touches
    # config.operator either).
    config = load_config(warn_issues=output_format is OutputFormat.HUMAN, require_ssh_key_files=False)
    registry = load_request_registry(
        config,
        warn=output_format is OutputFormat.HUMAN,
        probe_host_readiness=False,
    )
    result = show_graph(
        registry,
        identity,
        graph_direction,
        depth_limit,
        DatabaseLiveSource(db.DB_PATH),
    )

    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(
            MachineOutputCommand.GRAPH_SHOW,
            graph_result_data(result),
            get_binary_stream("stdout"),
        )
        return

    from agentworks.resources.graph_render import render_graph_result

    render_graph_result(result)
