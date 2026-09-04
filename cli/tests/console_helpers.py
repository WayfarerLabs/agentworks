"""Small database-only setup helpers for console unit tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.sessions.multi_console import default_shells, parse_session_spec

if TYPE_CHECKING:
    from agentworks.db import Database


def create_console_record(
    db: Database,
    *,
    name: str,
    vm_name: str,
    session_specs: list[str],
    fill_all: bool = False,
    add_admin_shell: bool = False,
) -> None:
    """Persist a validated-enough console fixture without realizing tmux."""
    specs = [parse_session_spec(value) for value in session_specs]
    if fill_all:
        present = {spec.name for spec in specs}
        extras = sorted(
            session.name
            for session in db.list_sessions()
            if session.name not in present
            and (workspace := db.get_workspace(session.workspace_name)) is not None
            and workspace.vm_name == vm_name
        )
        specs.extend(parse_session_spec(name) for name in extras)
    db.insert_console(name, vm_name, admin_shell=add_admin_shell)
    for spec in specs:
        db.add_console_session(name, spec.name, default_shells(spec.shells))
