"""User install commands run as the agent, on its own access; its profile is covered by test_agent_home_permissions."""

from __future__ import annotations

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
