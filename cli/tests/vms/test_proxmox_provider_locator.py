"""Live generation observation boundaries without provider access."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import VMRow
from agentworks.errors import (
    AuthorizationError,
    ConfigError,
    ConnectivityError,
    LimitExceededError,
    StateError,
    ValidationError,
)
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import ProxmoxConnection, _ProxmoxWire
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from tests.execution.test_proxmox import stub_process

_GENERATION = "613ea898-8445-4e6e-82c7-f6e9ae8d7235"


def _vm(*, vmid: object = "123") -> VMRow:
    return cast(VMRow, SimpleNamespace(name="test-vm", platform_metadata={"vmid": vmid, "node": "node1"}))


def _platform(**overrides: object) -> ProxmoxPlatform:
    return ProxmoxPlatform(
        "proxmox",
        {"api_url": "https://pve.example:8006", "node": "node1", "token_id": "user@pve!token", **overrides},
    )


@pytest.fixture
def observation(monkeypatch: pytest.MonkeyPatch) -> tuple[ProxmoxPlatform, MagicMock]:
    platform = _platform()
    connection = ProxmoxConnection("https://pve.example:8006", "node1", 123, "user@pve!token", "secret-canary")
    monkeypatch.setattr(platform, "_execution_connection", lambda *_args: connection)
    for name in ("_api", "start", "native_transport"):
        monkeypatch.setattr(platform, name, MagicMock(side_effect=AssertionError("unexpected legacy or start call")))
    monkeypatch.setattr(_ProxmoxWire, "request", MagicMock(side_effect=AssertionError("unexpected QGA request")))
    response = MagicMock(return_value={"vmgenid": _GENERATION})
    monkeypatch.setattr(_ProxmoxWire, "request_current_config", response)
    return platform, response


def test_generation_is_live_bounded_and_opaque(observation: tuple[ProxmoxPlatform, MagicMock]) -> None:
    local_delivery = LocalDeliveryCustody()
    platform, request = observation
    result = platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
    assert isinstance(result, ProviderLocator)
    assert len(result.token.encode()) < 4096
    assert all(value not in result.token for value in (_GENERATION, "pve.example", "secret-canary"))
    request.assert_called_once()
    assert 0 < request.call_args.kwargs["timeout"] <= 10
    request.return_value = {"vmgenid": "7884ca9f-9142-4bc9-a6d5-5a47d7358fe1"}
    assert (
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
        != result
    )


@pytest.mark.parametrize(
    "generation",
    [
        None,
        "",
        "0",
        "1",
        0,
        1,
        False,
        [],
        {},
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",
        _GENERATION.replace("-", ""),
        "{" + _GENERATION + "}",
        " " + _GENERATION,
        _GENERATION + "\n",
    ],
)
def test_missing_disabled_or_noncanonical_generation_is_failed_evidence(
    observation: tuple[ProxmoxPlatform, MagicMock], generation: object
) -> None:
    local_delivery = LocalDeliveryCustody()
    platform, request = observation
    request.return_value = {} if generation is None else {"vmgenid": generation}
    with pytest.raises(StateError):
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)


def test_uuid_case_normalizes(observation: tuple[ProxmoxPlatform, MagicMock]) -> None:
    local_delivery = LocalDeliveryCustody()
    platform, request = observation
    first = platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
    request.return_value = {"vmgenid": _GENERATION.upper()}
    assert (
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
        == first
    )


@pytest.mark.parametrize("field", ["node", "ca_bundle", "token_id", "token_secret", "api_url", "vmid"])
def test_namespace_uses_only_exact_origin_vmid_and_generation(
    observation: tuple[ProxmoxPlatform, MagicMock], monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    local_delivery = LocalDeliveryCustody()
    platform, _request = observation
    connection = ProxmoxConnection("https://pve.example:8006", "node1", 123, "user@pve!token", "secret-canary")
    first = platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
    values: dict[str, object] = {
        "node": "node2",
        "ca_bundle": Path("rotated-ca.pem"),
        "token_id": "other@pve!token",
        "token_secret": "rotated-secret",
        "api_url": "https://pve.example:8006/",
        "vmid": 124,
    }
    changed = replace(connection, **{field: values[field]})
    monkeypatch.setattr(platform, "_execution_connection", lambda *_args: changed)
    second = platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
    assert (second == first) == (field not in ("api_url", "vmid"))


@pytest.mark.parametrize(
    "origin",
    [
        "https://PVE.example:8006",
        "https://pve.example:8006/",
        "https://alias.example:8006",
        "https://pve.example",
        "https://pve.example:443",
        "https://" + "a" * 5000 + ".example",
    ],
)
def test_exact_configured_origin_remains_a_distinct_bounded_namespace(
    monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    local_delivery = LocalDeliveryCustody()
    request = MagicMock(return_value={"vmgenid": _GENERATION})
    monkeypatch.setattr(_ProxmoxWire, "request_current_config", request)
    ctx = RunContext(secrets=SimpleNamespace(get=lambda _name: "secret-canary"))
    vm = _vm()
    metadata = dict(vm.platform_metadata)
    initial = _platform().observe_provider_locator(vm, ctx, deadline=Deadline.after(10), custody=local_delivery)
    changed = _platform(api_url=origin).observe_provider_locator(
        vm, ctx, deadline=Deadline.after(10), custody=local_delivery
    )
    assert isinstance(changed, ProviderLocator)
    assert changed != initial
    assert len(changed.token.encode()) < 4096
    assert vm.platform_metadata == metadata


def test_actual_connection_and_fixed_request_use_scoped_token_without_legacy_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_delivery = LocalDeliveryCustody()
    platform = _platform(token_secret="locator-token", node="configured-node")
    for method in ("_api", "native_transport", "status", "start"):
        monkeypatch.setattr(platform, method, MagicMock(side_effect=AssertionError("unexpected legacy or active call")))
    process = stub_process(monkeypatch, json.dumps({"data": {"vmgenid": _GENERATION}}).encode())
    secret = MagicMock(return_value="secret-canary")
    ctx = RunContext(secrets=SimpleNamespace(get=secret))
    result = platform.observe_provider_locator(_vm(), ctx, deadline=Deadline.after(10), custody=local_delivery)
    assert isinstance(result, ProviderLocator)
    payload = json.loads(process.exchange.call_args.args[0])
    assert payload["endpoint"] == "current-config"
    assert payload["method"] == "GET" and payload["suffix"] is None and payload["body"] is None
    assert payload["connection"]["api_url"] == platform.config.api_url
    assert payload["connection"]["node"] == "node1"
    assert payload["connection"]["vmid"] == 123
    assert payload["connection"]["token_secret"] == "secret-canary"
    secret.assert_called_once_with("locator-token")
    process.run_process.assert_called_once()
    assert process.run_process.call_args.kwargs["custody"] is local_delivery
    assert "secret-canary" not in repr(process.run_process.call_args.args)
    assert 0 < process.exchange.call_args.kwargs["timeout"] <= payload["timeout"] <= 10


@pytest.mark.parametrize(
    "raw_vmid",
    [
        None,
        True,
        False,
        123.9,
        123.0,
        0,
        -123,
        "",
        "0",
        "-123",
        "+123",
        "123.9",
        " 123",
        "123\n",
        "１２３",
        "١٢٣",
        "secret-canary",
        [],
        {},
        object(),
    ],
)
def test_invalid_persisted_vmid_never_delivers_secrets_or_selects_provider_vm(
    monkeypatch: pytest.MonkeyPatch, raw_vmid: object
) -> None:
    local_delivery = LocalDeliveryCustody()
    platform = _platform()
    row = _vm(vmid=raw_vmid)
    secret = MagicMock(side_effect=AssertionError("unexpected secret access"))
    request = MagicMock(side_effect=AssertionError("unexpected provider request"))
    monkeypatch.setattr(_ProxmoxWire, "request_current_config", request)
    with pytest.raises(StateError) as raised:
        platform.observe_provider_locator(
            row, RunContext(secrets=SimpleNamespace(get=secret)), deadline=Deadline.after(10), custody=local_delivery
        )
    secret.assert_not_called()
    request.assert_not_called()
    assert "secret-canary" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.parametrize("raw_vmid", [123, "123", "00123"])
def test_exact_integer_and_decimal_persisted_vmid_identify_the_same_vm(
    monkeypatch: pytest.MonkeyPatch, raw_vmid: int | str
) -> None:
    local_delivery = LocalDeliveryCustody()
    request = MagicMock(return_value={"vmgenid": _GENERATION})
    monkeypatch.setattr(_ProxmoxWire, "request_current_config", request)
    ctx = RunContext(secrets=SimpleNamespace(get=lambda _name: "secret-canary"))
    row = _vm()
    baseline = _platform().observe_provider_locator(row, ctx, deadline=Deadline.after(10), custody=local_delivery)
    row = _vm(vmid=raw_vmid)
    platform = _platform()
    assert platform._execution_connection(row, ctx).vmid == 123
    assert platform.observe_provider_locator(row, ctx, deadline=Deadline.after(10), custody=local_delivery) == baseline


@pytest.mark.parametrize(
    "body", [b"secret-canary", b"[]", b'{"data":null}', b'{"data":[]}', b'{"data":"secret-canary"}']
)
def test_invalid_provider_envelope_is_sanitized_without_retry(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    local_delivery = LocalDeliveryCustody()
    process = stub_process(monkeypatch, body)
    ctx = RunContext(secrets=SimpleNamespace(get=lambda _name: "secret-canary"))
    with pytest.raises(ConnectivityError) as raised:
        _platform().observe_provider_locator(_vm(), ctx, deadline=Deadline.after(10), custody=local_delivery)
    process.run_process.assert_called_once()
    assert "secret-canary" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.parametrize(
    "deadline",
    [Deadline(None), Deadline(-1.0), cast(Deadline, SimpleNamespace(expires_at=1e10, remaining=lambda: 1.0))],
)
def test_invalid_or_expired_budget_never_prepares_secrets(monkeypatch: pytest.MonkeyPatch, deadline: Deadline) -> None:
    local_delivery = LocalDeliveryCustody()
    platform = _platform()
    prepare = MagicMock(side_effect=AssertionError("unexpected secret preparation"))
    monkeypatch.setattr(platform, "_execution_connection", prepare)
    with pytest.raises((ValidationError, LimitExceededError)):
        platform.observe_provider_locator(_vm(), RunContext(), deadline=deadline, custody=local_delivery)
    prepare.assert_not_called()


@pytest.mark.parametrize("expire_during", ["preparation", "response", "hash"])
def test_expiry_after_preparation_or_response_refuses_observation(
    monkeypatch: pytest.MonkeyPatch, expire_during: str
) -> None:
    local_delivery = LocalDeliveryCustody()
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    platform = _platform()
    ctx = RunContext(secrets=SimpleNamespace(get=lambda _name: "secret-canary"))
    request = MagicMock(return_value={"vmgenid": _GENERATION})
    monkeypatch.setattr(_ProxmoxWire, "request_current_config", request)

    if expire_during == "preparation":
        original_prepare = platform._execution_connection

        def prepare(vm: VMRow, context: RunContext) -> ProxmoxConnection:
            connection = original_prepare(vm, context)
            now[0] = 106.0
            return connection

        monkeypatch.setattr(platform, "_execution_connection", prepare)
    elif expire_during == "response":

        def respond(*, timeout: float, custody: LocalDeliveryCustody) -> dict[str, object]:
            now[0] = 106.0
            return {"vmgenid": _GENERATION}

        request.side_effect = respond
    else:
        original_hash = hashlib.sha256

        def hash_and_expire(data: bytes) -> object:
            digest = original_hash(data)
            now[0] = 106.0
            return digest

        monkeypatch.setattr(hashlib, "sha256", hash_and_expire)

    with pytest.raises(LimitExceededError):
        platform.observe_provider_locator(_vm(), ctx, deadline=Deadline(105), custody=local_delivery)
    assert request.call_count == (0 if expire_during == "preparation" else 1)


@pytest.mark.parametrize("expired", [False, True])
def test_secret_resolution_consumes_the_original_deadline(monkeypatch: pytest.MonkeyPatch, expired: bool) -> None:
    local_delivery = LocalDeliveryCustody()
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])

    def secret(_name: str) -> str:
        now[0] = 106.0 if expired else 102.0
        return "secret-canary"

    request = MagicMock(return_value={"vmgenid": _GENERATION})
    monkeypatch.setattr(_ProxmoxWire, "request_current_config", request)
    ctx = RunContext(secrets=SimpleNamespace(get=secret))
    if expired:
        with pytest.raises(LimitExceededError):
            _platform().observe_provider_locator(_vm(), ctx, deadline=Deadline(105), custody=local_delivery)
        request.assert_not_called()
    else:
        assert isinstance(
            _platform().observe_provider_locator(_vm(), ctx, deadline=Deadline(105), custody=local_delivery),
            ProviderLocator,
        )
        assert request.call_args.kwargs["timeout"] == 3


@pytest.mark.parametrize("failure", [OSError, TimeoutError])
def test_late_provider_failure_still_checks_deadline(
    observation: tuple[ProxmoxPlatform, MagicMock], monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    local_delivery = LocalDeliveryCustody()
    platform, request = observation
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])

    def fail(*, timeout: float, custody: LocalDeliveryCustody) -> dict[str, object]:
        now[0] = 106.0
        raise failure("secret-canary")

    request.side_effect = fail
    with pytest.raises(LimitExceededError):
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline(105), custody=local_delivery)
    request.assert_called_once()


def test_unverified_configuration_never_prepares_token(monkeypatch: pytest.MonkeyPatch) -> None:
    local_delivery = LocalDeliveryCustody()
    secret = MagicMock(side_effect=AssertionError("unexpected secret access"))
    monkeypatch.setattr(RunContext, "secret", secret)
    with pytest.raises(ConfigError):
        _platform(verify_ssl=False).observe_provider_locator(
            _vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery
        )
    secret.assert_not_called()


@pytest.mark.parametrize("error_type", [ConfigError, AuthorizationError])
def test_configuration_and_secret_refusal_preserve_original_error(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    local_delivery = LocalDeliveryCustody()
    platform = _platform()
    error = error_type("refused")
    secret = MagicMock(side_effect=error)
    request = MagicMock(side_effect=AssertionError("unexpected request"))
    monkeypatch.setattr(RunContext, "secret", secret)
    monkeypatch.setattr(_ProxmoxWire, "request_current_config", request)
    with pytest.raises(error_type) as raised:
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
    assert raised.value is error
    request.assert_not_called()


def test_provider_failure_is_sanitized(observation: tuple[ProxmoxPlatform, MagicMock]) -> None:
    local_delivery = LocalDeliveryCustody()
    platform, request = observation
    request.side_effect = OSError("secret-canary")
    with pytest.raises(ConnectivityError) as raised:
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
    assert "secret-canary" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_control_flow_propagates(
    observation: tuple[ProxmoxPlatform, MagicMock], interruption: type[BaseException]
) -> None:
    local_delivery = LocalDeliveryCustody()
    platform, request = observation
    request.side_effect = interruption
    with pytest.raises(interruption):
        platform.observe_provider_locator(_vm(), RunContext(), deadline=Deadline.after(10), custody=local_delivery)
