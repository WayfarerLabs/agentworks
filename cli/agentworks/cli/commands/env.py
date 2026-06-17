"""`agentworks env` -- inspect effective env across resource scopes."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db

env_app = typer.Typer(
    name="env",
    help="Inspect agentworks-managed env across resource scopes.",
    no_args_is_help=True,
)
app.add_typer(env_app)


@env_app.command("show")
def env_show(
    vm: Annotated[
        str | None,
        typer.Option("--vm", help="Anchor the chain at this VM."),
    ] = None,
    workspace: Annotated[
        str | None,
        typer.Option(
            "--workspace",
            help="Anchor the chain at this workspace (its VM is auto-resolved).",
        ),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="Anchor the chain at this agent (its VM is auto-resolved).",
        ),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help=(
                "Anchor the chain at this session (its workspace, agent, "
                "and VM are auto-resolved)."
            ),
        ),
    ] = None,
    reveal_secrets: Annotated[
        bool,
        typer.Option(
            "--reveal-secrets",
            help=(
                "Resolve secret-backed entries through the configured "
                "backend chain and print their values (default: redacted)."
            ),
        ),
    ] = False,
) -> None:
    """Show the effective env for a resource context.

    At least one of --vm / --workspace / --agent / --session is required.
    Entries are precedence-sorted and scope-annotated. Secret-backed
    entries are redacted by default; pass --reveal-secrets to resolve
    them through the active backend chain.
    """
    from agentworks.config import load_config
    from agentworks.env.show import show_env

    show_env(
        get_db(),
        load_config(),
        vm_name=vm,
        workspace_name=workspace,
        agent_name=agent,
        session_name=session,
        reveal_secrets=reveal_secrets,
    )
