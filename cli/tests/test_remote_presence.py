"""``probe_presence``'s contract beyond what its call sites exercise."""

from __future__ import annotations

import shutil
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ExternalError
from agentworks.remote_presence import SUDO_SAFE_ABSENT_EXIT, TEST_FALSE_EXIT, probe_presence, sudo_safe_test
from agentworks.ssh import SSHResult


def test_a_sudo_probe_cannot_take_exit_1_as_absent() -> None:
    """``sudo -n`` exits 1 when it refuses, so a sudo probe answering absent
    with 1 would be a guess; the helper refuses the pairing before running."""
    target = MagicMock()
    with pytest.raises(ValueError):
        probe_presence(target, "test -d /x", absent_exit=TEST_FALSE_EXIT, sudo=True)
    target.run.assert_not_called()


class _LocalShell:
    """Runs each probe command in a local bash, as the guest would (without sudo)."""

    def run(self, command: str, **kwargs: object) -> SSHResult:
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
        return SSHResult(done.returncode, done.stdout, done.stderr)

    def describe(self) -> str:
        return "local:bash"


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="requires bash")
@pytest.mark.parametrize(
    ("condition", "outcome"),
    [
        ("test -d /", "present"),
        ("test -e /nonexistent-agentworks-probe", "absent"),
        ("test 1 -eq x", ExternalError),  # test's own error (2) is not an answer
    ],
)
def test_sudo_safe_test_maps_only_false_to_absent(condition: str, outcome: object) -> None:
    def probe() -> SSHResult | None:
        return probe_presence(
            _LocalShell(),  # type: ignore[arg-type]
            sudo_safe_test(condition),
            absent_exit=SUDO_SAFE_ABSENT_EXIT,
            sudo=True,
        )

    if outcome is ExternalError:
        with pytest.raises(ExternalError):
            probe()
    else:
        assert (probe() is not None) == (outcome == "present")
