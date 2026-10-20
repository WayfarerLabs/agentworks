"""Bounded rollback wire facts, without native cleanup or task-identity claims."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers._proxmox_http import _request
from agentworks.execution.carriers.proxmox import ProxmoxConnection, _ProxmoxWire, _WireFailure
from tests.execution.test_proxmox import connection, interrupted_worker, stub_process, worker_payload
from tests.execution.test_proxmox_activation_wire import _ActivationEndpoint
from tests.execution.test_proxmox_trust import _Endpoint
from tests.execution.test_proxmox_trust import endpoint as endpoint

UPID = "UPID:node1:00000001:100000001:00000001:qmdestroy:123:user@pve!token:"
ROUTES = ["stop", "destroy", "free"]
WORKER_ROUTES = [
    ("vm-stop", "POST", None),
    ("vm-destroy", "DELETE", None),
    ("vmid-free", "GET", None),
]


@pytest.fixture
def rollback_endpoint(endpoint: _Endpoint) -> Iterator[_ActivationEndpoint]:
    value = _ActivationEndpoint(
        _ProxmoxWire(
            ProxmoxConnection(endpoint.url, "node1", 123, "user@pve!token", "secret-canary", endpoint.ca_bundle)
        ),
        [],
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.reply()

        def do_DELETE(self) -> None:
            self.reply()

        def do_GET(self) -> None:
            self.reply()

        def reply(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            value.requests.append((self.command, self.path, body, self.headers.get("Authorization")))
            self.send_response(value.status)
            if 300 <= value.status < 400:
                self.send_header("Location", endpoint.url + "/redirect-target")
            self.send_header("Content-Length", str(len(value.response)))
            self.end_headers()
            self.wfile.write(value.response)

        def log_message(self, format: str, *args: object) -> None:
            pass

    endpoint.server.RequestHandlerClass = Handler
    try:
        yield value
    finally:
        assert value.custody.close(Deadline.after(3))


def _call(wire: _ProxmoxWire, route: str, custody: LocalDeliveryCustody, *, timeout: float = 30) -> object:
    if route == "stop":
        return wire.request_vm_stop(custody=custody, timeout=timeout)
    if route == "destroy":
        return wire.request_vm_destroy(custody=custody, timeout=timeout)
    assert route == "free"
    return wire.request_vmid_free(custody=custody, timeout=timeout)


def _reply(route: str) -> bytes:
    if route in ("stop", "destroy"):
        return json.dumps({"data": UPID}).encode()
    assert route == "free"
    return b'{"data":123}'


@pytest.mark.windows
def test_owned_tls_fixed_rollback_routes(
    rollback_endpoint: _ActivationEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    value = rollback_endpoint
    for route in ROUTES:
        value.response = _reply(route)
        result = _call(value.wire, route, value.custody)
        assert result == (UPID if route in ("stop", "destroy") else None)
        assert value.custody.settled
    assert value.requests == [
        ("POST", "/api2/json/nodes/node1/qemu/123/status/stop", b"", "PVEAPIToken=user@pve!token=secret-canary"),
        ("DELETE", "/api2/json/nodes/node1/qemu/123", b"", "PVEAPIToken=user@pve!token=secret-canary"),
        ("GET", "/api2/json/cluster/nextid?vmid=123", b"", "PVEAPIToken=user@pve!token=secret-canary"),
    ]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("timeout", [None, 0, -1, True, "1", float("inf"), float("nan"), 10**1000])
def test_invalid_deadline_refuses_before_worker(monkeypatch: pytest.MonkeyPatch, route: str, timeout: Any) -> None:
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(ValidationError):
        _call(_ProxmoxWire(connection()), route, LocalDeliveryCustody(), timeout=timeout)
    spawn.assert_not_called()


@pytest.mark.parametrize("route", ROUTES)
def test_unsettled_custody_refuses_before_worker(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    custody = LocalDeliveryCustody()
    custody.begin_process()
    try:
        with pytest.raises(StateError):
            _call(_ProxmoxWire(connection()), route, custody, timeout=1)
        spawn.assert_not_called()
    finally:
        assert custody.close(Deadline.after(3))


@pytest.mark.parametrize("route", ["stop", "destroy"])
@pytest.mark.parametrize("receipt", [None, "", {}, [], True, 123, "a" * 256, "\u00e9" * 128, "\ud800"])
def test_invalid_receipt_fails_after_one_request(monkeypatch: pytest.MonkeyPatch, route: str, receipt: object) -> None:
    process = stub_process(monkeypatch, json.dumps({"data": receipt}).encode())
    with pytest.raises(_WireFailure):
        _call(_ProxmoxWire(connection()), route, LocalDeliveryCustody(), timeout=1)
    assert process.exchange.call_count == 1


@pytest.mark.parametrize("route", ["stop", "destroy"])
@pytest.mark.parametrize("receipt", [UPID, "raw opaque receipt", "a" * 255, "\u00e9" * 127 + "a"])
def test_receipt_is_returned_without_task_or_cleanup_claims(
    monkeypatch: pytest.MonkeyPatch, route: str, receipt: str
) -> None:
    stub_process(monkeypatch, json.dumps({"data": receipt}).encode())
    assert _call(_ProxmoxWire(connection()), route, LocalDeliveryCustody(), timeout=1) == receipt


@pytest.mark.parametrize("candidate", [None, True, 0, -1, 124, "123", {}, [], "123&vmid=456"])
def test_free_vmid_requires_exact_integer_response(monkeypatch: pytest.MonkeyPatch, candidate: object) -> None:
    process = stub_process(monkeypatch, json.dumps({"data": candidate}).encode())
    with pytest.raises(_WireFailure):
        _call(_ProxmoxWire(connection()), "free", LocalDeliveryCustody(), timeout=1)
    assert process.exchange.call_count == 1


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reply", [b"invalid", b"[]", b"null", b"{}"])
def test_malformed_worker_envelope_fails_once(monkeypatch: pytest.MonkeyPatch, route: str, reply: bytes) -> None:
    process = stub_process(monkeypatch, reply)
    with pytest.raises(_WireFailure):
        _call(_ProxmoxWire(connection()), route, LocalDeliveryCustody(), timeout=1)
    assert process.exchange.call_count == 1


@pytest.mark.parametrize("endpoint,method,suffix", WORKER_ROUTES)
@pytest.mark.parametrize("change", [{"method": "PATCH"}, {"body": "{}"}, {"timeout": None}, {"timeout": True}])
def test_worker_revalidates_fixed_route_and_deadline(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, method: str, suffix: str | None, change: dict[str, object]
) -> None:
    build = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    payload = {**worker_payload(), "endpoint": endpoint, "method": method, "suffix": suffix, "body": None}
    with pytest.raises(ValueError):
        _request({**payload, **change})
    build.assert_not_called()


@pytest.mark.parametrize("endpoint,method,suffix", WORKER_ROUTES)
def test_worker_refuses_arbitrary_suffix(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, method: str, suffix: str | None
) -> None:
    build = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    with pytest.raises(ValueError):
        _request(
            {
                **worker_payload(),
                "endpoint": endpoint,
                "method": method,
                "suffix": "?purge=1",
                "body": None,
            }
        )
    build.assert_not_called()


@pytest.mark.parametrize("vmid", [True, "123&vmid=456", "123/../456", 0, -1])
def test_worker_free_query_requires_exact_integer_vmid(monkeypatch: pytest.MonkeyPatch, vmid: object) -> None:
    build = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    payload = {**worker_payload(), "endpoint": "vmid-free", "method": "GET", "suffix": None, "body": None}
    payload["connection"]["vmid"] = vmid
    with pytest.raises(ValueError):
        _request(payload)
    build.assert_not_called()


@pytest.mark.parametrize("endpoint,method,suffix", WORKER_ROUTES)
def test_worker_caps_entire_response(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, method: str, suffix: str | None
) -> None:
    monkeypatch.setattr("agentworks.execution.carriers._proxmox_http._MAX_RESPONSE_BYTES", 10)
    response = io.BytesIO(b"x" * 11)
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError):
        _request({**worker_payload(), "endpoint": endpoint, "method": method, "suffix": suffix, "body": None})
    assert response.closed


@pytest.mark.parametrize("route", ROUTES)
def test_startup_uses_original_budget_and_credentials_stay_on_stdin(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    process = stub_process(monkeypatch, _reply(route))
    process.before_exchange.side_effect = lambda: now.__setitem__(0, 102.0)
    custody = LocalDeliveryCustody()
    _call(_ProxmoxWire(connection()), route, custody, timeout=5)
    assert process.run_process.call_args.kwargs["custody"] is custody
    assert process.exchange.call_args.kwargs["timeout"] == 3
    assert json.loads(process.exchange.call_args.args[0])["connection"]["token_secret"] == "secret-canary"
    assert "secret-canary" not in repr(process.run_process.call_args)
    assert process.exchange.call_count == 1


@pytest.mark.windows
@pytest.mark.parametrize("route", ROUTES)
def test_deadline_kills_and_reaps_original_worker_without_replay(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    original = subprocess.Popen
    children = []

    def spawn(argv, **kwargs):
        child = original([sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    custody = LocalDeliveryCustody()
    try:
        with pytest.raises(_WireFailure):
            _call(_ProxmoxWire(connection()), route, custody, timeout=0.05)
    finally:
        assert custody.close(Deadline.after(3))
    assert len(children) == 1 and children[0].returncode is not None
    assert all(pipe is not None and pipe.closed for pipe in (children[0].stdin, children[0].stdout, children[0].stderr))


@pytest.mark.windows
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_interrupt_reaps_original_worker_and_preserves_control_exception(
    monkeypatch: pytest.MonkeyPatch, route: str, interruption: type[BaseException]
) -> None:
    control = interruption()
    custody = LocalDeliveryCustody()
    with interrupted_worker(monkeypatch, control, custody) as children:
        with pytest.raises(interruption) as caught:
            _call(_ProxmoxWire(connection()), route, custody, timeout=1)
        assert caught.value is control
        assert custody.settled
        assert len(children) == 1 and children[0].returncode is not None


@pytest.mark.windows
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 404, 500])
def test_http_errors_never_prove_absence_replay_or_disclose_provider_text(
    rollback_endpoint: _ActivationEndpoint, route: str, status: int
) -> None:
    value = rollback_endpoint
    value.status, value.response = status, b"secret-canary provider details"
    with pytest.raises(_WireFailure) as caught:
        _call(value.wire, route, value.custody)
    assert "secret-canary" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(value.requests) == 1 and value.custody.settled


@pytest.mark.windows
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("trust", ["unknown-ca", "wrong-host"])
def test_tls_verifies_peer_before_rollback_http(
    rollback_endpoint: _ActivationEndpoint, endpoint: _Endpoint, route: str, trust: str
) -> None:
    value = ProxmoxConnection(
        endpoint.url.replace("localhost", "127.0.0.1") if trust == "wrong-host" else endpoint.url,
        "node1",
        123,
        "user@pve!token",
        "secret-canary",
        endpoint.ca_bundle if trust == "wrong-host" else None,
    )
    with pytest.raises(_WireFailure):
        _call(_ProxmoxWire(value), route, rollback_endpoint.custody)
    assert rollback_endpoint.requests == []
