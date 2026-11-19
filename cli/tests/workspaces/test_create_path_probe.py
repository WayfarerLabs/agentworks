"""``create_vm_workspace``'s existence check on the workspace path.

The check decides whether create may reserve the path, so only its own
two answers count: present refuses, absent proceeds, and a check that did
not run (ssh's 255) or answered something else raises before any
reservation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.errors import AlreadyExistsError, ExternalError
from agentworks.ssh import SSHError
from agentworks.workspaces.backends.vm import create_vm_workspace
from agentworks.workspaces.templates import ResolvedTemplate

_PATH = "/srv/workspaces/proj"


class _FakeTarget:
    def __init__(self, probe_exit: int) -> None:
        self.commands: list[str] = []
        self._probe_exit = probe_exit

    def run(self, command: str, **kwargs: Any) -> SimpleNamespace:
        self.commands.append(command)
        returncode = self._probe_exit if command.startswith("test -d") else 0
        return SimpleNamespace(ok=returncode == 0, returncode=returncode, stdout="", stderr="")

    def write_file(self, path: str, content: str, **kwargs: Any) -> None:
        pass


def _create(target: _FakeTarget, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.workspaces.backends.vm.transport", lambda vm, config, logger=None: target)
    vm = SimpleNamespace(name="box", tailscale_host="100.64.0.1", admin_username="admin")
    config = SimpleNamespace(paths=SimpleNamespace(vm_workspaces="/srv/workspaces"))
    create_vm_workspace(vm, config, "proj", ResolvedTemplate(name="proj"))  # type: ignore[arg-type]


def test_absent_path_is_reserved(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _FakeTarget(probe_exit=1)
    _create(target, monkeypatch)
    assert f"mkdir -- {_PATH}" in target.commands


@pytest.mark.parametrize(("probe_exit", "error"), [(0, AlreadyExistsError), (255, SSHError), (2, ExternalError)])
def test_any_other_answer_raises_before_reserving(
    monkeypatch: pytest.MonkeyPatch, probe_exit: int, error: type[Exception]
) -> None:
    target = _FakeTarget(probe_exit)
    with pytest.raises(error):
        _create(target, monkeypatch)
    assert len(target.commands) == 1 and target.commands[0].startswith("test -d")
