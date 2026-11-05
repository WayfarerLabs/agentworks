"""Proxmox's root channel: one QGA exec of /bin/bash -s, polled to the deadline, with the script kept private."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from agentworks.errors import ConnectivityError, TokenRejectedError
from agentworks.execution.root_script import ChannelUnreachable, RawRun
from agentworks.plugins.proxmox import guest_agent
from agentworks.plugins.proxmox.api import ProxmoxAPIError

SCRIPT = b"{\nAGW_SECRET=tskey-auth-private\n(\ntrue\n)\n}\n"


class FakeAPI:
    """Records each QGA call; ``statuses`` are the exec-status answers in order."""

    def __init__(
        self,
        statuses: list[dict[str, Any] | BaseException],
        dispatch: BaseException | None = None,
        pings: list[BaseException | None] | None = None,
    ) -> None:
        self.statuses = statuses
        self.dispatch = dispatch
        self.pings = pings if pings is not None else []
        self.ping_count = 0
        self.execs: list[tuple[list[str], str | None]] = []
        self.polls = 0

    def guest_agent_ping(self, node: str, vmid: int, *, timeout: float | None = None) -> None:
        """Answers once ``pings`` (each failure in turn, ``None`` for an answer) runs out."""
        self.ping_count += 1
        failure = self.pings.pop(0) if self.pings else None
        if failure is not None:
            raise failure

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
    clock = iter([0.0, 0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr("agentworks.plugins.proxmox.guest_agent.time.monotonic", lambda: next(clock))
    api = FakeAPI([])

    raw = run(api, deadline=5)

    assert (raw.stdout, raw.stderr) == (b"", b"")
    assert len(api.execs) == 1


@pytest.mark.parametrize(
    "api",
    [
        FakeAPI([], dispatch=ProxmoxAPIError(f"POST failed for {SCRIPT.decode()}")),
        FakeAPI([RuntimeError(f"socket error near {SCRIPT.decode()}")]),
    ],
    ids=["dispatch", "unexpected"],
)
def test_a_failed_call_raises_without_the_script_anywhere(api: FakeAPI) -> None:
    with pytest.raises(ConnectivityError) as caught:
        run(api)

    error = caught.value
    assert not isinstance(error, ChannelUnreachable)  # the agent answered, so a read may be tried again
    assert "tskey-auth-private" not in str(error)
    assert "tskey-auth-private" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert len(api.execs) == 1


def test_a_failed_status_poll_says_why_without_chaining() -> None:
    """The status request carries only the PID, so the API's own reason is safe to show, and it is the useful part."""
    reason = (
        "Proxmox API GET /nodes/pve1/qemu/101/agent/exec-status?pid=41 failed (500): Agent error: PID 41 does not exist"
    )
    api = FakeAPI([ProxmoxAPIError(reason)])

    with pytest.raises(ConnectivityError) as caught:
        run(api)

    assert reason in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_failed_status_poll_shows_the_provider_s_text_as_a_terminal_can_take_it() -> None:
    """The API's text is the provider's, not ours: controls are removed and only its last lines are kept."""
    body = "\n".join(f"line {number}" for number in range(40)) + "\n\x1b]0;owned\x07\x1b[31mAgent error\x1b[0m"
    api = FakeAPI([ProxmoxAPIError(body)])

    with pytest.raises(ConnectivityError) as caught:
        run(api)

    message = str(caught.value)
    assert "\x1b" not in message
    assert "\x07" not in message
    assert "line 39" in message
    assert "line 0\n" not in message


def test_an_agent_that_answers_on_the_third_ping_gets_the_script_once() -> None:
    """A VM that has just started has no agent yet: pinging carries no script, so it may be asked again."""
    not_yet = ProxmoxAPIError("QEMU guest agent is not running")
    api = FakeAPI([{"exited": True, "exitcode": 0}], pings=[not_yet, not_yet, None])

    run(api)

    assert api.ping_count == 3
    assert len(api.execs) == 1


def test_an_agent_that_never_answers_raises_at_the_cap_with_nothing_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VM with no agent fails after the cap, not after a long script deadline such as the repair's."""
    ticks = itertools.count(0.0, 2.0)
    seen: list[float] = []
    monkeypatch.setattr(
        "agentworks.plugins.proxmox.guest_agent.time.monotonic", lambda: seen.append(next(ticks)) or seen[-1]
    )
    api = FakeAPI([], pings=[ProxmoxAPIError("not running")] * 1000)

    # Unreachable, so a caller stops instead of waiting out the cap again.
    with pytest.raises(ChannelUnreachable) as caught:
        run(api, deadline=600)

    assert max(seen) <= guest_agent.AGENT_WAIT_SECONDS + 2 * guest_agent.POLL_INTERVAL_SECONDS
    assert api.execs == []
    assert caught.value.hint
    assert caught.value.__context__ is None


def test_a_rejected_token_on_the_ping_raises_at_once() -> None:
    api = FakeAPI([], pings=[_rejected(401), None])

    with pytest.raises(TokenRejectedError) as caught:
        run(api)

    assert api.ping_count == 1
    assert api.execs == []
    assert caught.value.__context__ is None


def test_a_lone_surrogate_in_the_output_is_replaced_rather_than_raised() -> None:
    api = FakeAPI([{"exited": True, "exitcode": 0, "out-data": "a\ud800b", "err-data": "\udfff"}])

    raw = run(api)

    assert (raw.stdout, raw.stderr) == (b"a?b", b"?")


def test_a_client_bug_on_the_ping_surfaces_at_once() -> None:
    """Only the API's own failures mean the agent is not answering yet."""
    api = FakeAPI([], pings=[TypeError("a bug in the client"), None])

    with pytest.raises(TypeError):
        run(api)

    assert api.ping_count == 1
    assert api.execs == []


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
