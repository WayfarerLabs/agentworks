"""Proxmox's root channel: one QGA exec of /bin/bash -s, polled to the deadline, with the script kept private."""

from __future__ import annotations

from typing import Any

import pytest

from agentworks.errors import ConnectivityError, TokenRejectedError
from agentworks.execution.root_script import RawRun
from agentworks.plugins.proxmox import guest_agent
from agentworks.plugins.proxmox.api import ProxmoxAPIError

SCRIPT = b"{\nAGW_SECRET=tskey-auth-private\n(\ntrue\n)\n}\n"


class FakeAPI:
    """Records each QGA call; ``statuses`` are the exec-status answers in order."""

    def __init__(self, statuses: list[dict[str, Any] | BaseException], dispatch: BaseException | None = None) -> None:
        self.statuses = statuses
        self.dispatch = dispatch
        self.execs: list[tuple[list[str], str | None]] = []
        self.polls = 0

    def guest_agent_exec(
        self, node: str, vmid: int, *, command: list[str], input_data: str | None = None, timeout: float | None = None
    ) -> int:
        self.execs.append((command, input_data))
        if self.dispatch is not None:
            raise self.dispatch
        return 41

    def guest_agent_exec_status(
        self, node: str, vmid: int, *, pid: int, timeout: float | None = None
    ) -> dict[str, Any]:
        self.polls += 1
        answer = self.statuses.pop(0) if self.statuses else {"exited": False}
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.plugins.proxmox.guest_agent.time.sleep", lambda seconds: None)


def run(api: FakeAPI, deadline: float = 60) -> RawRun:
    return guest_agent.run_as_root(api, "pve1", 101, SCRIPT, deadline)  # type: ignore[arg-type]


def test_the_script_is_the_input_of_one_bash_s_and_the_streams_come_back() -> None:
    api = FakeAPI([{"exited": False}, {"exited": True, "exitcode": 0, "out-data": "out", "err-data": "err"}])

    raw = run(api)

    assert api.execs == [(["/bin/bash", "-s"], SCRIPT.decode())]
    assert api.polls == 2
    assert (raw.stdout, raw.stderr) == (b"out", b"err")


def test_the_deadline_returns_empty_streams_without_dispatching_again(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr("agentworks.plugins.proxmox.guest_agent.time.monotonic", lambda: next(clock))
    api = FakeAPI([])

    raw = run(api, deadline=5)

    assert (raw.stdout, raw.stderr) == (b"", b"")
    assert len(api.execs) == 1


@pytest.mark.parametrize(
    "api",
    [
        FakeAPI([], dispatch=ProxmoxAPIError(f"POST failed for {SCRIPT.decode()}")),
        FakeAPI([ProxmoxAPIError(f"status failed for {SCRIPT.decode()}")]),
        FakeAPI([RuntimeError(f"socket error near {SCRIPT.decode()}")]),
    ],
    ids=["dispatch", "status", "unexpected"],
)
def test_a_failed_call_raises_without_the_script_anywhere(api: FakeAPI) -> None:
    with pytest.raises(ConnectivityError) as caught:
        run(api)

    error = caught.value
    assert "tskey-auth-private" not in str(error)
    assert "tskey-auth-private" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert len(api.execs) == 1


def test_a_script_over_qgas_input_limit_is_refused_before_dispatch() -> None:
    api = FakeAPI([])

    with pytest.raises(ConnectivityError):
        guest_agent.run_as_root(api, "pve1", 101, b"x" * (guest_agent.INPUT_DATA_LIMIT + 1), 60)  # type: ignore[arg-type]

    assert api.execs == []


def _rejected(code: int) -> ProxmoxAPIError:
    error = ProxmoxAPIError(f"Proxmox API POST failed ({code}): {SCRIPT.decode()}")
    error.code = code
    return error


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("call", ["dispatch", "status"])
def test_a_rejected_token_is_its_own_error_without_the_script(code: int, call: str) -> None:
    api = FakeAPI([], dispatch=_rejected(code)) if call == "dispatch" else FakeAPI([_rejected(code)])

    with pytest.raises(TokenRejectedError) as caught:
        run(api)

    assert "tskey-auth-private" not in str(caught.value)
    assert caught.value.__context__ is None
