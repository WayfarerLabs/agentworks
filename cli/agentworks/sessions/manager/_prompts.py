"""Interactive prompts used by ``create_session``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import (
    NotFoundError,
    ValidationError,
)

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow


def _prompt_workspace_choice(db: Database, vm_filter: str | None) -> tuple[str | None, bool]:
    """Pick an existing workspace or commit to creating a new one.

    Returns ``(workspace_name, new_workspace)`` where exactly one is the
    operator's choice: either an existing-workspace name (and
    ``new_workspace=False``) or ``new_workspace=True`` with no name (the
    caller defaults the new workspace's name to the session name).

    Always prompts -- no single-workspace auto-select. Including
    ``[Create new]`` as the last option makes interactive mode the
    functional equivalent of passing ``--new-workspace`` / ``--workspace``
    on the CLI.

    ``vm_filter`` narrows the chooser to workspaces on that VM when any
    other anchor (``--vm`` or ``--agent``) has already pinned one. The
    info line above the chooser tells the operator the filter is active
    so a missing workspace doesn't look like a bug.
    """
    if not output.is_interactive():
        raise ValidationError(
            "workspace is required in non-interactive mode",
            entity_kind="session",
            hint="pass --workspace <name> or --new-workspace",
        )
    all_workspaces = db.list_workspaces()
    if vm_filter is not None:
        workspaces = [w for w in all_workspaces if w.vm_name == vm_filter]
        if len(workspaces) < len(all_workspaces):
            output.info(f"Only showing workspaces on VM '{vm_filter}'")
    else:
        workspaces = all_workspaces
    options = [f"{ws.name}  (vm: {ws.vm_name}, template: {ws.template or '<none>'})" for ws in workspaces]
    options.append("[Create new workspace]")
    idx = output.choose("Select a workspace:", options)
    if idx == len(options) - 1:
        return None, True
    return workspaces[idx].name, False


def _prompt_mode_choice(db: Database, vm: VMRow | None) -> tuple[str | None, bool, bool]:
    """Pick admin, an existing agent, or commit to creating a new agent.

    Returns ``(agent_name, new_agent, admin)``. Exactly one of these
    encodes the operator's choice:
    - ``agent_name=<name>, new_agent=False, admin=False`` for an
      existing agent.
    - ``agent_name=None, new_agent=True, admin=False`` for ``[Create new]``.
    - ``agent_name=None, new_agent=False, admin=True`` for ``admin``.

    When ``vm`` is known, lists only agents on that VM (and prints an
    info line if other-VM agents got filtered out). When ``vm`` is
    ``None`` -- the VM hasn't been determined yet -- lists agents
    across all VMs, labeling each with its VM so an operator's pick
    of an existing agent pins the VM downstream. This is the path
    that lets ``agw session create my-sess --new-workspace`` resolve
    the VM via the mode prompt's agent pick rather than a separate
    VM prompt.

    Always prompts -- no single-option auto-select.
    """
    if not output.is_interactive():
        raise ValidationError(
            "session mode is required in non-interactive mode",
            entity_kind="session",
            hint="pass --admin, --agent <name>, or --new-agent",
        )
    all_agents = db.list_agents()
    if vm is not None:
        candidates = [a for a in all_agents if a.vm_name == vm.name]
        if len(candidates) < len(all_agents):
            output.info(f"Only showing agents on VM '{vm.name}'")
    else:
        candidates = all_agents
    options = ["admin"]
    for a in candidates:
        options.append(f"agent: {a.name}  (vm: {a.vm_name}, template: {a.template or '<none>'})")
    options.append("[Create new agent]")
    idx = output.choose("Run session as:", options)
    if idx == 0:
        return None, False, True  # admin
    if idx == len(options) - 1:
        return None, True, False  # new agent
    return candidates[idx - 1].name, False, False  # existing agent


def _prompt_vm(db: Database) -> VMRow:
    """Pick a VM when nothing else pins it.

    The only auto-resolution helper that survives for sessions:
    workspace and mode are session-semantic and demand explicit
    operator intent (``--workspace``/``--new-workspace``,
    ``--admin``/``--agent``/``--new-agent``). VM is infrastructure --
    pick a host; the choice doesn't change what the session IS, just
    where it runs. Reached only when the operator hasn't passed
    ``--vm`` and no workspace / agent anchor was available to
    pin the VM (e.g. ``--new-workspace --admin`` or
    ``--new-workspace --new-agent``, both without ``--vm``). Filters
    out VMs whose init is incomplete: a session on a half-initialized
    VM would just fail downstream.
    """
    from agentworks.db import InitStatus

    vms = db.list_vms()
    usable = [v for v in vms if v.init_status in {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}]
    if not usable:
        raise NotFoundError(
            "no VMs available",
            entity_kind="vm",
            hint="Create one with 'agw vm create', or pass --vm to override.",
        )
    if len(usable) == 1:
        output.info(f"Using VM '{usable[0].name}'")
        return usable[0]
    if not output.is_interactive():
        raise ValidationError(
            "--vm is required in non-interactive mode when no workspace or agent pins the VM",
            entity_kind="session",
        )
    options = [f"{v.name}  ({v.site})" for v in usable]
    idx = output.choose("Select a VM:", options)
    return usable[idx]
