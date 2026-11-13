"""An agent existence probe that does not complete raises, and never reads as an absent account.

Over a recording access: each probe's request gets an outcome that is not a
completion (a lost connection, a deadline), and the create or reinit must
raise the typed error before any ``useradd`` is sent (issue #899).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents import initializer as agent_initializer
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from tests.vm_access_support import AccessRecorder, install_access_recorder

if TYPE_CHECKING:
    from tests.vm_access_support import Call

LINUX_USER = "agt-dev"
UNANSWERED = (Unknown(UnknownReason.CONNECTION), Unknown(UnknownReason.DEADLINE))


def _vm() -> Any:
    return SimpleNamespace(name="box", tailscale_host="100.64.0.9", admin_username="agentworks")


def _is_account_probe(request: object) -> bool:
    return isinstance(request, Command) and request.argv == ("getent", "passwd", LINUX_USER)


def _is_home_probe(request: object) -> bool:
    return isinstance(request, Script) and f"test -e /home/{LINUX_USER}" in request.source


def _useradds(recorder: AccessRecorder) -> list[object]:
    return [r for r in recorder.requests("run") if isinstance(r, Command) and r.argv[0] == "useradd"]


@pytest.mark.parametrize("completion", UNANSWERED, ids=["connection", "deadline"])
@pytest.mark.parametrize("probe", ["account", "home"])
def test_create_raises_when_an_ownership_probe_goes_unanswered(
    monkeypatch: pytest.MonkeyPatch, completion: object, probe: str
) -> None:
    recorder = install_access_recorder(monkeypatch)
    unanswered = _is_account_probe if probe == "account" else _is_home_probe

    def answer(call: Call) -> Result | None:
        if unanswered(call.request):
            return Result(completion)  # type: ignore[arg-type]
        if _is_account_probe(call.request):
            return Result(Completed(2))  # getent: no such account
        return Result(Completed(1))  # nothing at the home

    recorder.answer = answer

    with pytest.raises(ExecutionIncomplete):
        agent_initializer.create_new_agent_user(
            _vm(), SimpleNamespace(), LINUX_USER, shell="bash", logger=SimpleNamespace(record=lambda *a: None)
        )

    assert _useradds(recorder) == []


@pytest.mark.parametrize("completion", UNANSWERED, ids=["connection", "deadline"])
def test_reinit_raises_when_the_account_probe_goes_unanswered(
    monkeypatch: pytest.MonkeyPatch, completion: object
) -> None:
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = lambda call: Result(completion) if _is_account_probe(call.request) else None  # type: ignore[arg-type]

    with pytest.raises(ExecutionIncomplete):
        agent_initializer.create_agent_on_vm(
            _vm(),
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(shell="bash"),  # type: ignore[arg-type]
            LINUX_USER,
            agent_name="dev",
            credential_requests=(),
            logger=SimpleNamespace(record=lambda *a: None),  # type: ignore[arg-type]
            admin_git_force_safe_directory=False,
        )

    assert _useradds(recorder) == []
