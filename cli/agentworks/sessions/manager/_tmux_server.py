"""The one rule naming a session's tmux server and the account that runs it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.db import SessionMode
from agentworks.errors import StateError
from agentworks.sessions.tmux import socket_owner

if TYPE_CHECKING:
    from agentworks.db import Database, SessionRow


@dataclass(frozen=True)
class SessionTmuxServer:
    """A session's validated dedicated tmux server.

    ``owner`` is ``None`` for the VM admin's own server, which the admin's
    access reaches directly. Otherwise it names the agent account that runs
    the server, and every tmux client the admin's access starts against it
    runs as ``owner``. Production code builds it only through
    ``resolve_session_tmux_server``, so holding one there means the row
    passed the whole owner rule.
    """

    socket_path: str
    owner: str | None

    @property
    def account(self) -> str:
        """The account that runs the server, the admin's included: its socket directory's name.

        The socket guard checks the socket belongs to it
        (``tmux.as_socket_owner_line``).
        """
        return PurePosixPath(self.socket_path).parent.name


def resolve_session_tmux_server(db: Database, session: SessionRow) -> SessionTmuxServer | None:
    """Validate a persisted row's tmux server before any VM work touches it.

    Returns ``None`` for a legacy row without a socket path, whose clients use
    the connecting account's own default server. Otherwise the stored path
    must satisfy ``socket_owner`` for the session's VM admin, its owner must
    match the row's mode, and an agent row's owner must be that agent's own
    Linux account. Raises ``StateError`` when any of these fails: such a row
    is refused or reported unknown, and never probed.
    """
    socket_path = session.socket_path
    if socket_path is None:
        return None
    workspace = db.get_workspace(session.workspace_name)
    vm = db.get_vm(workspace.vm_name) if workspace is not None else None
    if vm is None:
        raise StateError(
            f"session '{session.name}' has no VM to resolve its tmux server owner",
            entity_kind="session",
            entity_name=session.name,
        )
    owner = socket_owner(socket_path, admin_username=vm.admin_username)
    if (owner is not None) != (session.mode == SessionMode.AGENT.value):
        raise StateError(
            f"session '{session.name}' has no managed tmux socket for its mode",
            entity_kind="session",
            entity_name=session.name,
            hint="Repair the persisted session row before retrying.",
        )
    if owner is not None:
        agent = db.get_agent(session.agent_name or "")
        if agent is None or agent.linux_user != owner:
            raise StateError(
                f"session '{session.name}' has a tmux socket outside its agent's directory",
                entity_kind="session",
                entity_name=session.name,
                hint="Repair the persisted session row before retrying.",
            )
    return SessionTmuxServer(socket_path=socket_path, owner=owner)


def require_session_tmux_server(db: Database, session: SessionRow) -> SessionTmuxServer:
    """``resolve_session_tmux_server`` for work that needs a dedicated server.

    A legacy row without a socket path raises ``StateError``.
    """
    server = resolve_session_tmux_server(db, session)
    if server is None:
        raise StateError(
            f"session '{session.name}' has no dedicated tmux socket",
            entity_kind="session",
            entity_name=session.name,
        )
    return server
