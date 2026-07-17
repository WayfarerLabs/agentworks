"""`agentworks version`: print the installed CLI version.

Like `doctor`, this is a top-level command registered directly on the root
`app` rather than its own Typer subapp.
"""

from __future__ import annotations

import typer

from agentworks.cli._app import app

# The distribution name from pyproject `[project].name`, which is how the
# version is recorded in the installed package metadata (release-please
# bumps it there). Distinct from the import package name (`agentworks`).
_DIST_NAME = "agentworks-cli"


def resolve_version() -> str:
    """The installed CLI version, or ``"unknown"`` when the package
    metadata is unavailable (e.g. a source tree that was never installed).
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return "unknown"


@app.command("version")
def version_command() -> None:
    """Print the installed agentworks CLI version."""
    typer.echo(resolve_version())
