"""Agent tmux socket setup: ``ensure_agent_socket_root`` and ``ensure_agent_socket_dir``.

Each helper probes the directory first. The probe's answer decides whether setup
runs and whether the operator is warned; which commands need root decides how
each one is issued.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any, cast

import pytest

from agentworks.sessions.tmux import AGENT_SOCKET_GROUP, ensure_agent_socket_dir, ensure_agent_socket_root
from agentworks.ssh import SSHResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.transports import Transport

AGENT = "agt--alice"


class _SocketGuest:
    """A guest holding at most the one directory a helper sets up.

    The probe reports the directory in the ``stat`` format it asks for, or that it is
    missing or cannot be read; ``mkdir``, ``chown`` and ``chmod`` change it; and
    ``getent group`` reports whether the group exists. Records whether each command
    was issued with ``sudo``.
    """

    def __init__(
        self,
        directory: tuple[str, str, str] | None = None,
        *,
        stat_fails: bool = False,
        group_exists: bool = True,
    ) -> None:
        self.directory = directory  # (owner, group, mode)
        self._stat_fails = stat_fails
        self._group_exists = group_exists
        self.sudo: list[bool] = []

    def run(self, command: str, *, sudo: bool = False, check: bool = True, **_: object) -> SSHResult:
        self.sudo.append(sudo)
        words = shlex.split(command)
        stdout, returncode = "", 0
        if words[:2] == ["if", "test"]:
            stdout = self._probe(command)
        elif words[:2] == ["getent", "group"]:
            returncode = 0 if self._group_exists else 2
        elif words[0] == "mkdir" and self.directory is None:
            self.directory = ("root", "root", "755")
        elif words[0] == "chown" and self.directory is not None:
            owner, group = words[1].split(":")
            self.directory = (owner, group, self.directory[2])
        elif words[0] == "chmod" and self.directory is not None:
            self.directory = (*self.directory[:2], words[1])
        return SSHResult(returncode=returncode, stdout=stdout, stderr="")

    def _probe(self, command: str) -> str:
        if self.directory is None:
            return "MISSING"
        if self._stat_fails:
            return "PROBE_FAILED"
        found = re.search(r'stat -c "([^"]+)"', command)
        assert found is not None, "the probe reads the directory with stat"
        owner, group, mode = self.directory
        return found[1].replace("%U", owner).replace("%G", group).replace("%a", mode)


# Probe, then the unprivileged group lookup, then the privileged setup.
_ROOT_SETUP = [True, False, True, True, True, True]
# The same, with the group created between the lookup and the setup.
_ROOT_SETUP_NEW_GROUP = [True, False, True, True, True, True, True]
# Probe, then membership of the admin in the group: both need root.
_ROOT_FAST_PATH = [True, True]
_QUIET = {"warn_if_missing": False}
_LOUD = {"warn_if_missing": True}
# A directory no helper accepts.
_WRONG = ("root", "root", "755")


@pytest.mark.parametrize(
    ("state", "options", "warned", "issued"),
    [
        pytest.param({}, _LOUD, 1, _ROOT_SETUP, id="missing"),
        pytest.param({}, _QUIET, 0, _ROOT_SETUP, id="missing-expected"),
        pytest.param({"group_exists": False}, _QUIET, 0, _ROOT_SETUP_NEW_GROUP, id="group-missing"),
        pytest.param({"directory": _WRONG}, _QUIET, 1, _ROOT_SETUP, id="misconfigured"),
        pytest.param({"directory": _WRONG, "stat_fails": True}, _QUIET, 1, _ROOT_SETUP, id="probe-failed"),
        pytest.param({"directory": ("root", AGENT_SOCKET_GROUP, "2771")}, _LOUD, 0, _ROOT_FAST_PATH, id="ready"),
    ],
)
def test_socket_root_setup_follows_the_probe(
    warnings: list[str], state: dict[str, Any], options: dict[str, bool], warned: int, issued: list[bool]
) -> None:
    guest = _SocketGuest(**state)

    ensure_agent_socket_root(cast("Transport", guest), "agentworks", **options)

    assert len(warnings) == warned
    assert guest.sudo == issued


# Probe, then the directory's creation, ownership and mode: all need root.
_DIR_SETUP = [True, True, True, True]


@pytest.mark.parametrize(
    ("state", "options", "warned", "issued"),
    [
        pytest.param({}, _LOUD, 1, _DIR_SETUP, id="missing"),
        pytest.param({}, _QUIET, 0, _DIR_SETUP, id="missing-expected"),
        pytest.param({"directory": _WRONG}, _QUIET, 1, _DIR_SETUP, id="misconfigured"),
        pytest.param({"directory": ("agt--bob", AGENT_SOCKET_GROUP, "2770")}, _QUIET, 1, _DIR_SETUP, id="other-owner"),
        pytest.param({"directory": _WRONG, "stat_fails": True}, _QUIET, 1, _DIR_SETUP, id="probe-failed"),
        pytest.param({"directory": (AGENT, AGENT_SOCKET_GROUP, "2770")}, _LOUD, 0, [True], id="ready"),
    ],
)
def test_socket_dir_setup_follows_the_probe(
    warnings: list[str], state: dict[str, Any], options: dict[str, bool], warned: int, issued: list[bool]
) -> None:
    guest = _SocketGuest(**state)

    ensure_agent_socket_dir(cast("Transport", guest), AGENT, **options)

    assert len(warnings) == warned
    assert all(AGENT in warning for warning in warnings)
    assert guest.sudo == issued


@pytest.mark.parametrize(
    ("ensure", "fast_path"),
    [
        pytest.param(
            lambda target: ensure_agent_socket_root(target, "agentworks", warn_if_missing=True),
            _ROOT_FAST_PATH,
            id="root",
        ),
        pytest.param(lambda target: ensure_agent_socket_dir(target, AGENT, warn_if_missing=True), [True], id="dir"),
    ],
)
def test_setup_leaves_what_the_next_probe_accepts(
    warnings: list[str], ensure: Callable[[Transport], None], fast_path: list[bool]
) -> None:
    guest = _SocketGuest()
    ensure(cast("Transport", guest))
    warnings.clear()
    guest.sudo.clear()

    ensure(cast("Transport", guest))

    assert warnings == []
    assert guest.sudo == fast_path
