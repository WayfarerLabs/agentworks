"""``session_logs``: dump a session's scrollback buffer."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.db import SessionStatus
from agentworks.errors import (
    BrokenStateError,
    StateError,
)
from agentworks.machine_output import write_all

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.secrets.policy import TtyInteractionPolicy


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

    session = _mgr._require_session(db, name)
    with _mgr._prepare_vm(
        db,
        config,
        session,
        operation="session-logs",
        interaction=interaction,
    ) as (
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

        sock = session.socket_path
        captured = capture_output(
            name,
            run_command=run_command,
            lines=lines or config.session.history_limit,
            socket_path=sock,
        )
        # Raw data pipe (opaque tmux capture-pane output), not a structured message.
        # Intentionally not routed through the output handler.
        _write_raw_capture(captured)


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
