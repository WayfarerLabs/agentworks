"""User install commands run as the agent, on its own access, and the profile fragment they leave."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.agents.initializer import _run_agent_install_commands
from agentworks.execution.requests import Script
from agentworks.execution.results import Completed, Result
from agentworks.install_commands import UserInstallCommandEntry
from agentworks.vms.initializer.packages import _build_test_command, install_run
from tests.phase_b_support import StepGuest, requires_file_program
from tests.vm_access_support import Agent, RecordingVMAccess

if TYPE_CHECKING:
    from tests.vm_access_support import Call


class AgentGuest(StepGuest):
    """The agent's access, its file operations run on this machine, its completion tests answering ``status``."""

    def __init__(self, status: int) -> None:
        super().__init__()
        self.access = RecordingVMAccess([], Agent("dev", "agt-dev"))
        self.access.answer = self._answer
        self.files = self.access.files()
        self.status = status

    def _answer(self, call: Call) -> Result | BaseException | None:
        if isinstance(call.request, Script):
            return Result(Completed(self.status))
        return super()._answer(call)

    def checks(self) -> list[Script]:
        return [request for request in self.access.requests() if isinstance(request, Script)]


def _run(
    guest: AgentGuest, home: Path, *entries: UserInstallCommandEntry, shell: str = "bash", mise: bool = False
) -> None:
    registry = MagicMock()
    registry.iter_kind_items.return_value = [(entry.name, entry) for entry in entries]
    template = SimpleNamespace(
        shell=shell,
        user_install_commands=[entry.name for entry in entries],
        mise_packages=["jq@1"] if mise else [],
        mise_lockfile=None,
    )
    _run_agent_install_commands(
        agent_files=guest.files,
        logger=guest.log,
        registry=registry,
        agent_tmpl=template,  # type: ignore[arg-type]
        home=str(home),
        identity_env={"AGENTWORKS_AGENT": "dev"},
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@requires_file_program
def test_an_install_is_skipped_only_when_every_declared_test_passes(home: Path) -> None:
    entry = UserInstallCommandEntry(
        name="my-tool", command="run-agent-install", test_exec="my-tool", test_file="~/.my-tool/installed"
    )
    passing, failing = AgentGuest(status=0), AgentGuest(status=1)

    _run(passing, home, entry)
    _run(failing, home, entry)

    for guest in (passing, failing):
        (check,) = guest.checks()
        assert check.source == _build_test_command(entry, "bash", str(home))
    assert install_run("bash", "run-agent-install") not in passing.access.requests()
    assert install_run("bash", "run-agent-install") in failing.access.requests()
    assert not any(request.elevated or request.env for request in failing.access.requests())


@requires_file_program
def test_an_install_with_no_declared_test_always_runs(home: Path) -> None:
    guest = AgentGuest(status=0)

    _run(guest, home, UserInstallCommandEntry(name="my-tool", command="run-agent-install"))

    assert guest.checks() == []
    assert install_run("bash", "run-agent-install") in guest.access.requests()


@requires_file_program
@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_the_profile_puts_install_paths_ahead_of_mise_s_shims_as_the_admin_s_does(home: Path, shell: str) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is unavailable")
    entries = [
        UserInstallCommandEntry(name="first", command="install-first", path=["~/.local/bin", "/opt/tools"]),
        UserInstallCommandEntry(name="second", command="install-second", path=["$HOME/.local/bin"]),
    ]

    for _ in range(2):
        _run(AgentGuest(status=1), home, *entries, shell=shell, mise=True)

    result = subprocess.run(
        [shell_path, "-c", '. "$HOME/.agentworks-profile.sh"; printf "%s\\n" "$PATH" "$AGENTWORKS_AGENT"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    shims = f"{home}/.local/share/mise/shims"
    assert result.stdout.splitlines() == [f"{home}/.local/bin:/opt/tools:{shims}:/usr/bin:/bin", "dev"]
    profile_line = f". {home}/.agentworks-profile.sh"
    for name in [".profile", ".bashrc"] + ([".zprofile"] if shell == "zsh" else []):
        assert (home / name).read_text().splitlines() == [profile_line]

    # Removing the install commands on reinit removes their managed paths; mise's shims stay, as the admin's do.
    _run(AgentGuest(status=1), home, shell=shell)
    result = subprocess.run(
        [shell_path, "-c", '. "$HOME/.agentworks-profile.sh"; printf "%s" "$PATH"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == f"{shims}:/usr/bin:/bin"
