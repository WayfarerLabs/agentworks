"""Shared resolver helpers used by multiple command modules.

`get_db()`, `prompt_vm()`, and `prompt_workspace()` were defined alongside the
commands in the original monolithic `cli.py` (as `_get_db`, `_prompt_vm`, and
`_prompt_workspace`). They are pulled out here so the per-group command modules
can import them without depending on each other; the underscore prefix was
dropped because they are now imported across module boundaries rather than
being module-private.
"""

from __future__ import annotations

from typing import Protocol

import typer

from agentworks.cli._app import require_interactive
from agentworks.db import Database, VMRow, WorkspaceRow


def get_db() -> Database:
    return Database()


def prompt_workspace(db: Database, workspace: str | None) -> WorkspaceRow:
    """Resolve a workspace, prompting if not provided and validating either way."""
    from agentworks import output

    if workspace is not None:
        ws = db.get_workspace(workspace)
        if ws is None:
            typer.echo(f"Error: workspace '{workspace}' not found.", err=True)
            raise typer.Exit(1)
        return ws

    workspaces = db.list_workspaces()
    if not workspaces:
        typer.echo("Error: no workspaces found. Create one with 'agw workspace create'.", err=True)
        raise typer.Exit(1)

    if len(workspaces) == 1:
        output.info(f"Using workspace '{workspaces[0].name}'")
        return workspaces[0]

    require_interactive("--workspace")

    options = [f"{ws.name}  (vm: {ws.vm_name})" for ws in workspaces]
    idx = output.choose("Select a workspace:", options)
    return workspaces[idx]


def prompt_vm(db: Database, vm_name: str | None) -> VMRow:
    """Resolve a VM, prompting if not provided and validating either way."""
    from agentworks import output

    if vm_name is not None:
        vm = db.get_vm(vm_name)
        if vm is None:
            typer.echo(f"Error: VM '{vm_name}' not found.", err=True)
            raise typer.Exit(1)
        return vm

    vms = db.list_vms()
    if not vms:
        typer.echo("Error: no VMs found. Create one with 'agw vm create'.", err=True)
        raise typer.Exit(1)

    if len(vms) == 1:
        output.info(f"Using VM '{vms[0].name}'")
        return vms[0]

    require_interactive("--vm")

    options = [f"{v.name}  ({v.platform})" for v in vms]
    idx = output.choose("Select a VM:", options)
    return vms[idx]


class HasDescription(Protocol):
    """Structural protocol for catalog entries that have a description."""

    @property
    def description(self) -> str: ...


def parse_csv_filter(value: str | None) -> str | list[str] | None:
    """Parse a comma-separated CLI filter value.

    Returns ``None`` when the flag was not supplied or contained only
    whitespace and separators. Returns a bare string when exactly one name
    is present (preserves single-value semantics for the readable case).
    Returns a list of stripped, non-empty names when multiple values were
    supplied. Used by every list command's CSV filter flag so multi-value
    parsing is consistent across the surface.
    """
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return parts
