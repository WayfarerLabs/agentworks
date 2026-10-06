"""Passive provider power observations under one finite local budget."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import VMRow, VMStatus
from agentworks.errors import ConfigError, LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import _ProxmoxWire
from agentworks.plugins.proxmox.platform import ProxmoxPlatform

_UNKNOWN_STATUSES: tuple[object, ...] = ("paused", "starting", "RUNNING", " stopped", "", None, 1, True, [], {})


def platform(**overrides: object) -> ProxmoxPlatform:
    return ProxmoxPlatform(
        "cluster",
        {
            "api_url": "https://pve.example:8006",
            "node": "configured-node",
            "token_id": "operator@pve!token",
            "token_secret": "cluster-token",
            **overrides,
        },
    )


def vm() -> VMRow:
    return cast("VMRow", SimpleNamespace(name="vm-one", platform_metadata={"node": "recorded-node", "vmid": "123"}))


def context() -> RunContext:
    return RunContext(secrets=SimpleNamespace(get=lambda _name: "power-secret-canary"))


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"status": "running"}, VMStatus.RUNNING),
        ({"status": "stopped"}, VMStatus.STOPPED),
        *[({"status": status}, VMStatus.UNKNOWN) for status in _UNKNOWN_STATUSES],
        ({}, VMStatus.UNKNOWN),
    ],
)
def test_exact_status_and_passive_route(monkeypatch: pytest.MonkeyPatch, data: dict, expected: VMStatus) -> None:
    value = platform()
    for method in ("_api", "native_transport", "status", "start"):
        monkeypatch.setattr(value, method, MagicMock(side_effect=AssertionError("legacy or active call")))
    process = MagicMock(returncode=0)
    process.communicate.return_value = (json.dumps({"data": data}).encode(), None)
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    assert value.observe_execution_power(vm(), context(), deadline=Deadline.after(10)) == expected
    payload = json.loads(process.communicate.call_args.args[0])
    assert payload["method"] == "GET" and payload["suffix"] is None and payload["body"] is None
    assert payload["connection"]["node"] == "recorded-node"
    assert payload["connection"]["vmid"] == 123
    assert payload["connection"]["token_secret"] == "power-secret-canary"
    assert "power-secret-canary" not in repr(spawn.call_args)
    assert spawn.call_count == 1
    assert 0 < process.communicate.call_args.kwargs["timeout"] <= payload["timeout"] <= 10


@pytest.mark.parametrize("body", [b"invalid", b"[]", b'{"data":null}', b'{"data":[]}', b'{"data":"stopped"}'])
def test_bad_envelope_is_unknown_without_retry(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    process = MagicMock(returncode=0)
    process.communicate.return_value = (body, None)
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    assert platform().observe_execution_power(vm(), context(), deadline=Deadline.after(10)) == VMStatus.UNKNOWN
    spawn.assert_called_once()


@pytest.mark.parametrize("deadline,error", [(Deadline(None), ValidationError), (Deadline(0), LimitExceededError)])
def test_invalid_budget_never_resolves_secret_or_dispatches(
    monkeypatch: pytest.MonkeyPatch, deadline: Deadline, error: type[Exception]
) -> None:
    secret = MagicMock(side_effect=AssertionError("secret delivered"))
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(error):
        platform().observe_execution_power(vm(), RunContext(secrets=SimpleNamespace(get=secret)), deadline=deadline)
    spawn.assert_not_called()
    secret.assert_not_called()


def test_secret_resolution_consumes_original_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])

    def secret(_name: str) -> str:
        now[0] = 102.0
        return "power-secret-canary"

    read = MagicMock(return_value={"status": "running"})
    monkeypatch.setattr(_ProxmoxWire, "request_power", read)
    assert (
        platform().observe_execution_power(
            vm(), RunContext(secrets=SimpleNamespace(get=secret)), deadline=Deadline(105)
        )
        == VMStatus.RUNNING
    )
    read.assert_called_once_with(timeout=3.0)
    read.reset_mock()
    with pytest.raises(LimitExceededError):
        platform().observe_execution_power(
            vm(), RunContext(secrets=SimpleNamespace(get=secret)), deadline=Deadline(101)
        )
    read.assert_not_called()


def test_late_result_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])

    def read(_wire: _ProxmoxWire, *, timeout: float) -> dict[str, object]:
        now[0] += timeout
        return {"status": "stopped"}

    monkeypatch.setattr(_ProxmoxWire, "request_power", read)
    with pytest.raises(LimitExceededError):
        platform().observe_execution_power(vm(), context(), deadline=Deadline(105))


@pytest.mark.parametrize("failure", [OSError("power-secret-canary"), RuntimeError("power-secret-canary")])
def test_unavailable_is_unknown(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    read = MagicMock(side_effect=failure)
    monkeypatch.setattr(_ProxmoxWire, "request_power", read)
    assert platform().observe_execution_power(vm(), context(), deadline=Deadline.after(10)) == VMStatus.UNKNOWN
    read.assert_called_once()


def test_unsafe_trust_and_missing_metadata_are_preflight_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(ConfigError):
        platform(verify_ssl=False).observe_execution_power(vm(), context(), deadline=Deadline.after(10))
    row = vm()
    row.platform_metadata.clear()
    with pytest.raises(StateError):
        platform().observe_execution_power(row, context(), deadline=Deadline.after(10))
    spawn.assert_not_called()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_control_exception_kills_and_reaps(monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException]) -> None:
    process = MagicMock()
    process.communicate.side_effect = [interruption(), (b"", None)]
    process.poll.return_value = None
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(interruption):
        platform().observe_execution_power(vm(), context(), deadline=Deadline.after(10))
    process.kill.assert_called_once()
    assert process.communicate.call_count == 2
    spawn.assert_called_once()


def test_worker_timeout_reaps_and_raises_safe_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    process = MagicMock()
    process.poll.return_value = None
    calls = []

    def communicate(payload=None, *, timeout=None):
        calls.append((payload, timeout))
        if timeout is not None:
            now[0] += timeout
            raise subprocess.TimeoutExpired("power-secret-canary", timeout)
        return b"", None

    process.communicate.side_effect = communicate
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(LimitExceededError) as raised:
        platform().observe_execution_power(vm(), context(), deadline=Deadline(105))
    process.kill.assert_called_once()
    assert len(calls) == 2 and calls[1] == (None, None)
    assert "power-secret-canary" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    spawn.assert_called_once()
