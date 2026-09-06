"""Behavioral contract for the Proxmox QGA execution transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from agentworks.plugins.proxmox.api import ProxmoxAPI, ProxmoxAPIError
from agentworks.plugins.proxmox.transport import ProxmoxExecTransport
from agentworks.ssh import SSHError, SSHResult

_SECRET = "tskey-proxmox-transport-sentinel"


@dataclass
class _Clock:
    now: float = 100.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _API:
    def __init__(self, statuses: list[dict[str, Any] | BaseException] | None = None) -> None:
        self.statuses = list(statuses or [{"exited": True, "exitcode": 0}])
        self.dispatches: list[dict[str, Any]] = []
        self.status_calls: list[dict[str, Any]] = []
        self.dispatch_error: BaseException | None = None

    def guest_agent_exec(
        self,
        node: str,
        vmid: int,
        *,
        command: list[str],
        input_data: str | None = None,
        timeout: float | None = None,
    ) -> int:
        self.dispatches.append(
            {
                "node": node,
                "vmid": vmid,
                "command": command,
                "input_data": input_data,
                "timeout": timeout,
            }
        )
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return 42

    def guest_agent_exec_status(
        self,
        node: str,
        vmid: int,
        *,
        pid: int,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.status_calls.append({"node": node, "vmid": vmid, "pid": pid, "timeout": timeout})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, BaseException):
            raise status
        return status


class _Logger:
    def __init__(self) -> None:
        self.commands: list[tuple[str, SSHResult]] = []
        self.errors: list[str] = []

    def log_command(self, command: str, result: SSHResult) -> None:
        self.commands.append((command, result))

    def log_error(self, message: str) -> None:
        self.errors.append(message)


def _transport(
    api: _API,
    *,
    clock: _Clock | None = None,
    logger: _Logger | None = None,
    default_timeout: int | None = None,
) -> ProxmoxExecTransport:
    clock = clock or _Clock()
    return ProxmoxExecTransport(
        cast("ProxmoxAPI", api),
        node="pve1",
        vmid=101,
        admin_username="agentworks",
        logger=logger,  # type: ignore[arg-type]
        default_timeout=default_timeout,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def _assert_secret_absent(value: object) -> None:
    assert _SECRET not in repr(value)


def _assert_exception_graph_is_secret_free(failure: BaseException) -> None:
    pending = [failure]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        _assert_secret_absent(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


@pytest.mark.parametrize(
    ("sudo", "expected"),
    [
        (
            False,
            ["/usr/sbin/runuser", "-u", "agentworks", "--", "/bin/bash", "-lc", "echo one && echo two"],
        ),
        (True, ["/bin/bash", "-lc", "echo one && echo two"]),
    ],
)
def test_command_identity_is_rendered_as_qga_argv(sudo: bool, expected: list[str]) -> None:
    api = _API()

    _transport(api).run("echo one && echo two", sudo=sudo)

    assert api.dispatches[0]["command"] == expected


def test_success_maps_complete_output_and_logs_it() -> None:
    api = _API([{"exited": True, "exitcode": 0, "out-data": "out\n", "err-data": "err\n"}])
    logger = _Logger()

    result = _transport(api, logger=logger).run("printf output")

    assert result == SSHResult(returncode=0, stdout="out\n", stderr="err\n")
    assert logger.commands == [("printf output", result)]
    assert logger.errors == []


@pytest.mark.parametrize(
    ("status", "returncode"),
    [
        ({"exited": True, "exitcode": 7, "out-data": "out", "err-data": "err"}, 7),
        ({"exited": True, "signal": 15, "out-data": "out", "err-data": "err"}, -15),
    ],
)
def test_unchecked_failure_returns_real_status(status: dict[str, Any], returncode: int) -> None:
    result = _transport(_API([status])).run("false", check=False)

    assert result == SSHResult(returncode=returncode, stdout="out", stderr="err")


@pytest.mark.parametrize(
    "status",
    [
        {"exited": True, "exitcode": 7, "err-data": "failure"},
        {"exited": True, "signal": 9},
    ],
)
def test_checked_failure_logs_completed_result_then_raises(status: dict[str, Any]) -> None:
    logger = _Logger()

    with pytest.raises(SSHError):
        _transport(_API([status]), logger=logger).run("false")

    assert len(logger.commands) == 1
    assert logger.errors == []


def test_sensitive_input_has_one_carrier_and_suppresses_output() -> None:
    api = _API(
        [
            {
                "exited": True,
                "exitcode": 0,
                "out-data": f"reflected {_SECRET}",
                "err-data": f"reflected {_SECRET}",
            }
        ]
    )
    logger = _Logger()

    result = _transport(api, logger=logger).run("read secret", input_text=f"{_SECRET}\n")

    assert api.dispatches[0]["input_data"] == f"{_SECRET}\n"
    _assert_secret_absent(api.dispatches[0]["command"])
    assert result == SSHResult(returncode=0, stdout="", stderr="")
    _assert_secret_absent(logger.commands)
    _assert_secret_absent(logger.errors)


@pytest.mark.parametrize(
    "provider_failure",
    [
        ProxmoxAPIError(f"provider reflected {_SECRET}"),
        RuntimeError(f"unexpected provider failure reflected {_SECRET}"),
    ],
)
def test_sensitive_provider_failure_is_sanitized_without_chaining(
    provider_failure: BaseException,
) -> None:
    api = _API()
    api.dispatch_error = provider_failure

    with pytest.raises(SSHError) as caught:
        _transport(api).run("read secret", input_text=f"{_SECRET}\n")

    assert len(api.dispatches) == 1
    _assert_exception_graph_is_secret_free(caught.value)


def test_sensitive_status_failure_is_sanitized_without_chaining() -> None:
    api = _API([RuntimeError(f"provider reflected {_SECRET}")])

    with pytest.raises(SSHError) as caught:
        _transport(api).run("read secret", input_text=f"{_SECRET}\n")

    assert len(api.dispatches) == 1
    assert len(api.status_calls) == 1
    _assert_exception_graph_is_secret_free(caught.value)


def test_input_data_provider_boundary_is_checked_before_dispatch() -> None:
    accepted = _API()
    _transport(accepted).run("read secret", input_text="x" * 65_536)
    assert len(accepted.dispatches) == 1

    rejected = _API()
    with pytest.raises(ValueError):
        _transport(rejected).run("read secret", input_text="x" * 65_537)
    assert rejected.dispatches == []


def test_default_timeout_is_one_deadline_across_dispatch_and_polling() -> None:
    clock = _Clock()
    api = _API([{"exited": False}, {"exited": True, "exitcode": 0}])

    _transport(api, clock=clock, default_timeout=10).run("true")

    assert api.dispatches[0]["timeout"] == 10
    assert api.status_calls[0]["timeout"] == 10
    assert api.status_calls[1]["timeout"] == 8
    assert clock.sleeps == [2.0]


def test_timeout_after_dispatch_reports_pid_without_redispatch() -> None:
    clock = _Clock()
    api = _API([{"exited": False}])
    logger = _Logger()

    with pytest.raises(SSHError) as caught:
        _transport(api, clock=clock, logger=logger).run("long-operation", timeout=1)

    assert len(api.dispatches) == 1
    assert len(api.status_calls) == 1
    assert clock.sleeps == [1]
    assert "42" in str(caught.value)
    assert logger.errors == [str(caught.value)]


@pytest.mark.parametrize(
    "status",
    [
        {},
        {"exited": "yes"},
        {"exited": True},
        {"exited": True, "exitcode": 0, "signal": 1},
        {"exited": True, "exitcode": "0"},
        {"exited": True, "exitcode": 0, "out-data": 1},
        {"exited": True, "exitcode": 0, "out-truncated": "yes"},
        {"exited": True, "exitcode": 0, "out-truncated": True},
        {"exited": True, "exitcode": 0, "err-truncated": True},
    ],
)
def test_invalid_or_incomplete_status_is_never_returned(status: dict[str, Any]) -> None:
    with pytest.raises(SSHError) as caught:
        _transport(_API([status])).run("true")

    assert "proxmox:101@pve1" in str(caught.value)
    assert "42" in str(caught.value)


def test_status_api_failure_reports_possible_continuation_without_redispatch() -> None:
    api = _API([ProxmoxAPIError("unavailable")])

    with pytest.raises(SSHError) as caught:
        _transport(api).run("mutation")

    assert len(api.dispatches) == 1
    assert len(api.status_calls) == 1
    assert "42" in str(caught.value)
