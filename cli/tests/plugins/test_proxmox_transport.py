"""Behavioral contract for the Proxmox QGA execution transport."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.plugins.proxmox.api import ProxmoxAPI, ProxmoxAPIError
from agentworks.plugins.proxmox.transport import ProxmoxExecTransport
from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import native_transport as build_native_transport

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
    logger: _Logger | None = None,
    default_timeout: int | None = None,
) -> ProxmoxExecTransport:
    return ProxmoxExecTransport(
        cast("ProxmoxAPI", api),
        node="pve1",
        vmid=101,
        admin_username="agentworks",
        logger=logger,  # type: ignore[arg-type]
        default_timeout=default_timeout,
    )


def _patch_clock(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    monkeypatch.setattr("agentworks.plugins.proxmox.transport.time.monotonic", clock.monotonic)
    monkeypatch.setattr("agentworks.plugins.proxmox.transport.time.sleep", clock.sleep)


def _wire_response(data: object) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps({"data": data}).encode()
    response.__enter__ = lambda value: value
    response.__exit__ = MagicMock(return_value=False)
    return response


def _wire_api() -> ProxmoxAPI:
    return ProxmoxAPI(
        api_url="https://pve.example.com:8006",
        token_id="user@pam!token",
        token_secret="secret-value",
        verify_ssl=False,
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


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize(
    "status",
    [
        {"exited": True, "signal": 0},
        {"exited": True, "signal": -1},
        {"exited": True, "exitcode": -1},
    ],
)
def test_invalid_numeric_exit_status_is_rejected(status: dict[str, object], check: bool) -> None:
    with pytest.raises(SSHError):
        _transport(_API([status])).run("false", check=check)


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


@pytest.mark.parametrize(
    "provider_failure",
    [
        ProxmoxAPIError(f"provider reflected {_SECRET}"),
        RuntimeError(f"unexpected provider failure reflected {_SECRET}"),
    ],
)
def test_sensitive_status_failure_is_sanitized_without_chaining(
    provider_failure: BaseException,
) -> None:
    api = _API([provider_failure])

    with pytest.raises(SSHError) as caught:
        _transport(api).run("read secret", input_text=f"{_SECRET}\n")

    assert len(api.dispatches) == 1
    assert len(api.status_calls) == 1
    _assert_exception_graph_is_secret_free(caught.value)


def test_non_sensitive_dispatch_preserves_provider_failure() -> None:
    api = _API()
    failure = ProxmoxAPIError("provider dispatch diagnostic")
    failure.code = 503
    api.dispatch_error = failure

    with pytest.raises(SSHError) as caught:
        _transport(api).run("true")

    assert caught.value.__cause__ is failure
    assert failure.code == 503
    assert "provider dispatch diagnostic" in str(caught.value)
    assert _transport(api).describe() in str(caught.value)


def test_non_sensitive_status_preserves_provider_failure() -> None:
    failure = ProxmoxAPIError("provider status diagnostic")
    failure.code = 502
    api = _API([failure])

    with pytest.raises(SSHError) as caught:
        _transport(api).run("true")

    assert caught.value.__cause__ is failure
    assert failure.code == 502
    assert "provider status diagnostic" in str(caught.value)
    assert _transport(api).describe() in str(caught.value)
    assert "42" in str(caught.value)
    assert len(api.dispatches) == 1
    assert len(api.status_calls) == 1


def test_provider_failure_remains_retryable_by_native_factory() -> None:
    api = _API()
    target = _transport(api)
    platform = MagicMock()
    platform.name = "proxmox"
    platform.probe_failure_hint = None
    platform.transient_route.return_value = contextlib.nullcontext()
    platform.native_transport.return_value = target
    failure = ProxmoxAPIError("provider unavailable")

    with (
        patch.object(api, "guest_agent_exec", side_effect=[failure, 42]) as dispatch,
        patch("agentworks.transports.time.sleep"),
        contextlib.ExitStack() as stack,
    ):
        result = build_native_transport(
            MagicMock(),
            platform,
            MagicMock(),
            ctx=RunContext(),
            stack=stack,
        )

    assert result is target
    assert dispatch.call_count == 2


def test_input_data_provider_boundary_is_checked_before_dispatch() -> None:
    accepted = _API()
    _transport(accepted).run("read secret", input_text="x" * 65_536)
    assert len(accepted.dispatches) == 1

    rejected = _API()
    payload = f"{_SECRET}{'x' * 65_537}"
    with pytest.raises(SSHError) as caught:
        _transport(rejected).run("read secret", input_text=payload)
    assert rejected.dispatches == []
    _assert_exception_graph_is_secret_free(caught.value)


def test_default_timeout_is_one_deadline_across_dispatch_and_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    api = _API([{"exited": False}, {"exited": True, "exitcode": 0}])
    _patch_clock(monkeypatch, clock)

    _transport(api, default_timeout=10).run("true")

    assert api.dispatches[0]["timeout"] == 10
    assert api.status_calls[0]["timeout"] == 10
    assert api.status_calls[1]["timeout"] == 8
    assert clock.sleeps == [2.0]


def test_timeout_after_dispatch_reports_pid_without_redispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    api = _API([{"exited": False}])
    logger = _Logger()
    _patch_clock(monkeypatch, clock)

    with pytest.raises(SSHError) as caught:
        _transport(api, logger=logger).run("long-operation", timeout=1)

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
        {"exited": False, "exitcode": 0},
        {"exited": False, "signal": 9},
        {"exited": False, "out-data": ""},
        {"exited": False, "err-data": ""},
        {"exited": False, "out-truncated": False},
        {"exited": False, "err-truncated": False},
        {"exited": True},
        {"exited": True, "exitcode": 0, "signal": 1},
        {"exited": True, "exitcode": 0, "signal": None},
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


@patch("urllib.request.urlopen")
def test_numeric_wire_status_runs_then_exits(
    mock_urlopen: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_urlopen.side_effect = [
        _wire_response({"pid": 42}),
        _wire_response({"exited": 0}),
        _wire_response(
            {
                "exited": 1,
                "exitcode": 0,
                "out-data": "done\n",
                "out-truncated": 0,
                "err-truncated": 0,
            }
        ),
    ]
    monkeypatch.setattr("agentworks.plugins.proxmox.transport.time.sleep", lambda _seconds: None)

    result = ProxmoxExecTransport(
        _wire_api(),
        node="pve1",
        vmid=101,
        admin_username="agentworks",
    ).run("true")

    assert result == SSHResult(returncode=0, stdout="done\n", stderr="")
    assert mock_urlopen.call_count == 3


@patch("urllib.request.urlopen")
def test_numeric_wire_truncation_is_rejected(mock_urlopen: MagicMock) -> None:
    mock_urlopen.side_effect = [
        _wire_response({"pid": 42}),
        _wire_response(
            {
                "exited": 1,
                "exitcode": 0,
                "out-data": "partial",
                "out-truncated": 1,
                "err-truncated": 0,
            }
        ),
    ]

    with pytest.raises(SSHError):
        ProxmoxExecTransport(
            _wire_api(),
            node="pve1",
            vmid=101,
            admin_username="agentworks",
        ).run("true")
