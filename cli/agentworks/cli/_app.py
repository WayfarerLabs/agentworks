"""Root Typer app, global flags, and interactivity gate.

Lives apart from `commands/` so command modules can import the root `app`
(and the interactivity helpers) without a circular import. State that needs
to be reachable from anywhere in the CLI -- the `--non-interactive` and
`--debug` flags -- is kept here as module-level booleans.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

app = typer.Typer(
    name="agentworks",
    help="Orchestrate workspace lifecycle across multiple compute targets.",
    no_args_is_help=True,
    # Suppress typer's generic --install-completion / --show-completion flags
    # in favor of the project's hand-rolled `agw completion show|install`
    # subcommands, which emit scripts with the dynamic completers (vms,
    # workspaces, sessions, agents, consoles, ...).
    add_completion=False,
)


# -- Global flag state -----------------------------------------------------

_non_interactive = False
_debug = False


def debug_enabled() -> bool:
    """Whether --debug (or AGW_DEBUG=1) is in effect for this invocation."""
    return _debug


def _seed_debug_from_pre_callback() -> None:
    """Set ``_debug`` from sys.argv / AGW_DEBUG *before* Click parses anything.

    The typer callback below also sets ``_debug``, but it only fires after
    Click's own arg parsing succeeds. If the user passes ``--debug --bogus``,
    Click raises BadParameter before the callback ever runs -- so without
    this pre-pass, the user's ``--debug`` flag would be silently ineffective
    in exactly the case they're most likely to need it.
    """
    import os

    global _debug  # noqa: PLW0603
    _debug = "--debug" in sys.argv or os.environ.get("AGW_DEBUG") == "1"


@app.callback()
def _global_options(
    non_interactive: Annotated[
        bool,
        typer.Option("--non-interactive", help="Disable interactive prompts"),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Print full Python traceback on unhandled errors (also via AGW_DEBUG=1)",
        ),
    ] = False,
) -> None:
    """Global options for all commands."""
    import os

    global _non_interactive, _debug  # noqa: PLW0603
    _non_interactive = non_interactive
    _debug = debug or os.environ.get("AGW_DEBUG") == "1"


# -- Interactivity gate ----------------------------------------------------


def is_interactive() -> bool:
    """Check if stdin is a TTY and --non-interactive was not passed."""
    if _non_interactive:
        return False
    return sys.stdin.isatty()


def require_interactive(what: str) -> None:
    """Raise if not interactive and a prompt would be needed."""
    if not is_interactive():
        typer.echo(f"Error: {what} is required in non-interactive mode", err=True)
        raise typer.Exit(1)
