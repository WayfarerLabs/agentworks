"""What a command compares, once it holds its native lock set, with what it read before taking it.

A command reads its rows, opens the VM's activation gate, resolves its
secrets and asks its confirmations with nothing locked, so that no prompt
keeps another operation waiting. Another command may change those rows in
between; after acquiring its set (step 7 of the operation sequence), the
command reads them again and refuses with ``StateConflictError`` if any
field it relied on differs.

The lock set itself is re-checked the same way: the command builds its
request again from what it reads under the set and refuses if any entity
or mode differs (:func:`require_request_unchanged`), so a grant, session,
member or workspace that came or went since the command read its rows is
never acted on without its lock.

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
    from agentworks.native_locks import LockRequest

type Fields = tuple[object, ...] | None


def require_unchanged(entity_kind: str, entity_name: str, read: object, current: object) -> None:
    """Refuse when what the command reads now differs from what it read before it took its lock set."""
    if read != current:
        raise StateConflictError(
            f"{entity_kind} '{entity_name}' changed since this command read it",
            entity_kind=entity_kind,
            entity_name=entity_name,
            hint="Run the command again.",
        )


def require_request_unchanged(entity_kind: str, entity_name: str, read: LockRequest, current: LockRequest) -> None:
    """Refuse when the lock set the command's rows build now differs from ``read``, the one it holds.

    Compares each entity's key and mode, its ancestors included, so a
    console whose members changed differs although its own key does not.
    """
    require_unchanged(entity_kind, entity_name, _keys(read), _keys(current))


def _keys(request: LockRequest) -> tuple[tuple[str, object], ...]:
    return tuple((entity.key, mode) for entity, mode in request.modes().items())


def vm_fields(row: VMRow | None) -> Fields:
    """The VM's identity, intent, address and template bindings, from the row the command's body runs on.

    That is the row the activation gate admitted; for a command that opens no
    gate (``vm start`` and ``stop``, ``console stop``), the row its preparation
    last read; and for one that runs on its first read (``vm reinit``, the
    batch families), that read. The bindings are the templates every
    environment on the VM composes from, so a command that resolved secrets
    for one binding refuses another.
    """
    if row is None:
        return None
    return (row.vm_uuid, row.desired_runtime_state, row.tailscale_host, row.template, row.admin_template)


def vm_release_fields(row: VMRow | None) -> Fields:
    """:func:`vm_fields` and the recorded Debian release, for the command that adopts a release."""
    fields = vm_fields(row)
    return None if row is None or fields is None else (*fields, row.debian_release)


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
    lock, so a member another command added or removed meanwhile is kept.
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


def layer_inputs(
    db: Database, vm: VMRow | None, workspace: WorkspaceRow | None = None, agent: AgentRow | None = None
) -> Fields:
    """The template bindings and desired layers setup or a launch is prepared from.

    For the VM, and the workspace and agent where given: the template
    binding (the VM's admin binding too) and the desired overlay record,
    which for the VM carries its vm and admin layers. Every environment
    composed for those entities reads them, so a command that prepared its
    setup inputs, graph or launch from them before its final set records
    them then and refuses under the set if any differs
    (:func:`require_layers_unchanged`).
    """
    overlay = db.instance_state.get_desired_overlay
    return (
        None if vm is None else (vm.template, vm.admin_template, overlay("vm", vm.name)),
        None if workspace is None else (workspace.template, overlay("workspace", workspace.name)),
        None if agent is None else (agent.template, overlay("agent", agent.name)),
    )


def require_layers_unchanged(
    db: Database,
    entity_kind: str,
    entity_name: str,
    read: Fields,
    *,
    vm_name: str,
    workspace_name: str | None = None,
    agent_name: str | None = None,
) -> None:
    """Refuse when :func:`layer_inputs` for these entities, as they are now, differs from ``read``."""
    current = layer_inputs(
        db,
        db.get_vm(vm_name),
        None if workspace_name is None else db.get_workspace(workspace_name),
        None if agent_name is None else db.get_agent(agent_name),
    )
    require_unchanged(entity_kind, entity_name, read, current)


def require_session_unchanged(db: Database, name: str, read: SessionRow | None) -> None:
    """Refuse when the session row differs from the one the command read before it took its lock set."""
    require_unchanged("session", name, session_fields(read), session_fields(db.get_session(name)))


def require_vm_unchanged(db: Database, admitted: VMRow) -> None:
    """Refuse when the VM row differs from the one the activation gate admitted (or the command read)."""
    require_unchanged("vm", admitted.name, vm_fields(admitted), vm_fields(db.get_vm(admitted.name)))
