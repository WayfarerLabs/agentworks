"""Artifact publication and cleanup use one framed actual-user home observation."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.artifacts.session import cleanup_session_artifacts
from agentworks.errors import StateError
from agentworks.harness_setup.state import read_native_setup
from agentworks.ssh import SSHResult
from tests.artifacts.test_session_lifecycle import Lifecycle
from tests.artifacts.test_session_lifecycle import lifecycle as lifecycle

if TYPE_CHECKING:
    from typing import Any

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="executes actual Linux guest artifact file helpers")


def _observe_home(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
    *,
    banner: str = "",
    stdout: str | None = None,
    returncode: int = 0,
) -> None:
    original = lifecycle.target.run

    def run(command: str, **kwargs: Any) -> SSHResult:
        if "AGW_ARTIFACT_HOME=" in command or (command.startswith("printf ") and '"$HOME"' in command):
            result = original(command, **kwargs)
            return SSHResult(returncode, banner + result.stdout if stdout is None else stdout, result.stderr)
        return original(command, **kwargs)

    monkeypatch.setattr(lifecycle.target, "run", run)


@pytest.mark.parametrize("operation", ["prepare", "cleanup"])
@pytest.mark.parametrize("banner", ["Welcome\n", "Welcome"])
def test_session_artifact_home_tolerates_startup_stdout(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, operation: str, banner: str
) -> None:
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", lifecycle.target.home / "logs")
    if operation == "cleanup":
        lifecycle.restart()
        files = lifecycle.files()
    _observe_home(lifecycle, monkeypatch, banner=banner)
    if operation == "prepare":
        lifecycle.restart()
        assert lifecycle.files()
        assert all(path.is_relative_to(lifecycle.target.home) and path.is_file() for path in lifecycle.files())
    else:
        session = lifecycle.db.get_session("s1")
        assert session is not None
        cleanup_session_artifacts(lifecycle.db, session, lifecycle.target)
        assert all(not path.exists() for path in files)
    assert not list((lifecycle.target.root / "tmp").iterdir())


@pytest.mark.parametrize("operation", ["prepare", "cleanup"])
@pytest.mark.parametrize(
    "response",
    [
        "missing",
        "duplicate",
        "unframed",
        "relative",
        "parent",
        "dot",
        "repeated-slash",
        "control",
        "failed",
        "invalid-json",
        "wrong-type",
    ],
)
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
    payload = {
        "missing": "Welcome\n",
        "duplicate": f"AGW_ARTIFACT_HOME={json.dumps(home)}\nAGW_ARTIFACT_HOME={json.dumps(home)}\n",
        "unframed": home,
        "relative": 'AGW_ARTIFACT_HOME="home/user"\n',
        "parent": "AGW_ARTIFACT_HOME=" + json.dumps(home + "/../outside") + "\n",
        "dot": "AGW_ARTIFACT_HOME=" + json.dumps(home + "/./user") + "\n",
        "repeated-slash": "AGW_ARTIFACT_HOME=" + json.dumps(home + "//user") + "\n",
        "control": "AGW_ARTIFACT_HOME=" + json.dumps(home + "\x1b") + "\n",
        "failed": f"AGW_ARTIFACT_HOME={json.dumps(home)}\n",
        "invalid-json": "AGW_ARTIFACT_HOME=not-json\n",
        "wrong-type": "AGW_ARTIFACT_HOME=null\n",
    }[response]
    _observe_home(lifecycle, monkeypatch, stdout=payload, returncode=1 if response == "failed" else 0)
    lifecycle.events.clear()
    with pytest.raises(StateError):
        if operation == "prepare":
            lifecycle.restart()
        else:
            cleanup_session_artifacts(lifecycle.db, previous, lifecycle.target)
    assert lifecycle.db.get_session("s1") == previous
    assert read_native_setup(lifecycle.db, "session", "s1") == state
    assert {path: path.read_bytes() for path in files} == files
    assert "kill" not in lifecycle.events and "tmux_create" not in lifecycle.events
    assert not list((lifecycle.target.root / "tmp").iterdir())


@pytest.mark.parametrize(
    "home",
    ["relative", "/", "/home/../outside", "/home/./user", "/home//user", "/home/user\x1b", "/home/user\nsecond-line"],
)
def test_actual_home_observer_rejects_non_normalized_guest_paths(tmp_path, home: str) -> None:
    from agentworks.artifacts.session import _session_artifact_home
    from tests.native_setup_fixtures import LocalFixtureTransport

    target = LocalFixtureTransport(tmp_path / "home-observer")
    target.environment["HOME"] = home
    with pytest.raises(StateError):
        _session_artifact_home(target)
    assert len(target.commands) == 1
    assert not list((target.root / "tmp").iterdir())
