"""The agent tmux socket layout: each agent's server is reachable by the agent and root, and by nobody else.

``ensure_agent_socket_root`` and ``ensure_agent_socket_dir`` run over a
guest that executes their requests on this machine in a scratch root (a
``chown`` is recorded rather than run, since only root may give a
directory away), so a test reads the modes they leave. ``retire_agent_socket_group``'s
script runs under ``/bin/sh`` against stub account tools, so a test reads
which of them it ran.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from agentworks.execution.files import Files
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Result
from agentworks.sessions import tmux
from tests.vm_access_support import Admin, Call, RecordingVMAccess, guest_result

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a POSIX shell and GNU coreutils")

AGENT = "agt--alice"


class _LocalGuest:
    """Runs each request on this machine, as this user; records every request and only records a ``chown``."""

    def __init__(self) -> None:
        self.requests: list[Command | Script] = []

    def answer(self, call: Call) -> Result | None:
        request = call.request
        assert isinstance(request, Command | Script)
        self.requests.append(request)
        if isinstance(request, Command) and request.argv[0] == "chown":
            return guest_result()
        argv = list(request.argv) if isinstance(request, Command) else ["/bin/sh", "-c", request.source]
        ran = subprocess.run(argv, capture_output=True, text=True, check=False)
        return guest_result(ran.returncode, ran.stdout, ran.stderr)

    def files(self) -> Files:
        access = RecordingVMAccess([], Admin())
        access.answer = self.answer
        return access.files()

    def chowns(self) -> list[tuple[str, ...]]:
        return [
            request.argv for request in self.requests if isinstance(request, Command) and request.argv[0] == "chown"
        ]


class _ProbedGuest:
    """Answers the probe with ``probed`` and every later request with success."""

    def __init__(self, probed: str) -> None:
        self.probed = probed
        self.requests: list[Command | Script] = []

    def answer(self, call: Call) -> Result | None:
        request = call.request
        assert isinstance(request, Command | Script)
        self.requests.append(request)
        return guest_result(stdout=self.probed) if len(self.requests) == 1 else guest_result()

    def files(self) -> Files:
        access = RecordingVMAccess([], Admin())
        access.answer = self.answer
        return access.files()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "agent-tmux-sockets"
    monkeypatch.setattr(tmux, "AGENT_SOCKET_ROOT", str(root))
    return root


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- The socket root ----------------------------------------------------------------------


def test_the_root_is_made_root_s_and_traversable_only(root: Path) -> None:
    guest = _LocalGuest()

    tmux.ensure_agent_socket_root(guest.files(), warn_if_missing=False)

    assert _mode(root) == 0o711
    assert guest.chowns() == [("chown", "root:root", "--", str(root))]
    assert all(request.elevated for request in guest.requests)


def test_an_earlier_release_s_group_shared_root_loses_its_group_bits_and_set_group_id(root: Path) -> None:
    root.mkdir()
    root.chmod(0o2771)
    guest = _LocalGuest()

    tmux.ensure_agent_socket_root(guest.files())

    assert _mode(root) == 0o711
    assert guest.chowns() == [("chown", "root:root", "--", str(root))]


def test_a_root_already_in_shape_is_probed_once_and_left_alone(root: Path) -> None:
    guest = _ProbedGuest("root root 711")

    tmux.ensure_agent_socket_root(guest.files())

    assert len(guest.requests) == 1


@pytest.mark.parametrize(
    ("probed", "warn_if_missing", "warned"),
    [
        ("MISSING", True, True),
        ("MISSING", False, False),
        ("PROBE_FAILED", False, True),
        ("root root 755", False, True),
        (f"root {tmux.RETIRED_AGENT_SOCKET_GROUP} 2771", True, False),
    ],
    ids=["missing", "missing-expected", "probe-failed", "misconfigured", "earlier-release"],
)
def test_a_root_repair_warns_unless_it_was_expected(
    warnings: list[str], probed: str, warn_if_missing: bool, warned: bool
) -> None:
    guest = _ProbedGuest(probed)

    tmux.ensure_agent_socket_root(guest.files(), warn_if_missing=warn_if_missing)

    assert bool(warnings) is warned
    assert len(guest.requests) > 1  # the repair ran


# -- An agent's directory -------------------------------------------------------------------


def test_an_agent_s_directory_is_the_agent_s_alone(root: Path) -> None:
    root.mkdir()
    guest = _LocalGuest()

    tmux.ensure_agent_socket_dir(guest.files(), AGENT, warn_if_missing=False)

    directory = root / AGENT
    assert _mode(directory) == 0o700
    # The agent's login group, whatever it is named: no group shares the directory.
    assert guest.chowns() == [("chown", f"{AGENT}:", "--", str(directory))]
    assert all(request.elevated for request in guest.requests)


def test_an_earlier_release_s_group_shared_directory_loses_its_group_bits_and_set_group_id(root: Path) -> None:
    directory = root / AGENT
    directory.mkdir(parents=True)
    directory.chmod(0o2770)
    guest = _LocalGuest()

    tmux.ensure_agent_socket_dir(guest.files(), AGENT)

    assert _mode(directory) == 0o700
    assert guest.chowns() == [("chown", f"{AGENT}:", "--", str(directory))]


@pytest.mark.parametrize(
    ("probed", "repaired"),
    [
        (f"{AGENT} {AGENT} 700", False),
        (f"{AGENT} staff 700", False),
        (f"{AGENT} {tmux.RETIRED_AGENT_SOCKET_GROUP} 2770", True),
        ("root root 700", True),
        (f"{AGENT} {AGENT} 750", True),
    ],
    ids=["own-group", "other-group", "earlier-release", "not-the-agent-s", "group-readable"],
)
def test_an_agent_s_directory_is_left_alone_only_when_the_agent_s_and_private(probed: str, repaired: bool) -> None:
    guest = _ProbedGuest(probed)

    tmux.ensure_agent_socket_dir(guest.files(), AGENT)

    assert (len(guest.requests) > 1) is repaired


@pytest.mark.parametrize(
    ("probed", "warn_if_missing", "warned"),
    [
        ("MISSING", True, True),
        ("MISSING", False, False),
        ("PROBE_FAILED", False, True),
        ("root root 755", False, True),
        (f"{AGENT} {tmux.RETIRED_AGENT_SOCKET_GROUP} 2770", True, False),
    ],
    ids=["missing", "missing-expected", "probe-failed", "misconfigured", "earlier-release"],
)
def test_a_directory_repair_warns_unless_it_was_expected(
    warnings: list[str], probed: str, warn_if_missing: bool, warned: bool
) -> None:
    tmux.ensure_agent_socket_dir(_ProbedGuest(probed).files(), AGENT, warn_if_missing=warn_if_missing)

    assert bool(warnings) is warned


# -- Retiring the shared group --------------------------------------------------------------


def _retirement_script() -> Script:
    access = RecordingVMAccess([], Admin())

    tmux.retire_agent_socket_group(access.files())

    [request] = access.requests()
    assert isinstance(request, Script) and request.elevated
    return request


def _run_against_stubs(tmp_path: Path, *, getent: int, groupdel: int = 0) -> tuple[int, list[str]]:
    """Run the retirement script under ``/bin/sh`` with stub account tools; its status and the tools it ran."""
    bin_dir, log = tmp_path / "bin", tmp_path / "ran.log"
    bin_dir.mkdir()
    for name, status in (("getent", getent), ("groupdel", groupdel)):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> {shlex.quote(str(log))}\nexit {status}\n')
        stub.chmod(0o755)
    ran = subprocess.run(
        ["/bin/sh", "-c", _retirement_script().source],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        check=False,
    )
    return ran.returncode, log.read_text().splitlines() if log.exists() else []


GROUP = tmux.RETIRED_AGENT_SOCKET_GROUP


def test_a_present_group_is_deleted(tmp_path: Path) -> None:
    status, ran = _run_against_stubs(tmp_path, getent=0)

    assert (status, ran) == (0, [f"getent group {GROUP}", f"groupdel {GROUP}"])


def test_an_absent_group_is_left_absent(tmp_path: Path) -> None:
    status, ran = _run_against_stubs(tmp_path, getent=2)

    assert (status, ran) == (0, [f"getent group {GROUP}"])


def test_a_refused_deletion_fails_the_script(tmp_path: Path) -> None:
    status, ran = _run_against_stubs(tmp_path, getent=0, groupdel=8)

    assert (status, ran) == (8, [f"getent group {GROUP}", f"groupdel {GROUP}"])
