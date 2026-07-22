"""Query-building and row-conversion helpers shared by Database's CRUD
methods: turning ``sqlite3.Row`` results into the typed row dataclasses,
and decoding the JSON-encoded columns (``platform_metadata``,
``harness_state``, ``shells``) those rows carry.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agentworks.db.models import (
    AgentGrantRow,
    AgentRow,
    ConsoleRow,
    ConsoleSessionRow,
    SessionRow,
    ShellEntry,
    VMEventRow,
    VMRow,
    WorkspaceRow,
)

if TYPE_CHECKING:
    import sqlite3


def _eq_or_in(column: str, value: str | list[str] | None) -> tuple[str, tuple[str, ...]]:
    """Build a SQL WHERE-clause fragment for a singular OR list filter.

    Returns ``("", ())`` when the filter is absent. Returns ``column = ?``
    for a single string or single-element list. Returns ``column IN (?, ?, ...)``
    for a multi-element list. Used by every ``Database.list_*`` method that
    accepts a multi-value filter so single-value callers stay readable and
    multi-value callers get OR-within-filter semantics from one place.
    """
    if value is None:
        return "", ()
    values = [value] if isinstance(value, str) else list(value)
    if not values:
        return "", ()
    if len(values) == 1:
        return f"{column} = ?", (values[0],)
    placeholders = ",".join("?" * len(values))
    return f"{column} IN ({placeholders})", tuple(values)


# -- Row converters --------------------------------------------------------


def _to_vm(row: sqlite3.Row) -> VMRow:
    extra = row["extra_packages"]
    metadata = row["platform_metadata"]
    return VMRow(
        name=row["name"],
        site=row["site"],
        template=row["template"],
        admin_template=row["admin_template"],
        extra_packages=json.loads(extra) if extra else [],
        provisioning_status=row["provisioning_status"],
        init_status=row["init_status"],
        tailscale_host=row["tailscale_host"],
        cpus=row["cpus"],
        memory_gib=row["memory_gib"],
        disk_gib=row["disk_gib"],
        swap_gib=row["swap_gib"],
        admin_username=row["admin_username"],
        hostname=row["hostname"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        platform_metadata=json.loads(metadata) if metadata else {},
        operator_stopped=bool(row["operator_stopped"]),
    )


def _to_workspace(row: sqlite3.Row) -> WorkspaceRow:
    return WorkspaceRow(
        name=row["name"],
        vm_name=row["vm_name"],
        template=row["template"],
        workspace_path=row["workspace_path"],
        created_at=row["created_at"],
        linux_group=row["linux_group"],
    )


def _to_agent(row: sqlite3.Row) -> AgentRow:
    return AgentRow(
        name=row["name"],
        vm_name=row["vm_name"],
        linux_user=row["linux_user"],
        template=row["template"],
        grant_all=bool(row["grant_all"]),
        created_at=row["created_at"],
    )


def _to_agent_grant(row: sqlite3.Row) -> AgentGrantRow:
    return AgentGrantRow(
        agent_name=row["agent_name"],
        workspace_name=row["workspace_name"],
        grant_type=row["grant_type"],
        session_name=row["session_name"],
        created_at=row["created_at"],
    )


def _to_session(row: sqlite3.Row) -> SessionRow:
    return SessionRow(
        name=row["name"],
        workspace_name=row["workspace_name"],
        template=row["template"],
        mode=row["mode"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        agent_name=row["agent_name"],
        created_workspace=bool(row["created_workspace"]),
        created_agent=bool(row["created_agent"]),
        socket_path=row["socket_path"],
        pid=row["pid"],
        boot_id=row["boot_id"],
        harness_state=_parse_harness_state(row["harness_state"], row["name"]),
    )


def _parse_harness_state(raw: str, session_name: str) -> dict[str, object]:
    """Decode the harness_state JSON column. The blob is harness-owned and
    opaque to the core, so this only checks it is a JSON object.

    A malformed or non-object blob (a future harness bug, a hand-edited DB)
    degrades to ``{}`` with a warning rather than raising: ``_to_session``
    is mapped over every row by ``list_sessions``, so a single corrupt row
    must not break ``session list`` (and every other read) for all the
    others. A blank harness starts fresh from ``{}``; a stateful one
    re-mints on its next op, the same as an unmigrated row.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn_bad_harness_state(session_name, f"invalid JSON ({exc})")
        return {}
    if not isinstance(decoded, dict):
        _warn_bad_harness_state(session_name, f"expected a JSON object, got {type(decoded).__name__}")
        return {}
    return decoded


def _warn_bad_harness_state(session_name: str, detail: str) -> None:
    from agentworks import output

    output.warn(f"session '{session_name}': ignoring malformed harness_state ({detail}); treating it as empty.")


def _to_vm_event(row: sqlite3.Row) -> VMEventRow:
    return VMEventRow(
        id=row["id"],
        vm_name=row["vm_name"],
        event=row["event"],
        detail=row["detail"],
        created_at=row["created_at"],
    )


def _to_console(row: sqlite3.Row) -> ConsoleRow:
    return ConsoleRow(
        name=row["name"],
        vm_name=row["vm_name"],
        admin_shell=bool(row["admin_shell"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_console_session(row: sqlite3.Row) -> ConsoleSessionRow:
    return ConsoleSessionRow(
        console_name=row["console_name"],
        session_name=row["session_name"],
        position=row["position"],
        shells=_parse_shells(row["shells"], row["console_name"], row["session_name"]),
    )


def _parse_shells(raw: str, console_name: str, session_name: str) -> list[ShellEntry]:
    """Decode the shells JSON column and verify it matches ShellEntry shape."""
    where = f"console_sessions[{console_name!r}, {session_name!r}].shells"
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{where}: invalid JSON ({exc})") from None
    if not isinstance(decoded, list):
        raise ValueError(f"{where}: expected list, got {type(decoded).__name__}")
    for i, entry in enumerate(decoded):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}[{i}]: expected dict, got {type(entry).__name__}")
        extra = set(entry.keys()) - {"cwd", "admin"}
        missing = {"cwd", "admin"} - set(entry.keys())
        if extra or missing:
            raise ValueError(
                f"{where}[{i}]: keys must be exactly cwd, admin (extra={sorted(extra)}, missing={sorted(missing)})"
            )
        if entry["cwd"] is not None and not isinstance(entry["cwd"], str):
            raise ValueError(f"{where}[{i}].cwd: expected str or null")
        if not isinstance(entry["admin"], bool):
            raise ValueError(f"{where}[{i}].admin: expected bool")
    return decoded
