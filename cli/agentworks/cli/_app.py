"""Root Typer app, global flags, and interactivity gate.

Lives apart from `commands/` so command modules can import the root `app`
(and the interactivity helpers) without a circular import. State that needs
to be reachable from anywhere in the CLI -- the `--non-interactive` and
`--debug` flags -- is kept here as module-level booleans.
"""

from __future__ import annotations

import os
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
# The --non-interactive flag lives in agentworks.output (service-layer reads
# it from there to stay Typer-isolated). The Typer callback below seeds it.
# --debug stays here because only the CLI error wrapper consults it.

_debug = False


def debug_enabled() -> bool:
    """Whether --debug (or AGW_DEBUG=1) is in effect for this invocation."""
    return _debug


def _set_debug(enabled: bool) -> None:
    """Record the effective debug state for this invocation (``_debug`` only).

    Deliberately does NOT touch ``AGW_DEBUG``. Mirroring the env signal is a
    separate step (:func:`_mirror_debug_to_env`) that fires only from the real
    Typer callback, after Click's authoritative parse. Both debug entry points
    (the pre-callback and the Typer callback) route through here so ``_debug``
    is set the same way, but only the authoritative one is allowed to write the
    env: the pre-callback's decision rests on an argv heuristic (see
    :func:`_seed_debug_from_pre_callback`) that must never feed back into the
    env the real callback later reads, or a false positive could not
    self-correct.
    """
    global _debug  # noqa: PLW0603
    _debug = enabled


def _mirror_debug_to_env() -> None:
    """Mirror the (authoritative) debug state into ``AGW_DEBUG``.

    Called only from the real Typer callback, after ``_set_debug`` has recorded
    the canonical debug state from Click's parsed ``--debug`` flag or the
    ambient ``AGW_DEBUG``. Layers below the CLI then read one process-wide
    signal without importing ``agentworks.cli`` (which would invert the
    layering): the azure plugin, for instance, quiets azure-identity's own
    credential-failure logging only when debug is off. ``debug_enabled``
    already treats ``--debug`` and ``AGW_DEBUG=1`` as equivalent inputs; this
    makes ``--debug`` imply the env signal too, closing the gap.

    Only ever set, never cleared: when ``_debug`` is False the ambient
    ``AGW_DEBUG`` was not "1" to begin with, so leaving it untouched keeps the
    two consistent.
    """
    if _debug:
        os.environ["AGW_DEBUG"] = "1"


def _seed_debug_from_pre_callback() -> None:
    """Set ``_debug`` from sys.argv / AGW_DEBUG *before* Click parses anything.

    The typer callback below also sets ``_debug``, but it only fires after
    Click's own arg parsing succeeds. If the user passes ``--debug --bogus``,
    Click raises BadParameter before the callback ever runs, so without
    this pre-pass, the user's ``--debug`` flag would be silently ineffective
    in exactly the case they're most likely to need it.

    The ``"--debug" in sys.argv`` match is a HEURISTIC: a literal ``--debug``
    token that Click will not bind to the flag (a positional after ``--``, a
    future passthrough arg) trips it too. That is acceptable for its only
    consumer, the parse-error traceback path, which reads ``_debug`` directly.
    Crucially this pre-pass sets ``_debug`` ONLY and never mirrors to
    ``AGW_DEBUG`` (that is the real callback's job): otherwise the heuristic's
    write would become the env value the real callback reads back, and a false
    positive would stick instead of self-correcting against Click's parse.
    """
    _set_debug("--debug" in sys.argv or os.environ.get("AGW_DEBUG") == "1")


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
    no_deprecations: Annotated[
        bool,
        typer.Option(
            "--no-deprecations",
            help="Silence the ambient per-command deprecation banner (agw doctor always reports deprecation health)",
        ),
    ] = False,
) -> None:
    """Global options for all commands."""
    from agentworks import output

    output.set_non_interactive(non_interactive)
    output.set_suppress_deprecations(no_deprecations)
    # Authoritative: Click has parsed, so `debug` is the real flag. Recompute
    # the canonical state (flag OR ambient AGW_DEBUG), then mirror it to the
    # env for layers below the CLI. Only this callback writes AGW_DEBUG.
    _set_debug(debug or os.environ.get("AGW_DEBUG") == "1")
    _mirror_debug_to_env()


# -- Interactivity gate ----------------------------------------------------


def require_interactive(what: str) -> None:
    """Raise if not interactive and a prompt would be needed."""
    from agentworks import output

    if not output.is_interactive():
        typer.echo(f"Error: {what} is required in non-interactive mode", err=True)
        raise typer.Exit(1)
