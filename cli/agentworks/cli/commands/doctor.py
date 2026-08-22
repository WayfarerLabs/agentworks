"""`agentworks doctor` -- check environment, config, and dependencies.

Unlike the sibling modules in `commands/`, this one does not declare its own
Typer subapp: `doctor` is a top-level command (`agentworks doctor`), so it is
registered directly on the root `app` via `@app.command(...)`.
"""

from __future__ import annotations

from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import ordinary_tty_interaction_access
from agentworks.machine_output import OutputFormat
from agentworks.output import StatusStyle, style_status


@app.command("doctor")
def doctor(
    output_format: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            help="Output format: human or json. Default: human.",
        ),
    ] = OutputFormat.HUMAN,
) -> None:
    """Check environment, config, and dependencies."""
    from agentworks.completions.spec import build_spec, completion_version
    from agentworks.doctor import Status, health_report_data, run_checks

    report = run_checks(
        tty_access=ordinary_tty_interaction_access(),
        completion_version=completion_version(build_spec(app)),
    )

    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks.machine_output import MachineOutputCommand, write_json_envelope

        write_json_envelope(MachineOutputCommand.DOCTOR, health_report_data(report), get_binary_stream("stdout"))
        if report.has_failures:
            raise typer.Exit(1)
        return

    # Doctor's own Status -> StatusStyle mapping: the CLI renderer's business,
    # not doctor.py's (the service layer returns the bare Status enum and
    # knows nothing about presentation).
    status_style = {
        Status.OK: StatusStyle.GOOD,
        Status.INFO: StatusStyle.NEUTRAL,
        Status.WARN: StatusStyle.WARN,
        Status.FAIL: StatusStyle.BAD,
    }

    typer.echo("Checking environment...\n")
    for group in report.groups:
        typer.echo(f"{group.name}:")
        for check in group.checks:
            label = {
                Status.OK: "[ok]",
                Status.INFO: "[info]",
                Status.WARN: "[warn]",
                Status.FAIL: "[FAIL]",
            }[check.status].ljust(6)
            # Style AFTER ljust: the visible column width must match the
            # plain-text case exactly (ANSI bytes would otherwise widen
            # the padded field), and NEUTRAL/no-color both return the
            # label unchanged.
            label = style_status(label, status_style[check.status])
            # `name: message` rather than `name (message)`: parens stay
            # available inside messages for asides without nesting.
            msg = check.name
            if check.message is not None:
                msg += f": {check.message}"
            typer.echo(f"  {label} {msg}")
            if check.hint:
                typer.echo(f"         hint: {check.hint}")
        typer.echo()

    c = report.counts()
    # Color each count only when it is nonzero, so a `0 ok`/`0 warn`/`0 fail`
    # all render plain and a nonzero count stands out.
    ok_count = str(c[Status.OK])
    if c[Status.OK] > 0:
        ok_count = style_status(ok_count, StatusStyle.GOOD)
    warn_count = str(c[Status.WARN])
    if c[Status.WARN] > 0:
        warn_count = style_status(warn_count, StatusStyle.WARN)
    fail_count = str(c[Status.FAIL])
    if c[Status.FAIL] > 0:
        fail_count = style_status(fail_count, StatusStyle.BAD)
    typer.echo(f"Results: {ok_count} ok, {c[Status.INFO]} info, {warn_count} warn, {fail_count} fail")
    if c[Status.FAIL] > 0:
        raise typer.Exit(1)
