"""Shared resolver helpers used by multiple command modules.

`get_db()`, `prompt_vm()`, and `prompt_workspace()` were defined alongside the
commands in the original monolithic `cli.py` (as `_get_db`, `_prompt_vm`, and
`_prompt_workspace`). They are pulled out here so the per-group command modules
can import them without depending on each other; the underscore prefix was
dropped because they are now imported across module boundaries rather than
being module-private.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import typer

from agentworks.cli._app import completion_mode_enabled, require_interactive
from agentworks.errors import BackupError, ConfigError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy, tty_interaction_access

if TYPE_CHECKING:
    from agentworks.capabilities.secret_backend import TtyInteractionAccess
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.resources.registry import Registry


def ordinary_tty_interaction_policy() -> TtyInteractionPolicy:
    """Derive ordinary-operation TTY authority at a CLI root."""
    from agentworks import output

    return TtyInteractionPolicy.REFUSE if output.non_interactive() else TtyInteractionPolicy.ALLOW


def ordinary_tty_interaction_access() -> TtyInteractionAccess:
    """Combine the CLI-root TTY policy with the current terminal fact."""
    return tty_interaction_access(
        ordinary_tty_interaction_policy(),
        terminal_input_usable=sys.stdin.isatty(),
    )


def load_completion_registry(config: Config) -> Registry:
    """Build the fullest safe Registry available to a names-only callback."""
    from agentworks import db as db_module
    from agentworks.bootstrap import load_request_registry
    from agentworks.db import open_completion_database

    completion_database = open_completion_database(db_module.DB_PATH)
    if completion_database is None:
        return load_request_registry(
            config,
            include_live_resources=False,
        )

    try:
        registry = load_request_registry(
            config,
            live_database=completion_database,
        )
    except (ConfigError, StateError):
        registry = None
    finally:
        completion_database.close()

    if registry is not None:
        return registry
    return load_request_registry(
        config,
        include_live_resources=False,
    )


def get_db() -> Database:
    """Open writable state through the migration-safety interaction boundary."""
    from agentworks import db as db_module
    from agentworks import output
    from agentworks.config import load_database_config
    from agentworks.db import (
        SchemaState,
        open_completion_database,
        open_database_safely,
        prepare_database_open,
    )

    database_path = db_module.DB_PATH
    if completion_mode_enabled():
        completion_database = open_completion_database(database_path)
        if completion_database is None:
            raise StateError("database-backed completion is unavailable")
        return completion_database

    plan = prepare_database_open(database_path)
    if plan.inspection.state is not SchemaState.STALE:
        return open_database_safely(database_path, plan, create_backup=False).database

    # Version zero has no completed Agentworks schema to preserve. Keep the
    # same locked safe-open sequencing, but initialize without interaction or
    # a non-restorable automatic backup.
    if plan.inspection.current_version == 0:
        return open_database_safely(database_path, plan, create_backup=False).database

    output.notice(
        "State database schema migration "
        f"from version {plan.inspection.current_version} to {plan.inspection.latest_version} is about to begin."
    )
    interactive = output.is_interactive() and sys.stderr.isatty()
    if interactive:
        with output.suppress_presentation():
            create_backup = output.confirm("Back up the state database before migrating?", default=True)
    else:
        create_backup = load_database_config().auto_backup_before_migration

    try:
        result = open_database_safely(database_path, plan, create_backup=create_backup)
    except BackupError as error:
        if interactive:
            hint = "Migration did not start. Retry and answer no to explicitly decline the pre-migration backup."
        else:
            hint = (
                "Migration did not start. To retry without a backup, set "
                "`[database] auto_backup_before_migration = false` in config.toml."
            )
        raise BackupError(str(error), hint=hint) from error
    except BaseException as error:
        # The safety service preserves interrupt/exit semantics with standard
        # exception notes. Surface its recovery association before Typer turns
        # KeyboardInterrupt into an exit and suppresses the traceback.
        for note in getattr(error, "__notes__", ()):
            if note.startswith("Agentworks migration recovery:"):
                output.notice(note)
        raise

    if result.backup is not None:
        from agentworks.path_rendering import format_host_path

        output.notice(f"Pre-migration database backup completed: {format_host_path(result.backup.path)}")
        for failure in result.backup.cleanup_failures:
            output.warn(f"Could not remove old automatic database backup {failure.path}: {failure.message}")
    return result.database


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

    options = [f"{v.name}  ({v.site})" for v in vms]
    idx = output.choose("Select a VM:", options)
    return vms[idx]


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
