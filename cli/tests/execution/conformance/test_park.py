"""What ``park`` does around a pane's handover: entering, leaving, and the pane's command line.

The pane itself, with its ``TERM`` and its shell, is vector T2 in ``vectors.py``,
on ``script``'s terminal so every lane can run it; the last test here runs it in
a real tmux pane.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import attach as attach_module
from agentworks.execution import ssh_process
from agentworks.execution.attach import Handover
from agentworks.execution.errors import RequestRefused
from agentworks.execution.requests import LoginShell, TerminalCommand

if TYPE_CHECKING:
    from pathlib import Path

    from tests.execution.conformance.lanes import LocalLane


def test_a_block_that_raises_lets_the_request_go_at_once(local_lane: LocalLane) -> None:
    token = local_lane.handover_token()
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="split failed"), local_lane.access.park(LoginShell()):
        raise RuntimeError("split failed")

    # Closing the parking stdin ends the launcher's wait, well inside its own 30 seconds.
    assert time.monotonic() - started < attach_module.HANDOVER_COLLECT_SECONDS / 2
    assert local_lane.wait_gone(f"/tmp/agw-handover-{token}")


def test_a_handover_no_pane_collects_reports_it(local_lane: LocalLane) -> None:
    local_lane.shorten_handover_wait(1)
    token = local_lane.handover_token()

    with local_lane.access.park(LoginShell()) as handover:
        pass

    assert handover.collected is False
    assert local_lane.wait_gone(f"/tmp/agw-handover-{token}")


def test_a_directory_it_cannot_enter_is_refused_before_anything_is_parked(local_lane: LocalLane) -> None:
    token = local_lane.handover_token()

    with pytest.raises(RequestRefused) as raised, local_lane.access.park(LoginShell(cwd="/nonexistent/agw-5e")):
        pytest.fail("the block ran")

    assert raised.value.code == "cwd"
    assert not local_lane.exists(f"/tmp/agw-handover-{token}")


def test_only_a_login_shell_parks(local_lane: LocalLane, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_process, "spawn", lambda *a, **k: pytest.fail("spawned"))

    with pytest.raises(ValidationError), local_lane.access.park(TerminalCommand(("tmux",))):  # type: ignore[arg-type]
        pytest.fail("the block ran")


def test_the_pane_runs_the_handover_stage_and_collects_as_the_account_that_parked() -> None:
    handover = Handover("ab" * 8, "agt-x")
    stage0 = ssh_process.stage0_command("/tmp/agw-handover-abababababababab/s")

    assert handover.command() == stage0
    assert handover.command(run_as="agt-x") == f"sudo -n -u agt-x {stage0}"
    with pytest.raises(ValueError):
        handover.command(run_as="agt-y")


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("tmux") is None, reason="runs a real tmux server on Linux")
def test_a_tmux_pane_collects_its_request_and_has_the_panes_term(local_lane: LocalLane, tmp_path: Path) -> None:
    secret = "sekrit-pane-value-5e"
    local_lane.quiet_interactive_startup()
    config = tmp_path / "tmux.conf"
    config.write_text('set -g default-terminal "screen-256color"\n')
    shown = tmp_path / "shown"
    tmux = ["tmux", "-f", str(config), "-S", str(tmp_path / "tmux.sock")]
    # The server's environment: tmux runs a pane's command through the default shell it names.
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SHELL": local_lane.shell}
    try:
        with local_lane.access.park(LoginShell(cwd=local_lane.workdir, env={"TOKEN": secret})) as handover:
            pane = local_lane.pane_command(handover.command())
            subprocess.run([*tmux, "new-session", "-d", pane], check=True, env=environment)

        assert handover.collected
        probe = f'printf "%s:%s:%s\\n" "$TERM" "$TOKEN" "$PWD" > {shown}'
        subprocess.run([*tmux, "send-keys", probe, "Enter"], check=True, env=environment)
        deadline = time.monotonic() + 20
        while not (shown.exists() and shown.read_text().endswith("\n")) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert shown.read_text() == f"screen-256color:{secret}:{local_lane.workdir}\n"
    finally:
        subprocess.run([*tmux, "kill-server"], check=False, env=environment, capture_output=True)
