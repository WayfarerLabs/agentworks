"""Explicit synchronous reconciliation of stored runtime targets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.harness_integration import HarnessLaunchIntent
from agentworks.secrets.policy import require_exact_tty_interaction_policy

from ._selection import select_console_scope, select_session_scope, select_vm_scope, session_selection
from ._service import run_selected

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.secrets.policy import TtyInteractionPolicy

    from ._models import RuntimeSelection, SessionOperation


def _reconcile(
    db: Database, config: Config, selection: RuntimeSelection, *, dry_run: bool, interaction: TtyInteractionPolicy
) -> None:
    require_exact_tty_interaction_policy(interaction)
    if dry_run:
        from ._preview import preview_selected

        preview_selected(db, config, selection, interaction=interaction)
    else:
        run_selected(db, config, selection, interaction=interaction, intent=HarnessLaunchIntent.RESUME_OR_NEW)


def reconcile_vm(
    db: Database,
    config: Config,
    *,
    name: str | None = None,
    all_vms: bool = False,
    dry_run: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    selection = select_vm_scope(db, name=name, all_vms=all_vms)
    _reconcile(db, config, selection, dry_run=dry_run, interaction=interaction)


def reconcile_session(
    db: Database, config: Config, *, name: str, dry_run: bool = False, interaction: TtyInteractionPolicy
) -> None:
    _reconcile(db, config, select_session_scope(db, name), dry_run=dry_run, interaction=interaction)


def reconcile_console(
    db: Database, config: Config, *, name: str, dry_run: bool = False, interaction: TtyInteractionPolicy
) -> None:
    _reconcile(db, config, select_console_scope(db, name), dry_run=dry_run, interaction=interaction)


def run_session_batch(
    db: Database,
    config: Config,
    sessions: Sequence[SessionRow],
    *,
    operation: SessionOperation,
    force: bool,
    intent: HarnessLaunchIntent,
    running_restart_authorized: frozenset[str],
    interaction: TtyInteractionPolicy,
) -> None:
    require_exact_tty_interaction_policy(interaction)
    if not sessions:
        return
    run_selected(
        db,
        config,
        session_selection(db, sessions),
        interaction=interaction,
        intent=intent,
        operation=operation,
        force=force,
        authorized_running=running_restart_authorized,
    )
