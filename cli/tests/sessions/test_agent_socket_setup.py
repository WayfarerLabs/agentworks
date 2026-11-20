"""Agent tmux socket setup: ``ensure_agent_socket_root`` and ``ensure_agent_socket_dir``.

Each helper probes the directory first. The probe's answer decides whether setup
runs and whether the operator is warned; which commands need root decides how
each one is issued.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from agentworks.sessions.tmux import AGENT_SOCKET_GROUP, ensure_agent_socket_dir, ensure_agent_socket_root
from agentworks.ssh import SSHResult

if TYPE_CHECKING:
    from agentworks.transports import Transport

AGENT = "agt--alice"


class _ProbedTarget:
    """Answers the first command (the probe) with ``probe_stdout`` and every later one
    with success, recording whether each was issued with ``sudo``."""

    def __init__(self, probe_stdout: str) -> None:
        self._probe_stdout = probe_stdout
        self.sudo: list[bool] = []

    def run(self, command: str, *, sudo: bool = False, check: bool = True, **_: object) -> SSHResult:
        first = not self.sudo
        self.sudo.append(sudo)
        return SSHResult(returncode=0, stdout=self._probe_stdout if first else "", stderr="")


# Probe, then the unprivileged group lookup, then the privileged setup.
_ROOT_SETUP = [True, False, True, True, True, True]
# Probe, then membership of the admin in the group: both need root.
_ROOT_FAST_PATH = [True, True]


@pytest.mark.parametrize(
    ("probe", "warn_if_missing", "warned", "issued"),
    [
        pytest.param("MISSING", True, 1, _ROOT_SETUP, id="missing"),
        pytest.param("MISSING", False, 0, _ROOT_SETUP, id="missing-expected"),
        pytest.param("root 755", False, 1, _ROOT_SETUP, id="misconfigured"),
        pytest.param("PROBE_FAILED", False, 1, _ROOT_SETUP, id="probe-failed"),
        pytest.param(f"{AGENT_SOCKET_GROUP} 2771", True, 0, _ROOT_FAST_PATH, id="ready"),
    ],
)
def test_socket_root_setup_follows_the_probe(
    warnings: list[str], probe: str, warn_if_missing: bool, warned: int, issued: list[bool]
) -> None:
    target = _ProbedTarget(probe)

    ensure_agent_socket_root(cast("Transport", target), "agentworks", warn_if_missing=warn_if_missing)

    assert len(warnings) == warned
    assert target.sudo == issued


# Probe, then the directory's creation, ownership and mode: all need root.
_DIR_SETUP = [True, True, True, True]


@pytest.mark.parametrize(
    ("probe", "warn_if_missing", "warned", "issued"),
    [
        pytest.param("MISSING", True, 1, _DIR_SETUP, id="missing"),
        pytest.param("MISSING", False, 0, _DIR_SETUP, id="missing-expected"),
        pytest.param("root root 755", False, 1, _DIR_SETUP, id="misconfigured"),
        pytest.param(f"agt--bob {AGENT_SOCKET_GROUP} 2770", False, 1, _DIR_SETUP, id="other-owner"),
        pytest.param("PROBE_FAILED", False, 1, _DIR_SETUP, id="probe-failed"),
        pytest.param(f"{AGENT} {AGENT_SOCKET_GROUP} 2770", True, 0, [True], id="ready"),
    ],
)
def test_socket_dir_setup_follows_the_probe(
    warnings: list[str], probe: str, warn_if_missing: bool, warned: int, issued: list[bool]
) -> None:
    target = _ProbedTarget(probe)

    ensure_agent_socket_dir(cast("Transport", target), AGENT, warn_if_missing=warn_if_missing)

    assert len(warnings) == warned
    assert all(AGENT in warning for warning in warnings)
    assert target.sudo == issued
