"""Shared constants, spec parsing, and read-only DB helpers for named consoles.

Carved out of the ``multi_console`` package's top level so that ``crud``,
``attach``, ``restore``, ``secrets_env``, and ``tmux_build`` all have a
dependency-free base to import from. Nothing here talks to tmux or opens a
live transport; that lives in ``tmux_build`` / ``attach``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.config import validate_name
from agentworks.errors import ConnectivityError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import ConsoleRow, Database, SessionRow, ShellEntry

TMUX_PREFIX = "aw-console-"

# Literal tmux window name for the optional admin-shell window. Wrapped in
# double hyphens so it cannot collide with any session name: validate_name
# rejects leading hyphens, consecutive hyphens, and trailing hyphens.
ADMIN_SHELL_WINDOW = "--admin--"


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

    The shell count N must be a non-negative integer. The session name uses
    the loose reference form of validate_name (``allow_double_hyphen=True``)
    so legacy sessions with the pre-rename ``ws--agent`` convention can
    still be referenced; the DB is the ultimate arbiter of existence and is
    checked downstream by the caller.
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
            raise ValidationError(
                f"invalid session spec '{spec}': shell count must be a non-negative integer"
            ) from None
        if shells < 0:
            raise ValidationError(f"invalid session spec '{spec}': shell count must be >= 0")
    else:
        raise ValidationError(f"invalid session spec '{spec}': use 'name' or 'name+N'")
    try:
        validate_name(name, allow_double_hyphen=True)
    except ValidationError as exc:
        raise ValidationError(f"invalid session spec '{spec}': {exc}") from None
    return SessionSpec(name=name, shells=shells)


def default_shells(count: int) -> list[ShellEntry]:
    """Build N default shell entries (agent user, workspace root)."""
    return [{"cwd": None, "admin": False} for _ in range(count)]


# -- Helpers ---------------------------------------------------------------


def _require_console(db: Database, name: str) -> ConsoleRow:
    console = db.get_console(name)
    if console is None:
        raise NotFoundError(
            f"console '{name}' not found",
            entity_kind="console",
            entity_name=name,
        )
    return console


def _vm_sessions(db: Database, vm_name: str) -> list[SessionRow]:
    """All sessions belonging to workspaces on the given VM."""
    sessions: list[SessionRow] = []
    for ws in db.list_workspaces(vm_name=vm_name):
        sessions.extend(db.list_sessions(workspace_name=ws.name))
    return sessions


def running_session_names(db: Database, config: Config, vm_name: str) -> list[str]:
    """SSH-probe the VM and return names of sessions whose live tmux state is OK.

    Uses the same one-round-trip-per-VM check that powers ``aw session list``.
    Returns alphabetically sorted names.

    Raises ConnectivityError when the VM has sessions eligible to be probed
    (valid PID + boot_id) but the probe came back empty -- almost always a
    transport failure that we don't want to silently report as "nothing
    running". A VM with zero eligible sessions simply returns an empty list.
    """
    from agentworks.db import PID_STOPPED, SessionStatus
    from agentworks.sessions.manager import batch_check_all_sessions, filter_sessions

    sessions = filter_sessions(db, vm_name=vm_name)
    status_map = batch_check_all_sessions(sessions, db=db, config=config)

    # If we have sessions that *should* have been probed but none came back
    # with a status, the probe almost certainly failed (e.g. SSH unreachable).
    # batch_check_all_sessions warns on exceptions but returns silently on
    # `check=False` non-zero exits, so we cannot rely on the warning alone.
    checkable = [s for s in sessions if s.pid is not None and s.pid != PID_STOPPED and s.pid > 0 and s.boot_id]
    if checkable and not status_map:
        raise ConnectivityError(
            f"could not determine running sessions on VM '{vm_name}' (status probe returned no results)",
            entity_kind="vm",
            entity_name=vm_name,
            hint="Check VM reachability.",
        )

    return sorted(s.name for s in sessions if status_map.get(s.name) == SessionStatus.OK)


def infer_vm_from_session_specs(db: Database, session_specs: list[str]) -> str | None:
    """Return the single VM hosting all listed sessions.

    - Returns None if *session_specs* is empty or none of the names resolve to
      a known session (callers prompt for --vm or surface the not-found error
      from create_console).
    - Raises ValidationError if listed sessions span more than one VM (the
      user must disambiguate with --vm explicitly).
    """
    if not session_specs:
        return None

    vms: set[str] = set()
    for spec in session_specs:
        try:
            session_name = parse_session_spec(spec).name
        except ValidationError:
            # Bad spec -- defer the error to create_console's own validation.
            continue
        session = db.get_session(session_name)
        if session is None:
            continue
        ws = db.get_workspace(session.workspace_name)
        if ws and ws.vm_name:
            vms.add(ws.vm_name)

    if len(vms) > 1:
        raise ValidationError(
            f"sessions span multiple VMs ({', '.join(sorted(vms))})",
            entity_kind="console",
            hint="Pass --vm to pick one.",
        )
    return next(iter(vms)) if vms else None


def _verify_session_on_vm(db: Database, session_name: str, vm_name: str) -> None:
    """Raise if the session does not exist or is not on the given VM."""
    session = db.get_session(session_name)
    if session is None:
        raise NotFoundError(
            f"session '{session_name}' not found",
            entity_kind="session",
            entity_name=session_name,
        )
    ws = db.get_workspace(session.workspace_name)
    if ws is None or ws.vm_name != vm_name:
        raise ValidationError(
            f"session '{session_name}' is not on VM '{vm_name}'",
            entity_kind="session",
            entity_name=session_name,
        )


def _dedupe_specs(specs: list[SessionSpec]) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ValidationError(
                f"session '{spec.name}' listed more than once",
                entity_kind="session",
                entity_name=spec.name,
            )
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
