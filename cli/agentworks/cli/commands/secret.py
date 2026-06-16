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
    from agentworks.secrets.inspect import build_secret_table

    table = build_secret_table(load_config())

    if not table.rows:
        typer.echo("No secrets declared in config.")
        return
    if not table.backend_kinds:
        typer.echo(
            "No active secret backends. Set [secret_config].backends in your "
            "config (or leave it unset to use the default chain)."
        )
        return

    # Render each cell to a string up front so column widths can be
    # computed from the rendered text.
    rendered: list[tuple[str, ...]] = []
    for row in table.rows:
        cells: list[str] = [row.name]
        for cell in row.cells:
            if not cell.would_attempt:
                cells.append("disabled")
            elif cell.identifier is not None:
                cells.append(cell.identifier)
            else:
                cells.append("enabled")
        rendered.append(tuple(cells))

    headers = ("NAME", *table.backend_kinds)
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rendered))
        for i in range(len(headers))
    ]

    def _fmt(cols: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    typer.echo(_fmt(headers))
    typer.echo(_fmt(tuple("-" * w for w in widths)))
    for r in rendered:
        typer.echo(_fmt(r))
