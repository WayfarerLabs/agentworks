"""Validation for list and batch command name filters.

The name-filter options (``--vm``, ``--workspace``, ``--agent``, ``--console``) narrow a
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
    hint: str,
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
        hint=hint,
    )


def validate_name_filters(
    db: Database,
    *,
    vm_name: str | list[str] | None = None,
    workspace_name: str | list[str] | None = None,
    agent_name: str | list[str] | None = None,
    console_name: str | list[str] | None = None,
) -> None:
    """Raise ``NotFoundError`` for any filter name with no matching entity.

    Each filter takes the same single-name-or-list shape as the
    ``Database.list_*`` query filters; every element is checked, and one
    error reports all unknown names of the first failing kind (checked in
    VM, workspace, agent, console order). ``None`` filters are skipped, so a call
    with no filters set is a no-op and a valid filter that simply matches
    nothing stays an empty result, not an error.
    """
    if vm_name is not None:
        _check_filter(
            vm_name,
            kind="vm",
            label="VM",
            defined={vm.name for vm in db.list_vms()},
            hint="Run 'agw vm list' to see the defined VMs.",
        )
    if workspace_name is not None:
        _check_filter(
            workspace_name,
            kind="workspace",
            label="workspace",
            defined={ws.name for ws in db.list_workspaces()},
            hint="Run 'agw workspace list' to see the defined workspaces.",
        )
    if agent_name is not None:
        _check_filter(
            agent_name,
            kind="agent",
            label="agent",
            defined={agent.name for agent in db.list_agents()},
            hint="Run 'agw agent list' to see the defined agents.",
        )
    if console_name is not None:
        _check_filter(
            console_name,
            kind="console",
            label="console",
            defined={console.name for console in db.list_consoles()},
            hint="Run 'agw console list' to see the defined consoles.",
        )
