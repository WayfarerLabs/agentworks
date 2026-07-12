"""CLI entrypoint: install the output handler, run the app, route errors."""

from __future__ import annotations

import click
import typer

from agentworks.cli._app import _seed_debug_from_pre_callback, debug_enabled
from agentworks.cli._errors import echo_hint, record_unhandled_error
from agentworks.cli._typer_output import TyperHandler


def main() -> None:
    """CLI entrypoint. Sets up output handler and catches business logic errors."""
    # Resolve `app` through the package namespace at call time so tests that
    # monkeypatch `agentworks.cli.app` to swap in a minimal test app actually
    # affect the invocation. A module-level `from agentworks.cli import app`
    # (or any other module-level import of `app`) would bind the name to the
    # original Typer instance and silently ignore the monkeypatch.
    from agentworks import cli as _cli
    from agentworks.errors import (
        AgentworksError,
        AlreadyExistsError,
        AuthorizationError,
        ConfigError,
        ConnectivityError,
        ExternalError,
        NotFoundError,
        StateError,
        UserAbort,
        ValidationError,
    )
    from agentworks.output import set_handler

    set_handler(TyperHandler())

    # -- Run app ---------------------------------------------------------------

    try:
        # Set _debug from sys.argv/env *before* Click parses anything, so a
        # framework-level parse error (e.g. --debug --bogus) still honors the
        # flag. The typer callback re-sets _debug after Click parses
        # successfully. Inside the try so a Ctrl-C during the pre-pass still
        # routes through our wrapper.
        _seed_debug_from_pre_callback()
        _cli.app()
    except ConfigError as e:
        # Config errors get their own label since the user is looking at the
        # wrong file, not at a runtime state problem.
        typer.echo(f"Configuration error: {e}", err=True)
        echo_hint(e)
        raise SystemExit(1) from None
    except UserAbort:
        typer.echo("Aborted.", err=True)
        raise SystemExit(1) from None
    except (NotFoundError, AlreadyExistsError, ValidationError, StateError, AuthorizationError) as e:
        # Clean domain errors: render as a one-liner with no traceback. These
        # are user-facing and a traceback adds noise without diagnostic value.
        typer.echo(f"Error: {e}", err=True)
        echo_hint(e)
        raise SystemExit(1) from None
    except (ConnectivityError, ExternalError) as e:
        # External-system failures: render the one-liner AND persist the
        # full traceback to the error log so postmortem diagnosis can see
        # the underlying SSH command, platform API response, etc. Type-qualify
        # the message (Error: SSHError: ...) since these often have messages
        # that don't carry the failure category in their text.
        typer.echo(f"Error: {type(e).__name__}: {e}", err=True)
        echo_hint(e)
        if debug_enabled():
            raise
        log_path = record_unhandled_error(e)
        if log_path is not None:
            typer.echo(
                f"(full traceback written to {log_path}; "
                f"rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        else:
            typer.echo(
                "(could not write traceback to log; "
                "rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        raise SystemExit(1) from None
    except AgentworksError as e:
        # Safety net for any AgentworksError subclass that doesn't match the
        # specific clauses above. Should not normally fire (every raise site
        # uses a kind-based type), but keeps an accidental
        # `raise AgentworksError(...)` from falling into the generic Exception
        # traceback path. Renders as the same clean one-liner the domain
        # categories use.
        typer.echo(f"Error: {e}", err=True)
        echo_hint(e)
        raise SystemExit(1) from None
    except (click.exceptions.ClickException, click.exceptions.Exit, click.exceptions.Abort):
        # Let Click / Typer own their own rendering and exit codes. Typer
        # converts KeyboardInterrupt to click.Exit(130) internally before this
        # try block sees it (see typer/core.py), so ctrl-C is already handled
        # silently with the conventional SIGINT exit code; per-op rollback
        # handlers fire inside the command, before typer's conversion.
        raise
    except KeyboardInterrupt:
        # Defensive: a KI that somehow bypasses typer's internal conversion
        # (e.g. raised during main()'s own setup, before app() runs).
        typer.echo("Cancelled.", err=True)
        raise SystemExit(130) from None
    except Exception as e:
        # Anything else is an unhandled error (third-party library, internal
        # bug, OSError, etc.). Print a clean one-liner, persist the full
        # traceback to the error log for post-hoc debugging, and exit non-zero.
        # Re-raise under --debug / AGW_DEBUG=1 so devs/CI see the traceback.
        if debug_enabled():
            raise
        log_path = record_unhandled_error(e)
        typer.echo(f"Error: {type(e).__name__}: {e}", err=True)
        if log_path is not None:
            typer.echo(
                f"(full traceback written to {log_path}; "
                f"rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        else:
            typer.echo(
                "(could not write traceback to log; "
                "rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        raise SystemExit(1) from None
