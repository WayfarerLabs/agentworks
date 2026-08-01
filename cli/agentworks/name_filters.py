"""Validation for the list/batch commands' name filters (issue #304).

The name-filter options (``--vm``, ``--workspace``, ``--agent``) narrow a
list or batch command's result set by entity name. A mistyped name would
otherwise produce an empty result that is indistinguishable from "nothing
matched", so the service-layer functions that accept name filters call
``validate_name_filters`` before querying and raise ``NotFoundError`` for
any name with no matching entity.

Validation is DB-only by design: a filter name is valid when the entity is
defined in the state database, regardless of live VM state, so a
defined-but-stopped VM is a valid filter value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError

if TYPE_CHECKING:
    from agentworks.db import Database


def _check_filter(
    value: str | list[str],
    *,
    kind: str,
    label: str,
    defined: set[str],
    list_command: str,
) -> None:
    """Raise ``NotFoundError`` naming every unknown element of one filter.

    Unknown names are reported once each, in first-seen order, so a
    repeated element (``--vm foo,foo``) does not duplicate in the message.
    """
    names = [value] if isinstance(value, str) else value
    unknown = list(dict.fromkeys(n for n in names if n not in defined))
    if not unknown:
        return
    if len(unknown) == 1:
        message = f"unknown {label} '{unknown[0]}'"
    else:
        message = f"unknown {label}s: " + ", ".join(f"'{n}'" for n in unknown)
    raise NotFoundError(
        message,
        entity_kind=kind,
        entity_name=unknown[0],
        hint=f"Run 'agw {list_command}' to see the defined {label}s.",
    )


def validate_name_filters(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
) -> None:
    """Raise ``NotFoundError`` for any filter name with no matching entity.

    Each filter takes the same single-name-or-list shape as the
    ``Database.list_*`` query filters; every element is checked, and one
    error reports all unknown names of the first failing kind (checked in
    vm, workspace, agent order). ``None`` filters are skipped, so a call
    with no filters set is a no-op and a valid filter that simply matches
    nothing stays an empty result, not an error.
    """
    if vm_name is not None:
        _check_filter(
            vm_name,
            kind="vm",
            label="VM",
            defined={vm.name for vm in db.list_vms()},
            list_command="vm list",
        )
    if workspace_name is not None:
        _check_filter(
            workspace_name,
            kind="workspace",
            label="workspace",
            defined={ws.name for ws in db.list_workspaces()},
            list_command="workspace list",
        )
    if agent_name is not None:
        _check_filter(
            agent_name,
            kind="agent",
            label="agent",
            defined={agent.name for agent in db.list_agents()},
            list_command="agent list",
        )
