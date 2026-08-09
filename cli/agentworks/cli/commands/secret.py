"""`agentworks secret` commands for value-free source inspection and proof."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks import output
from agentworks.cli._app import app
from agentworks.cli._helpers import get_db
from agentworks.secrets.policy import InteractionPolicy, validate_interaction_policy

secret_app = typer.Typer(
    name="secret",
    help="Inspect declared secrets and their source mappings.",
    no_args_is_help=True,
)
app.add_typer(secret_app)


@secret_app.command("list")
def secret_list(
    names_only: bool = typer.Option(
        False,
        "--names-only",
        help="Emit one secret name per line (no header, no formatting). "
        "Used by shell completion; the order matches the table's row order.",
    ),
) -> None:
    """Show declared secrets and how each active source would look them up.

    Rows are declared secrets; columns are the active sources in
    ``[secret_config].backends`` precedence order. Each cell says what that
    source would do for the secret: its lookup identifier (env var name,
    op:// URI, etc.), ``would attempt`` (no static key, e.g. prompt),
    ``not ready: <reason>`` (its host tool is missing), or ``won't attempt``
    (a ``false`` opt-out, or a mapping-required backend with no mapping).
    Values are never resolved.
    """
    from agentworks import output
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.secrets.inspect import build_secret_table, render_secret_table

    config = load_config()
    registry = load_request_registry(config)
    table = build_secret_table(config, registry)
    if names_only:
        for row in table.rows:
            output.info(row.name)
        return
    render_secret_table(table)


@secret_app.command("describe")
def secret_describe(
    name: str = typer.Argument(..., help="Secret name to describe."),
) -> None:
    """Show the full per-secret detail view.

    Five sections: header (name,
    kind, origin, description, hint); ``Referenced by:`` (one row per
    matching config reference); ``Used by (per current config):`` (one
    row per live session whose subgraph reaches this secret, projected
    via the secret kind's ``instances`` hook -- same shape as
    ``agw resource describe``); ``Backend mappings:`` (per-active-source
    disposition with selected backend and provenance); ``Resolution preview:``
    (which active source would attempt, or "not attemptable"). Does not prompt, does
    not resolve values.

    The secret must be in the Resource Registry: either operator-declared
    as a ``secret`` manifest or auto-declared via a reference's miss policy.
    The framework auto-declares missing names that something references;
    ``agw secret list`` shows every such name.
    """
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.secrets.inspect import describe_secret, render_secret_description

    config = load_config()
    registry = load_request_registry(config)
    db = get_db()
    desc = describe_secret(config, registry, name, db=db)
    render_secret_description(desc)


@secret_app.command("verify")
def secret_verify(
    names: Annotated[list[str], typer.Argument(help="Secret names to verify.")],
    allow_interaction: bool = typer.Option(
        False,
        "--allow-interaction",
        help="Allow sources that may prompt, authenticate, or require operator presence.",
    ),
) -> None:
    """Prove that declared secrets resolve without displaying their values."""
    interaction = validate_interaction_policy(
        InteractionPolicy.ALLOW if allow_interaction else InteractionPolicy.REFUSE
    )
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.errors import ValidationError
    from agentworks.secrets.outcomes import ResolutionCategory
    from agentworks.secrets.verification import render_verification, verify_secrets

    if allow_interaction and output.non_interactive():
        raise ValidationError("--allow-interaction cannot be used with --non-interactive")
    config = load_config()
    registry = load_request_registry(config)
    outcomes = verify_secrets(config, registry, names, interaction=interaction)
    render_verification(outcomes)
    if any(outcome.category is not ResolutionCategory.RESOLVED for outcome in outcomes):
        raise typer.Exit(1)
