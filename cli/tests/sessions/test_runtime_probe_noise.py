"""Login stdout cannot impersonate an owned native runtime observation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.sessions import tmux


def result(stdout: str):
    return SimpleNamespace(returncode=0, ok=True, stdout=stdout, stderr="")


@pytest.mark.parametrize("noise", ["", "welcome\n", "partial banner", "\x1b[31mbanner\x1b[0m\n"])
def test_framed_native_observations_accept_preceding_login_output(noise):
    class Target:
        def run(self, command, **kwargs):
            if "#{pid}" in command:
                return result(noise + "\nAGW_RUNTIME_PID=42\n")
            if "boot_id" in command:
                return result(noise + "\nAGW_RUNTIME_BOOT=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\n")
            if "#{pane_dead}" in command:
                return result(noise + "\nAGW_RUNTIME_PANE=1 23\n")
            return result(noise + "\nAGW_RUNTIME_SESSION=")

    target = Target()
    assert tmux.probe_tmux_server_pid(target=target).value == 42
    assert tmux.read_boot_id(target) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert tmux.probe_tmux_session("s", run_command=target.run) is tmux.ProbeStatus.PRESENT
    assert tmux._probe_pane_liveness("s", run_command=target.run, socket_path="/tmp/owned") == ("dead", "23")


@pytest.mark.parametrize(
    "stdout",
    ["42\n", "AGW_RUNTIME_PID=42\nAGW_RUNTIME_PID=42\n", "AGW_RUNTIME_PID=42\nextra", "AGW_RUNTIME_PID=+42\n"],
)
def test_unframed_duplicate_and_malformed_pid_results_remain_unknown(stdout):
    target = SimpleNamespace(run=lambda *a, **k: result(stdout))
    assert tmux.probe_tmux_server_pid(target=target).status is tmux.ProbeStatus.UNKNOWN


@pytest.mark.parametrize("payload", ["unexpected", "\nAGW_RUNTIME_SESSION=", "\x00"])
def test_successful_has_session_requires_one_empty_native_payload(payload):
    assert (
        tmux.probe_tmux_session("s", run_command=lambda *a, **k: result("AGW_RUNTIME_SESSION=" + payload))
        is tmux.ProbeStatus.UNKNOWN
    )
