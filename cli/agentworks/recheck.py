"""What a command compares, once it holds the native mutation guard, with what it read before taking it.

A command reads its rows, opens the VM's activation gate, resolves its
secrets and asks its confirmations with no guard held, so that no prompt
keeps another operation on the VM waiting. Another command may change those
rows in between; after acquiring the guard, the command reads them again and
refuses with ``StateConflictError`` if any field it relied on differs.

Each ``*_fields`` function names those fields for one row and returns
``None`` for a missing row, so presence is compared too. Only identity and
placement are compared, never whole rows: timestamps and recorded pids
change under ordinary use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateConflictError

if TYPE_CHECKING:
    from agentworks.db import AgentRow, ConsoleSessionRow, Database, SessionRow, VMRow, WorkspaceRow

type Fields = tuple[object, ...] | None


def require_unchanged(entity_kind: str, entity_name: str, read: object, current: object) -> None:
    """Refuse when what the command reads now differs from what it read before it took the guard."""
    if read != current:
        raise StateConflictError(
            f"{entity_kind} '{entity_name}' changed since this command read it",
            entity_kind=entity_kind,
            entity_name=entity_name,
            hint="Run the command again.",
        )


def vm_fields(row: VMRow | None) -> Fields:
    """The VM's identity, intent and address, as the activation gate admitted them."""
    return None if row is None else (row.vm_uuid, row.desired_runtime_state, row.tailscale_host)


def vm_release_fields(row: VMRow | None) -> Fields:
    """:func:`vm_fields` and the recorded Debian release, for the command that adopts a release."""
    return None if row is None else (row.vm_uuid, row.desired_runtime_state, row.tailscale_host, row.debian_release)


def workspace_fields(row: WorkspaceRow | None) -> Fields:
    return None if row is None else (row.vm_name, row.workspace_path, row.linux_group)


def agent_fields(row: AgentRow | None) -> Fields:
    return None if row is None else (row.vm_name, row.linux_user, row.grant_all)


def session_fields(row: SessionRow | None) -> Fields:
    return None if row is None else (row.session_uuid, row.workspace_name, row.agent_name, row.mode)


def console_member_fields(row: ConsoleSessionRow | None) -> Fields:
    """A console member's place in its console and its configured shells, what a window is rebuilt from."""
    return None if row is None else (row.position, row.shells)


def console_row_fields(db: Database, name: str) -> Fields:
    """The console's placement and its admin shell, without its members.

    For a command that rewrites a member list from what it reads under the
    guard, so a member another command added or removed meanwhile is kept.
    """
    console = db.get_console(name)
    return None if console is None else (console.vm_name, console.admin_shell)


def console_fields(db: Database, name: str) -> Fields:
    """The console's placement, its admin shell and its ordered member sessions."""
    console = db.get_console(name)
    if console is None:
        return None
    members = tuple(member.session_name for member in db.list_console_sessions(name))
    return (console.vm_name, console.admin_shell, members)


def require_session_unchanged(db: Database, name: str, read: SessionRow | None) -> None:
    """Refuse when the session row differs from the one the command read before it took the guard."""
    require_unchanged("session", name, session_fields(read), session_fields(db.get_session(name)))


def require_vm_unchanged(db: Database, admitted: VMRow) -> None:
    """Refuse when the VM row differs from the one the activation gate admitted (or the command read)."""
    require_unchanged("vm", admitted.name, vm_fields(admitted), vm_fields(db.get_vm(admitted.name)))
