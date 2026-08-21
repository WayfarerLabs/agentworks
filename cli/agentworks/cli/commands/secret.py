"""`agentworks secret` commands for value-free source inspection and proof."""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks import output
from agentworks.cli._app import app
from agentworks.cli._helpers import get_db
from agentworks.machine_output import OutputFormat
from agentworks.secrets.policy import TtyInteractionPolicy

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
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            help="Output format: human or json. Default: human.",
        ),
    ] = OutputFormat.HUMAN,
) -> None:
    """Show declared secrets and how each active source would look them up.

    Rows are declared secrets; columns are the active sources in
    ``[secret_config].sources`` precedence order. Each cell says what that
    source would do for the secret: its lookup identifier (env var name,
    op:// URI, etc.), ``would attempt`` (no static key, e.g. prompt),
    ``not ready: <reason>`` (its host tool is missing), or ``won't attempt``
    (a ``false`` opt-out, or a mapping-required backend with no mapping).
    Values are never resolved.
    """
    if names_only and output_format is OutputFormat.JSON:
        raise typer.BadParameter("cannot be used with --output json", param_hint="--names-only")

    from agentworks import output
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.secrets.inspect import build_secret_table, render_secret_table, secret_table_data

    config = load_config(warn_issues=output_format is OutputFormat.HUMAN, workload_gated_issues_fatal=False)
    registry = load_request_registry(config, warn=output_format is OutputFormat.HUMAN)
    table = build_secret_table(config, registry)
    if names_only:
        for row in table.rows:
            output.info(row.name)
        return
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(MachineOutputCommand.SECRET_LIST, secret_table_data(table), get_binary_stream("stdout"))
        return
    render_secret_table(table)


@secret_app.command("describe")
def secret_describe(
    name: str = typer.Argument(..., help="Secret name to describe."),
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            help="Output format: human or json. Default: human.",
        ),
    ] = OutputFormat.HUMAN,
) -> None:
    """Show the full per-secret detail view.

    Five sections: header (name,
    kind, origin, description, hint); ``Referenced by:`` (one row per
    matching config reference); ``Used by (per current config):`` (one
    row per live session whose subgraph reaches this secret, projected
    via the secret kind's ``instances`` hook -- same shape as
    the resource graph); ``Backend mappings:`` (per-active-source
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
    from agentworks.secrets.inspect import describe_secret, render_secret_description, secret_description_data

    config = load_config(warn_issues=output_format is OutputFormat.HUMAN, workload_gated_issues_fatal=False)
    registry = load_request_registry(config, warn=output_format is OutputFormat.HUMAN)
    db = get_db()
    desc = describe_secret(config, registry, name, db=db)
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(
            MachineOutputCommand.SECRET_DESCRIBE,
            secret_description_data(desc),
            get_binary_stream("stdout"),
        )
        return
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
    interaction = TtyInteractionPolicy.ALLOW if allow_interaction else TtyInteractionPolicy.REFUSE
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
