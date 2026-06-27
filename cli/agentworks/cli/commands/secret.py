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
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.secrets.inspect import build_secret_table, render_secret_table

    config = load_config()
    registry = build_registry(config)
    render_secret_table(build_secret_table(config, registry))


@secret_app.command("describe")
def secret_describe(
    name: str = typer.Argument(..., help="Secret name to describe."),
) -> None:
    """Show the full per-secret detail view.

    Four sections per FRD R10: header (name, kind, origin,
    description), usages (one row per matching requirement), backend
    mappings (per-active-backend disposition without merging),
    resolution preview (which active backend would resolve, or
    "not available"). Does not prompt, does not resolve values.

    The secret must be in the Resource Registry -- either
    operator-declared via ``[secrets.<name>]`` or auto-declared via a
    requirement's miss policy (the framework auto-declares missing
    names that something references; ``agw secret list`` shows every
    such name).
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.secrets.inspect import describe_secret, render_secret_description

    config = load_config()
    registry = build_registry(config)
    desc = describe_secret(registry, config, name)
    render_secret_description(desc)
