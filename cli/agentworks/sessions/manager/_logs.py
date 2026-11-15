"""``session_logs``: dump a session's scrollback buffer."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.db import SessionMode, SessionStatus
from agentworks.errors import (
    BrokenStateError,
    StateError,
    ValidationError,
)
from agentworks.machine_output import write_all

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.execution.access import VMAccess
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.sessions.tmux import RunCommand


def session_logs(
    db: Database,
    config: Config,
    *,
    name: str,
    lines: int | None = None,
    interaction: TtyInteractionPolicy,
) -> None:
    """Dump the scrollback buffer for a session."""
    from agentworks.sessions.tmux import capture_output

    if lines is not None and lines < 1:
        raise ValidationError("session log line count must be positive")
    session = _mgr._require_session(db, name)
    with _mgr._prepare_vm(
        db,
        config,
        session,
        operation="session-logs",
        interaction=interaction,
    ) as (_ws, _vm, admin, _log):
        status = _mgr.check_session_status(session, admin=admin, db=db)

        if status == SessionStatus.STOPPED:
            raise StateError(
                f"session '{name}' is not running",
                entity_kind="session",
                entity_name=name,
            )
        if status == SessionStatus.UNKNOWN:
            raise StateError(
                f"session '{name}' runtime state is unknown",
                entity_kind="session",
                entity_name=name,
                hint="Retry after transport access is reliable.",
            )
        if status == SessionStatus.RESIDUAL:
            raise StateError(
                f"session '{name}' has a residual tmux server but no canonical session",
                entity_kind="session",
                entity_name=name,
                hint=f"Run `agw session start {name}` or `agw session restart {name}` to recover it.",
            )
        if status == SessionStatus.BROKEN:
            raise BrokenStateError(
                f"session '{name}' is broken (PID alive but tmux unreachable).",
                entity_kind="session",
                entity_name=name,
            )

        captured = capture_output(
            name,
            run_command=_capture_run_command(db, session, admin=admin),
            lines=lines or config.session.history_limit,
            socket_path=session.socket_path,
        )
        # Raw data pipe (opaque tmux capture-pane output), not a structured message.
        # Intentionally not routed through the output handler.
        _write_raw_capture(captured)


def _capture_run_command(db: Database, session: SessionRow, *, admin: VMAccess) -> RunCommand:
    """The admin's unbounded capture of ``session``'s pane, reaching its tmux server only as its owner.

    A legacy row's server is the admin's own default one. A dedicated
    server's capture runs behind the socket guard, an agent session's as the
    agent (``socket_owner_run_command``).
    """
    from agentworks.sessions.manager._teardown import _validated_socket
    from agentworks.sessions.tmux import access_run_command, socket_owner_run_command

    if session.socket_path is None:
        return access_run_command(admin, deadline=None, elevated=False)
    socket = _validated_socket(db, session)
    return socket_owner_run_command(
        admin,
        socket_path=socket.path,
        owner=socket.owner,
        switch_user=session.mode == SessionMode.AGENT.value,
        deadline=None,
    )


def _write_raw_capture(captured: str) -> None:
    """Write scrollback text as exact UTF-8 bytes, bypassing text-mode stdout.

    Session scrollback can hold arbitrary Unicode captured verbatim from a
    workload (it is the raw data pipe the module docstring describes, not
    a structured message), so this is a third Unicode boundary distinct
    from the two the CLI entrypoint already owns: terminal display, which
    degrades unencodable characters instead of crashing
    (``_reconfigure_std_streams`` in ``cli/_entry.py``), and the
    machine-output JSON layer, which already writes exact UTF-8 through
    its own binary writer. Routing this payload through the reconfigured
    text-mode ``sys.stdout`` would re-encode it into a legacy console's
    codepage and silently alias distinct input (a genuine non-ASCII
    character and a literal ``?``) onto the same output byte, corrupting
    a redirected capture. Writing straight to the binary buffer keeps
    this boundary byte-for-byte regardless of the console's codepage.

    Streams without a ``.buffer`` (the ``StringIO``-like stand-ins tests
    and embedders install, mirroring the guard in
    ``_reconfigure_std_streams``) fall back to the text write.
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        write_all(captured.encode("utf-8"), buffer)
        buffer.flush()
    else:
        sys.stdout.write(captured)
        sys.stdout.flush()
