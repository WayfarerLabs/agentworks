"""``create_vm_workspace``'s existence check on the workspace path.

The check decides whether create may reserve the path, so only its own
two answers count: present refuses, absent proceeds, and a check that did
not complete or answered something else raises before any reservation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import AlreadyExistsError
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.workspaces.backends.vm import create_vm_workspace
from agentworks.workspaces.templates import ResolvedTemplate

if TYPE_CHECKING:
    from tests.vm_access_support import AccessRecorder, Call

_PATH = "/srv/workspaces/proj"
_RESERVATION = Command(("mkdir", "--", _PATH), elevated=True)


class _Reserved(Exception):
    """Stops create at its reservation, which is all these tests need to see."""


def _create(recorder: AccessRecorder, probe: Result) -> None:
    def answer(call: Call) -> Result | BaseException | None:
        if isinstance(call.request, Script) and 'test -e "$1"' in call.request.source:
            return probe
        if call.request == _RESERVATION:
            return _Reserved()
        return None

    recorder.answer = answer
    vm = SimpleNamespace(name="box", tailscale_host="100.64.0.1", admin_username="admin")
    config = SimpleNamespace(paths=SimpleNamespace(vm_workspaces="/srv/workspaces"))
    create_vm_workspace(vm, config, "proj", ResolvedTemplate(name="proj"))  # type: ignore[arg-type]


def test_absent_path_is_reserved(vm_access_recorder: AccessRecorder) -> None:
    with pytest.raises(_Reserved):
        _create(vm_access_recorder, Result(Completed(1)))
    assert _RESERVATION in vm_access_recorder.requests("run")


@pytest.mark.parametrize(
    ("probe", "error"),
    [
        (Result(Completed(0)), AlreadyExistsError),
        (Result(Unknown(UnknownReason.CONNECTION)), ExecutionIncomplete),
        (Result(Completed(2)), CommandFailed),
    ],
)
def test_any_other_answer_raises_before_reserving(
    vm_access_recorder: AccessRecorder, probe: Result, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _create(vm_access_recorder, probe)
    (only,) = vm_access_recorder.requests("run")
    assert isinstance(only, Script)
