"""`agentworks database` commands for direct state backup and restore."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003  # Typer resolves the runtime annotation.
from typing import Annotated

import typer

from agentworks import output
from agentworks.cli._app import app

database_app = typer.Typer(
    name="database",
    help="Back up or restore the Agentworks state database.",
    no_args_is_help=True,
)
app.add_typer(database_app)


@database_app.command("backup")
def database_backup() -> None:
    """Create an on-demand snapshot of the state database."""
    import agentworks.db as db
    from agentworks.path_rendering import format_host_path

    typer.echo("Creating database backup...", err=True)
    path = db.create_manual_backup(db.DB_PATH)
    output.result(format_host_path(path))


@database_app.command("restore")
def database_restore(
    backup_path: Annotated[Path, typer.Argument(help="Agentworks database backup to restore.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Replace the live database without prompting."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Restore even when the backup has inconsistent relationships."),
    ] = False,
) -> None:
    """Replace the live state database with a validated backup."""
    import agentworks.db as db
    from agentworks.errors import UserAbort, ValidationError
    from agentworks.path_rendering import format_host_path

    if not yes and not output.is_interactive():
        raise ValidationError(
            "database restore requires confirmation in non-interactive mode",
            hint="Pass --yes only when replacing the live state database is intentional.",
        )

    with db.prepare_restore(
        backup_path,
        db.DB_PATH,
        allow_foreign_key_violations=force,
    ) as prepared:
        if prepared.inspection.has_foreign_key_violations:
            output.warn(
                "The selected backup has inconsistent relationships. Some resources may be unavailable "
                "until they are repaired; --force permits restoring this state."
            )
        typer.echo(f"Backup: {format_host_path(prepared.backup_path)}", err=True)
        typer.echo(f"Live database: {format_host_path(prepared.database_path)}", err=True)
        if not yes:
            with output.suppress_presentation():
                confirmed = output.confirm("Replace the live state database with this backup?", default=False)
            if not confirmed:
                raise UserAbort("database restore cancelled")

        prepared.apply()
        if prepared.inspection.has_foreign_key_violations:
            output.warn(
                "The restored database has inconsistent relationships. Some resources may be unavailable "
                "until they are repaired."
            )
        typer.echo("Database restore complete.", err=True)
