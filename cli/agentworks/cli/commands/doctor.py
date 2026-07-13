"""`agentworks doctor` -- check environment, config, and dependencies.

Unlike the sibling modules in `commands/`, this one does not declare its own
Typer subapp: `doctor` is a top-level command (`agentworks doctor`), so it is
registered directly on the root `app` via `@app.command(...)`.
"""

from __future__ import annotations

import typer

from agentworks.cli._app import app


@app.command("doctor")
def doctor() -> None:
    """Check environment, config, and dependencies."""
    from agentworks.completions.spec import build_spec, completion_version
    from agentworks.doctor import Status, run_checks

    report = run_checks(completion_version=completion_version(build_spec(app)))

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
    typer.echo(
        f"Results: {c[Status.OK]} ok, {c[Status.INFO]} info, "
        f"{c[Status.WARN]} warn, {c[Status.FAIL]} fail"
    )
    if c[Status.FAIL] > 0:
        raise typer.Exit(1)
