"""Read-only artifact inspection across actual resource scopes."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.machine_output import OutputFormat

artifact_app = typer.Typer(name="artifact", help="Inspect scoped agent artifact delivery.", no_args_is_help=True)
app.add_typer(artifact_app)


@artifact_app.command("show")
def show(
    vm: Annotated[str | None, typer.Option("--vm", help="Inspect this VM or confirm the selected owner's VM.")] = None,
    admin: Annotated[
        bool, typer.Option("--admin", help="Inspect the actual admin user; requires --vm or an admin session.")
    ] = False,
    agent: Annotated[str | None, typer.Option("--agent", help="Inspect this agent and its VM.")] = None,
    workspace: Annotated[
        str | None, typer.Option("--workspace", help="Inspect this workspace and its VM, without an implied user.")
    ] = None,
    session: Annotated[
        str | None, typer.Option("--session", help="Inspect this session and its actual ancestors.")
    ] = None,
    integration: Annotated[
        str | None, typer.Option("--integration", help="Explain this integration, including inactive passthrough.")
    ] = None,
    output_format: Annotated[
        OutputFormat, typer.Option("--output", help="Output format: human or json.")
    ] = OutputFormat.HUMAN,
) -> None:
    """Explain declared, captured, and recorded artifacts without fetching or applying them."""
    from agentworks.artifacts.inspection import inspect_artifacts, inspection_data, render_artifacts
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.db import Database

    config = load_config(warn_issues=output_format is OutputFormat.HUMAN, workload_gated_issues_fatal=False)
    registry = load_request_registry(
        config, include_live_resources=False, probe_host_readiness=False, warn=output_format is OutputFormat.HUMAN
    )
    # A read must never initialize or migrate state just to manufacture an answer.
    db = Database(read_only=True)
    try:
        result = inspect_artifacts(
            db,
            registry,
            vm_name=vm,
            admin=admin,
            agent_name=agent,
            workspace_name=workspace,
            session_name=session,
            integration_name=integration,
        )
    finally:
        db.close()
    if output_format is OutputFormat.JSON:
        from agentworks.cli._machine_output import write_json_stdout
        from agentworks.machine_output import MachineOutputCommand

        write_json_stdout(MachineOutputCommand.ARTIFACT_SHOW, inspection_data(result))
    else:
        render_artifacts(result)
