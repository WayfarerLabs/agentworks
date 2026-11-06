"""Tests for the factory functions in :mod:`agentworks.transports`: ``transport``,
``agent_transport`` and their helper ``transport_for_user``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.errors import StateError
from agentworks.transports import (
    SSHTransport,
    agent_transport,
    transport,
    transport_for_user,
)


def _mock_vm(
    *,
    name: str = "vm1",
    tailscale_host: str | None = "100.64.0.1",
    admin_username: str = "agentworks",
    site: str = "lima",
) -> MagicMock:
    vm = MagicMock()
    vm.name = name
    vm.tailscale_host = tailscale_host
    vm.admin_username = admin_username
    vm.site = site
    return vm


def _mock_config() -> MagicMock:
    config = MagicMock()
    config.operator.ssh_private_key = Path("/home/op/.ssh/agentworks_ed25519")
    return config


def _mock_agent(linux_user: str = "claude") -> MagicMock:
    a = MagicMock()
    a.linux_user = linux_user
    a.name = linux_user
    return a


# ---------------------------------------------------------------------------
# transport_for_user / transport / agent_transport
# ---------------------------------------------------------------------------


def test_transport_for_user_returns_ssh_transport_with_explicit_user() -> None:
    t = transport_for_user(_mock_vm(), _mock_config(), user="alice")
    assert isinstance(t, SSHTransport)
    assert t.host == "100.64.0.1"
    assert t.user == "alice"


def test_transport_for_user_always_uses_operator_key() -> None:
    """``transport_for_user`` always builds the SSHTransport with
    ``config.operator.ssh_private_key`` as the identity. There is no
    override -- the operator's key is the only credential the on-VM
    authorized_keys reconciler installs, so any other path would
    fail auth anyway. Pinning this prevents the regression class
    where a caller forgets to pass identity_file and SSH falls back
    to ``~/.ssh/id_*`` defaults (works on Linux by accident, fails
    on Windows / non-standard key names).
    """
    config = _mock_config()
    t = transport_for_user(_mock_vm(), config, user="alice")
    assert isinstance(t, SSHTransport)
    assert t.identity_file == config.operator.ssh_private_key


def test_transport_for_user_raises_state_error_without_tailscale_host() -> None:
    """The pre-refactor code asserted (disappears under ``python -O``);
    the new factory promotes this to a typed error per SDD R6.
    """
    vm = _mock_vm(tailscale_host=None)
    with pytest.raises(StateError, match="no Tailscale host"):
        transport_for_user(vm, _mock_config(), user="alice")


def test_transport_uses_admin_username() -> None:
    t = transport(_mock_vm(), _mock_config())
    assert isinstance(t, SSHTransport)
    assert t.user == "agentworks"
    assert t.identity_file == Path("/home/op/.ssh/agentworks_ed25519")


def test_agent_transport_uses_agent_linux_user() -> None:
    t = agent_transport(_mock_vm(), _mock_config(), _mock_agent("claude"))
    assert isinstance(t, SSHTransport)
    assert t.user == "claude"
    assert t.host == "100.64.0.1"


def test_admin_and_agent_transports_differ_only_in_user() -> None:
    vm = _mock_vm()
    config = _mock_config()
    admin = transport(vm, config)
    agent = agent_transport(vm, config, _mock_agent("claude"))
    assert isinstance(admin, SSHTransport)
    assert isinstance(agent, SSHTransport)
    assert admin.host == agent.host
    assert admin.identity_file == agent.identity_file
    assert admin.user == "agentworks"
    assert agent.user == "claude"
