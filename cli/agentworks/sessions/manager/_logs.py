"""``session_logs``: dump a session's scrollback buffer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

import agentworks.sessions.manager as _mgr
from agentworks.db import SessionStatus
from agentworks.errors import (
    BrokenStateError,
    StateError,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database


def session_logs(
    db: Database,
    config: Config,
    *,
    name: str,
    lines: int | None = None,
) -> None:
    """Dump the scrollback buffer for a session."""
    from agentworks.sessions.tmux import capture_output

    session = _mgr._require_session(db, name)
    with _mgr._prepare_vm(db, config, session, operation="session-logs") as (
        _ws,
        _vm,
        run_command,
        _run_as_root,
        target,
    ):
        session = _mgr._ensure_pid(session, target=target, db=db)
        status = _mgr.check_session_status(session, target=target)

        if status == SessionStatus.STOPPED:
            raise StateError(
                f"session '{name}' is not running",
                entity_kind="session",
                entity_name=name,
            )
        if status == SessionStatus.BROKEN:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
            )

        sock = session.socket_path
        captured = capture_output(
            name,
            run_command=run_command,
            lines=lines or config.session.history_limit,
            socket_path=sock,
        )
        # Raw data pipe (opaque tmux capture-pane output), not a structured message.
        # Intentionally not routed through the output handler.
        typer.echo(captured, nl=False)
