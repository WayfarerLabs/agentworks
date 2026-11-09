"""Artifact publication and cleanup use the actual session user's home, as the guest reports it."""

from __future__ import annotations

import sys

import pytest

from agentworks.artifacts.session import HOME_PROBE, cleanup_session_artifacts
from agentworks.errors import StateError
from agentworks.execution.results import Completed, Result
from agentworks.harness_setup.state import read_native_setup
from tests.artifacts.test_session_lifecycle import Lifecycle
from tests.artifacts.test_session_lifecycle import lifecycle as lifecycle
from tests.vm_access_support import Admin, Call, RecordingVMAccess

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes actual Linux guest artifact file helpers")


def _observe_home(lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, *, stdout: str, status: int = 0) -> None:
    """Answer the session home probe with ``stdout`` and ``status``; every other request as before."""
    answer = lifecycle.target.answer

    def observe(call: Call) -> Result | None:
        if call.request == HOME_PROBE:
            return Result(Completed(status), stdout.encode())
        return answer(call)

    monkeypatch.setattr(lifecycle.target, "answer", observe)


@pytest.mark.parametrize("operation", ["prepare", "cleanup"])
@pytest.mark.parametrize("response", ["empty", "relative", "parent", "dot", "repeated-slash", "control", "failed"])
def test_invalid_home_observation_preserves_existing_session_artifacts(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, operation: str, response: str
) -> None:
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", lifecycle.target.home / "logs")
    lifecycle.restart()
    previous = lifecycle.db.get_session("s1")
    assert previous is not None
    state = read_native_setup(lifecycle.db, "session", "s1")
    files = {path: path.read_bytes() for path in lifecycle.files()}
    home = str(lifecycle.target.home)
    stdout = {
        "empty": "",
        "relative": "home/user",
        "parent": home + "/../outside",
        "dot": home + "/./user",
        "repeated-slash": home + "//user",
        "control": home + "\x1b",
        "failed": home,
    }[response]
    _observe_home(lifecycle, monkeypatch, stdout=stdout, status=1 if response == "failed" else 0)
    lifecycle.events.clear()
    with pytest.raises(StateError):
        if operation == "prepare":
            lifecycle.restart()
        else:
            cleanup_session_artifacts(lifecycle.db, previous, lifecycle.target.access())
    assert lifecycle.db.get_session("s1") == previous
    assert read_native_setup(lifecycle.db, "session", "s1") == state
    assert {path: path.read_bytes() for path in files} == files
    assert "kill" not in lifecycle.events and "tmux_create" not in lifecycle.events


@pytest.mark.parametrize(
    "home",
    ["relative", "/", "/home/../outside", "/home/./user", "/home//user", "/home/user\x1b", "/home/user\nsecond-line"],
)
def test_actual_home_observer_rejects_non_normalized_guest_paths(home: str) -> None:
    from agentworks.artifacts.session import _session_artifact_home

    access = RecordingVMAccess([], Admin())
    access.respond(HOME_PROBE, Result(Completed(0), home.encode()))
    with pytest.raises(StateError):
        _session_artifact_home(access)
    assert access.requests() == [HOME_PROBE]
