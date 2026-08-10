"""`agentworks version`: print the installed CLI version.

Like `doctor`, this is a top-level command registered directly on the root
`app` rather than its own Typer subapp.
"""

from __future__ import annotations

import typer

from agentworks.cli._app import app
from agentworks.version import resolve_version as resolve_version


@app.command("version")
def version_command() -> None:
    """Print the installed agentworks CLI version."""
    typer.echo(resolve_version())
