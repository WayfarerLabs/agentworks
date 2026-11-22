"""A session's tmux server names one account: its socket directory's."""

from __future__ import annotations

import pytest

from agentworks.sessions.manager import SessionTmuxServer

SOCKET = "/run/agentworks/agent-tmux-sockets/agt-owner/s1.sock"


def test_an_agent_server_runs_as_the_account_its_socket_path_names() -> None:
    assert SessionTmuxServer(SOCKET, "agt-owner").owner == "agt-owner"
    assert SessionTmuxServer("/run/agentworks/admin-tmux-sockets/admin/s1.sock", None).owner is None


def test_a_server_whose_owner_disagrees_with_its_socket_path_is_refused() -> None:
    with pytest.raises(ValueError, match="agt-other"):
        SessionTmuxServer(SOCKET, "agt-other")
