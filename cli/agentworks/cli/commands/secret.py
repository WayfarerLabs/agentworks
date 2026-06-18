"""`agentworks secret` -- inspect declared secrets and their backend mappings."""

from __future__ import annotations

import typer

from agentworks.cli._app import app

secret_app = typer.Typer(
    name="secret",
    help="Inspect declared secrets and their backend mappings.",
    no_args_is_help=True,
)
app.add_typer(secret_app)


@secret_app.command("list")
def secret_list() -> None:
    """Show declared secrets and how each active backend would look them up.

    Rows are declared secrets; columns are the active backends in
    ``[secret_config].backends`` precedence order. Cells render the
    backend's lookup identifier (env var name, op:// URI, etc.) or
    ``disabled`` / ``enabled`` for backends with no static identifier or
    an explicit opt-out. Values are never resolved.
    """
    from agentworks.config import load_config
    from agentworks.secrets.inspect import build_secret_table, render_secret_table

    render_secret_table(build_secret_table(load_config()))
