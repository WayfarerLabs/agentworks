"""`agentworks completion` -- generate or install shell completions."""

from __future__ import annotations

from typing import Annotated

import click
import typer

from agentworks.cli._app import app

completion_app = typer.Typer(
    name="completion",
    help="Generate or install shell completions.",
    no_args_is_help=True,
)
app.add_typer(completion_app)

# Accept the canonical `powershell` plus the `pwsh` alias users see in their
# binary name.
_SHELL_CHOICES = click.Choice(["bash", "zsh", "powershell", "pwsh"])


def _resolve_shell(shell: str | None) -> str:
    """Normalize and validate a --shell option, autodetecting if not given."""
    from agentworks.completions import detect_shell

    if shell is None:
        detected = detect_shell()
        if detected is None:
            typer.echo(
                "Error: unable to detect the shell. Pass --shell {bash|zsh|powershell}.",
                err=True,
            )
            raise typer.Exit(1)
        return detected
    if shell == "pwsh":
        return "powershell"
    return shell


@completion_app.command("show")
def completion_show(
    shell: Annotated[
        str | None,
        typer.Option("--shell", help="Shell type (autodetected if omitted)", click_type=_SHELL_CHOICES),
    ] = None,
) -> None:
    """Print the completion script to stdout."""
    from agentworks.completions import generate

    typer.echo(generate(_resolve_shell(shell)), nl=False)


@completion_app.command("install")
def completion_install(
    shell: Annotated[
        str | None,
        typer.Option("--shell", help="Shell type (autodetected if omitted)", click_type=_SHELL_CHOICES),
    ] = None,
) -> None:
    """Install the completion script to the appropriate location."""
    from agentworks.completions import generate
    from agentworks.completions.install import install_completions

    resolved = _resolve_shell(shell)
    install_completions(resolved, generate(resolved))


@completion_app.command("uninstall")
def completion_uninstall(
    shell: Annotated[
        str | None,
        typer.Option("--shell", help="Shell type (autodetected if omitted)", click_type=_SHELL_CHOICES),
    ] = None,
) -> None:
    """Remove installed completion files for the given shell."""
    from agentworks.completions.install import uninstall_completions

    uninstall_completions(_resolve_shell(shell))
