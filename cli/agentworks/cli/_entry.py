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
    from agentworks.output import error, set_handler

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
        # The handler owns the (red on a TTY) `Error:` prefix, so pass the
        # message without it.
        error(str(e))
        echo_hint(e)
        raise SystemExit(1) from None
    except (ConnectivityError, ExternalError) as e:
        # External-system failures: render the one-liner AND persist the
        # full traceback to the error log so postmortem diagnosis can see
        # the underlying SSH command, platform API response, etc. Type-qualify
        # the message (Error: SSHError: ...) since these often have messages
        # that don't carry the failure category in their text. The handler
        # owns the `Error:` prefix, so the rendered line is `Error: SSHError: ...`.
        error(f"{type(e).__name__}: {e}")
        echo_hint(e)
        if debug_enabled():
            raise
        log_path = record_unhandled_error(e)
        if log_path is not None:
            typer.echo(
                f"(full traceback written to {log_path}; rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        else:
            typer.echo(
                "(could not write traceback to log; rerun with --debug or AGW_DEBUG=1 to print on stderr)",
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
        error(str(e))
        echo_hint(e)
        raise SystemExit(1) from None
    except click.exceptions.ClickException as e:
        # Real-`click` parse/usage errors raised through a raw `click_type=`
        # (e.g. a `click.Choice`, as in resource.py and completion.py). These
        # reach us because typer vendors its own private copy of click
        # (`typer._click`) and its internal handler catches only that vendored
        # ClickException; a real-`click` exception fails typer's isinstance check
        # and propagates past typer's own Rich renderer, out of app(), to here
        # (which imports the same real `click`, so this except matches). Render
        # Click's own message via e.show() (the one-line `Error: <message>`,
        # already listing the valid choices, plus the usage/"Try --help" pointer
        # when Click attached a context) and exit with its usage code, instead
        # of re-raising into typer's Rich excepthook (a full traceback). No
        # error.log write: these are user input errors, not bugs. This does NOT
        # cover unknown-option / missing-argument errors from typer-native
        # params; typer renders those itself as its boxed panel and they never
        # reach this clause.
        e.show()
        raise SystemExit(e.exit_code) from None
    except (click.exceptions.Exit, typer.Exit) as e:
        # Defensive: no known path delivers an Exit here. Typer vendors its own
        # click (see the ClickException clause above), so `typer.Exit` and the
        # real `click.exceptions.Exit` are distinct classes. Typer's standalone
        # runner (typer/core.py, `_main`) normally converts its vendored Exit
        # to sys.exit(exit_code) inside app(), and converts KeyboardInterrupt
        # to Exit(130) first, so ctrl-C already exits with the conventional
        # SIGINT code; per-op rollback handlers fire inside the command, before
        # that conversion. Should an Exit escape anyway (a framework change, or
        # a raise from code running outside app()), exit with its carried code:
        # a bare `raise` would land in typer's excepthook as a traceback with
        # exit code 1, and falling through would hit the generic Exception
        # clause and pollute error.log. A deliberate exit is not a bug, so, as
        # in the ClickException clause, there is no debug-mode re-raise here.
        raise SystemExit(e.exit_code) from None
    except (click.exceptions.Abort, typer.Abort):
        # Defensive, same vendored-vs-real story as Exit above: typer's
        # standalone runner normally handles a vendored Abort inside app() by
        # rendering its aborted message and calling sys.exit(1), so no known
        # path reaches here. For an escapee, mirror that handling (and the
        # UserAbort clause above): print the plain line and exit 1. Real ctrl-C
        # still exits 130 through typer's KeyboardInterrupt-to-Exit(130)
        # conversion, handled inside app() (or, if it too escaped, carried by
        # the Exit clause above), so SIGINT parity does not depend on this
        # clause.
        typer.echo("Aborted.", err=True)
        raise SystemExit(1) from None
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
        error(f"{type(e).__name__}: {e}")
        if log_path is not None:
            typer.echo(
                f"(full traceback written to {log_path}; rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        else:
            typer.echo(
                "(could not write traceback to log; rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        raise SystemExit(1) from None
