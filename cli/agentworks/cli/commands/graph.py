"""``agentworks graph`` resource relationship queries."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db
from agentworks.machine_output import OutputFormat
from agentworks.resources.graph_query import GraphDirection

graph_app = typer.Typer(
    name="graph",
    help="Query declared and live resource relationships.",
    no_args_is_help=True,
)
app.add_typer(graph_app)


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
        GraphDirection,
        typer.Option(
            "--direction",
            help="Relationship traversal direction. Default: both.",
        ),
    ] = GraphDirection.BOTH,
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
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.resources.access import parse_resource_identity
    from agentworks.resources.graph_query import (
        graph_result_data,
        show_graph,
    )

    identity = parse_resource_identity(focus)
    depth_limit = parse_graph_depth(depth)
    # Never reads the operator's SSH key files; see load_config's
    # workload_gated_issues_fatal doc.
    config = load_config(warn_issues=output_format is OutputFormat.HUMAN, workload_gated_issues_fatal=False)
    db = get_db()
    registry = load_request_registry(
        config,
        warn=output_format is OutputFormat.HUMAN,
        probe_host_readiness=False,
        live_database=db,
    )
    result = show_graph(
        registry,
        identity,
        direction,
        depth_limit,
    )

    if output_format is OutputFormat.JSON:
        from agentworks.cli._machine_output import write_json_stdout
        from agentworks.machine_output import MachineOutputCommand

        write_json_stdout(
            MachineOutputCommand.GRAPH_SHOW,
            graph_result_data(result),
        )
        return

    from agentworks.resources.graph_render import render_graph_result

    render_graph_result(result)
