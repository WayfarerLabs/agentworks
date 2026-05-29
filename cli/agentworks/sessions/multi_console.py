"""Named consoles with explicit session lists.

A console is a named tmux session on a VM that aggregates a curated subset of
the VM's sessions as windows, with optional extra shell panes per session
window. Unlike the legacy vm-console (one per VM, holds all sessions), a
console is created explicitly with a chosen set of sessions and can be
attached, modified, or deleted independently.

This module handles DB-level orchestration and validation. Tmux orchestration
is layered on top in a companion module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name

if TYPE_CHECKING:
    from agentworks.db import ConsoleRow, Database, SessionRow, ShellEntry

TMUX_PREFIX = "aw-console-"


def tmux_session_name(console_name: str) -> str:
    """Return the tmux session name for a console."""
    return f"{TMUX_PREFIX}{console_name}"


# -- Spec parsing ----------------------------------------------------------


@dataclass(frozen=True)
class SessionSpec:
    """A session name plus a default-shell count requested via '+N' shorthand."""

    name: str
    shells: int


def parse_session_spec(spec: str) -> SessionSpec:
    """Parse 'session' or 'session+N' into a SessionSpec.

    The shell count N must be a non-negative integer. Raises ValidationError
    on syntax errors or invalid session names; does not check existence.
    """
    parts = spec.split("+")
    if len(parts) == 1:
        name = parts[0]
        shells = 0
    elif len(parts) == 2:
        name = parts[0]
        try:
            shells = int(parts[1])
        except ValueError:
            raise output.ValidationError(
                f"invalid session spec '{spec}': shell count must be a non-negative integer"
            ) from None
        if shells < 0:
            raise output.ValidationError(
                f"invalid session spec '{spec}': shell count must be >= 0"
            )
    else:
        raise output.ValidationError(
            f"invalid session spec '{spec}': use 'name' or 'name+N'"
        )
    try:
        validate_name(name)
    except output.ValidationError as exc:
        raise output.ValidationError(f"invalid session spec '{spec}': {exc}") from None
    return SessionSpec(name=name, shells=shells)


def default_shells(count: int) -> list[ShellEntry]:
    """Build N default shell entries (agent user, workspace root)."""
    return [{"cwd": None, "admin": False} for _ in range(count)]


# -- Helpers ---------------------------------------------------------------


def _require_console(db: Database, name: str) -> ConsoleRow:
    console = db.get_console(name)
    if console is None:
        raise output.ConsoleError(f"console '{name}' not found")
    return console


def _vm_sessions(db: Database, vm_name: str) -> list[SessionRow]:
    """All sessions belonging to workspaces on the given VM."""
    sessions: list[SessionRow] = []
    for ws in db.list_workspaces(vm_name=vm_name):
        sessions.extend(db.list_sessions(workspace_name=ws.name))
    return sessions


def _verify_session_on_vm(db: Database, session_name: str, vm_name: str) -> None:
    """Raise if the session does not exist or is not on the given VM."""
    session = db.get_session(session_name)
    if session is None:
        raise output.SessionError(f"session '{session_name}' not found")
    ws = db.get_workspace(session.workspace_name)
    if ws is None or ws.vm_name != vm_name:
        raise output.ConsoleError(
            f"session '{session_name}' is not on VM '{vm_name}'"
        )


def _dedupe_specs(specs: list[SessionSpec]) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise output.ConsoleError(f"session '{spec.name}' listed more than once")
        seen.add(spec.name)


def _shell_summary(shells: list[ShellEntry]) -> str:
    if not shells:
        return "no extra shells"
    parts = []
    for s in shells:
        cwd = s.get("cwd") or "<workspace>"
        user_tag = "admin" if s.get("admin", False) else "agent"
        parts.append(f"{user_tag}:{cwd}")
    return f"{len(shells)} shell(s): " + ", ".join(parts)


# -- Orchestration (DB only; tmux side handled by companion module) -------


def create_console(
    db: Database,
    *,
    name: str,
    vm_name: str,
    session_specs: list[str],
    fill_all: bool = False,
) -> None:
    """Create a new console with the given sessions.

    Explicit *session_specs* keep their argument order. When *fill_all* is
    True, every other session on the VM is appended in alphabetical order
    with zero shells. All inserts run in one transaction; the console is
    not created if any step fails.
    """
    validate_name(name)

    if db.get_console(name) is not None:
        raise output.ConsoleError(f"console '{name}' already exists")
    if db.get_vm(vm_name) is None:
        raise output.VMError(f"VM '{vm_name}' not found")

    specs = [parse_session_spec(s) for s in session_specs]
    _dedupe_specs(specs)
    for spec in specs:
        _verify_session_on_vm(db, spec.name, vm_name)

    if fill_all:
        explicit_names = {s.name for s in specs}
        extras = sorted(
            s.name for s in _vm_sessions(db, vm_name) if s.name not in explicit_names
        )
        specs.extend(SessionSpec(name=n, shells=0) for n in extras)

    if not specs:
        # fill_all on a VM with no other sessions, and no explicit specs --
        # almost certainly a typo / misunderstanding rather than an empty console.
        raise output.ConsoleError(
            f"refusing to create empty console '{name}' "
            f"(no sessions specified, and VM '{vm_name}' has none to fill)"
        )

    with db.transaction():
        db.insert_console(name, vm_name)
        for spec in specs:
            db.add_console_session(name, spec.name, default_shells(spec.shells))

    output.info(f"Console '{name}' created with {len(specs)} session(s).")


def add_sessions(
    db: Database,
    *,
    console_name: str,
    session_specs: list[str],
) -> None:
    """Append sessions to an existing console in argument order. Atomic."""
    console = _require_console(db, console_name)
    specs = [parse_session_spec(s) for s in session_specs]
    _dedupe_specs(specs)

    for spec in specs:
        _verify_session_on_vm(db, spec.name, console.vm_name)
        if db.get_console_session(console_name, spec.name) is not None:
            raise output.ConsoleError(
                f"session '{spec.name}' is already a member of console '{console_name}'"
            )

    with db.transaction():
        for spec in specs:
            db.add_console_session(console_name, spec.name, default_shells(spec.shells))

    output.info(f"Added {len(specs)} session(s) to console '{console_name}'.")


def remove_sessions(
    db: Database,
    *,
    console_name: str,
    session_names: list[str],
) -> None:
    """Remove sessions from a console. Raises if any are not members. Atomic."""
    _require_console(db, console_name)
    for n in session_names:
        if db.get_console_session(console_name, n) is None:
            raise output.ConsoleError(
                f"session '{n}' is not a member of console '{console_name}'"
            )
    with db.transaction():
        for n in session_names:
            db.remove_console_session(console_name, n)
    output.info(
        f"Removed {len(session_names)} session(s) from console '{console_name}'."
    )


def delete_console_record(db: Database, *, name: str) -> None:
    """Delete the DB record for a console. Cascade handles its session list.

    Tmux teardown is the caller's responsibility.
    """
    _require_console(db, name)
    db.delete_console(name)
    output.info(f"Console '{name}' deleted.")


def add_shell(
    db: Database,
    *,
    console_name: str,
    session_name: str,
    cwd: str | None = None,
    admin: bool = False,
) -> None:
    """Append a single shell entry to a session's window in a console."""
    _require_console(db, console_name)
    cs = db.get_console_session(console_name, session_name)
    if cs is None:
        raise output.ConsoleError(
            f"session '{session_name}' is not a member of console '{console_name}'"
        )
    new_shells = list(cs.shells) + [{"cwd": cwd, "admin": admin}]
    db.update_console_shells(console_name, session_name, new_shells)
    user_tag = "admin" if admin else "agent"
    output.info(
        f"Added {user_tag} shell at {cwd or '<workspace>'} to '{session_name}' "
        f"in console '{console_name}'."
    )


# -- Read-side helpers ----------------------------------------------------


def list_consoles(db: Database, *, vm_name: str | None = None) -> None:
    """Print a table of consoles, optionally filtered by VM."""
    consoles = db.list_consoles_with_counts(vm_name=vm_name)
    if not consoles:
        output.info("No consoles found.")
        return

    rows = [(c.name, c.vm_name, str(n)) for c, n in consoles]
    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    vm_w = max(len("VM"), max(len(r[1]) for r in rows))

    header = f"{'NAME':<{name_w}}  {'VM':<{vm_w}}  SESSIONS"
    output.info(header)
    output.info("-" * len(header))
    for n, vm, count in rows:
        output.info(f"{n:<{name_w}}  {vm:<{vm_w}}  {count}")


def describe_console(db: Database, *, name: str) -> None:
    """Print console membership and shell layout."""
    console = _require_console(db, name)
    members = db.list_console_sessions(name)

    output.info(f"Name:     {console.name}")
    output.info(f"VM:       {console.vm_name}")
    output.info(f"Created:  {console.created_at}")
    output.info(f"Updated:  {console.updated_at}")
    output.info(f"Sessions: {len(members)}")

    if not members:
        return

    output.info("")
    for i, m in enumerate(members):
        output.info(f"  [{i}] {m.session_name}  ({_shell_summary(m.shells)})")
