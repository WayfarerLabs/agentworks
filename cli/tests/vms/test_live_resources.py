"""``vm describe``'s live resources: one bounded request as the admin, and ``None`` for anything incomplete."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.execution.access import Admin
from agentworks.execution.errors import ClientUnsupported
from agentworks.execution.requests import Script, Shell
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.vms.manager._helpers import _query_live_resources

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import AccessRecorder

ANSWER = b"4\n0.15\n8589934592 2147483648\n1073741824 0\n50G 10G 20%\n"


@pytest.fixture(autouse=True)
def _boundary_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.vms.manager.boundary.require_vm_ssh_boundary", lambda *a, **k: None)


def query() -> dict[str, str] | None:
    vm: Any = SimpleNamespace(name="box")
    return _query_live_resources(SimpleNamespace(), vm, SimpleNamespace())  # type: ignore[arg-type]


def test_live_resources_are_one_bounded_script_as_the_admin(vm_access_recorder: AccessRecorder) -> None:
    vm_access_recorder.access().default_outcome = Result(Completed(0), ANSWER)

    resources = query()

    assert resources is not None
    assert (resources["cpus"], resources["mem_pct"], resources["disk_pct"]) == ("4", "25%", "20%")
    (call,) = vm_access_recorder.calls
    assert call.identity == Admin()
    assert isinstance(call.request, Script)
    assert (call.request.shell, call.request.elevated) == (Shell.SH, False)
    assert call.request.deadline is not None


@pytest.mark.parametrize(
    "outcome",
    [
        Result(Unknown(UnknownReason.DEADLINE)),
        Result(Unknown(UnknownReason.CONNECTION)),
        Result(Completed(1), ANSWER),
        Result(Completed(0), b"4\n0.15\n"),
    ],
    ids=["deadline", "connection", "failed", "short"],
)
def test_anything_but_a_complete_answer_leaves_live_resources_out_after_one_dispatch(
    vm_access_recorder: AccessRecorder, outcome: Result
) -> None:
    vm_access_recorder.access().default_outcome = outcome

    assert query() is None
    assert len(vm_access_recorder.calls) == 1


def test_an_unsupported_client_leaves_live_resources_out_and_says_so(
    vm_access_recorder: AccessRecorder, captured_output: CapturedOutput
) -> None:
    vm_access_recorder.access().default_outcome = ClientUnsupported("no supported ssh client")

    assert query() is None
    assert len(captured_output.warnings) == 1


def test_guest_text_reaches_the_resources_without_terminal_controls(vm_access_recorder: AccessRecorder) -> None:
    answer = b"4\x1b]0;owned\x07\n0.15\x1b[2J\n8589934592 2147483648\n1073741824 0\n50G 10G\x1b[31m 20%\n"
    vm_access_recorder.access().default_outcome = Result(Completed(0), answer)

    resources = query()

    assert resources is not None
    assert not any("\x1b" in value or "\x07" in value for value in resources.values())
