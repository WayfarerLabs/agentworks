"""Fixed activation requests against local TLS and owned worker fixtures.

These observations establish wire behavior, not task matching, activation
settlement, generation freshness, cancellation or native provider acceptance.
"""

from __future__ import annotations

import io
import json
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carriers._proxmox_http import _request
from agentworks.execution.carriers.proxmox import ProxmoxConnection, _ProxmoxWire, _WireFailure
from tests.execution.test_proxmox import connection, interrupted_worker, stub_process, worker_payload
from tests.execution.test_proxmox_trust import _Endpoint
from tests.execution.test_proxmox_trust import endpoint as endpoint

if TYPE_CHECKING:
    from collections.abc import Iterator

_UPID = "UPID:node1:00000001:100000001:00000001:qmstart:123:user@pve!token:"


@dataclass
class _ActivationEndpoint:
    wire: _ProxmoxWire
    requests: list[tuple[str, str, bytes, str | None]]
    response: bytes = b'{"data":null}'
    status: int = 200


@pytest.fixture
def activation_endpoint(endpoint: _Endpoint) -> Iterator[_ActivationEndpoint]:
    value = _ActivationEndpoint(
        _ProxmoxWire(
            ProxmoxConnection(endpoint.url, "node1", 123, "user@pve!token", "secret-canary", endpoint.ca_bundle)
        ),
        [],
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
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
    yield value


def _call(wire: _ProxmoxWire, route: str, *, timeout: float = 30):
    if route == "info":
        return wire.request_guest_info(timeout=timeout, custody=LocalDeliveryCustody())
    return (
        wire.request_vm_start(timeout=timeout, custody=LocalDeliveryCustody())
        if route == "start"
        else wire.request_task_status(_UPID, timeout=timeout, custody=LocalDeliveryCustody())
    )


@pytest.mark.windows
@pytest.mark.parametrize("task_type", ["qmstart", "hastart"])
def test_owned_tls_start_and_task_reads_are_fixed_body_free(
    activation_endpoint: _ActivationEndpoint, monkeypatch: pytest.MonkeyPatch, task_type: str
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    value = activation_endpoint
    upid = _UPID.replace("qmstart", task_type)
    value.response = json.dumps({"data": upid}).encode()
    assert value.wire.request_vm_start(timeout=30, custody=LocalDeliveryCustody()) == upid
    status = {"status": "stopped", "exitstatus": "OK", "upid": upid}
    value.response = json.dumps({"data": status}).encode()
    assert value.wire.request_task_status(upid, timeout=30, custody=LocalDeliveryCustody()) == status
    encoded = urllib.parse.quote(upid, safe="")
    assert value.requests == [
        ("POST", "/api2/json/nodes/node1/qemu/123/status/start", b"", "PVEAPIToken=user@pve!token=secret-canary"),
        ("GET", f"/api2/json/nodes/node1/tasks/{encoded}/status", b"", "PVEAPIToken=user@pve!token=secret-canary"),
    ]


@pytest.mark.windows
@pytest.mark.parametrize("upid", ["a/b?#%\r\n\0", "owner@realm!\u00e9", ".", "..", "a" * 255, "\u00e9" * 127 + "a"])
def test_literal_task_id_is_one_encoded_component(activation_endpoint: _ActivationEndpoint, upid: str) -> None:
    value = activation_endpoint
    value.response = b'{"data":{"status":"running"}}'
    assert value.wire.request_task_status(upid, timeout=30, custody=LocalDeliveryCustody()) == {"status": "running"}
    encoded = urllib.parse.quote(upid, safe="").replace(".", "%2E")
    assert value.requests[0][1] == f"/api2/json/nodes/node1/tasks/{encoded}/status"
    assert value.requests[0][0] == "GET" and value.requests[0][2] == b""


@pytest.mark.parametrize("route", ["start", "task", "info"])
@pytest.mark.parametrize("timeout", [None, 0, -1, float("inf"), float("-inf"), float("nan"), True, "1", 10**1000])
def test_invalid_timeout_refuses_before_worker(monkeypatch: pytest.MonkeyPatch, route: str, timeout) -> None:
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(ValidationError):
        _call(_ProxmoxWire(connection()), route, timeout=timeout)
    spawn.assert_not_called()


@pytest.mark.parametrize("upid", [None, "", b"bytes", 42, "a" * 256, "\u00e9" * 128, "\ud800"])
def test_unretainable_task_id_refuses_before_worker(monkeypatch: pytest.MonkeyPatch, upid) -> None:
    spawn = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(ValidationError):
        _ProxmoxWire(connection()).request_task_status(upid, timeout=1, custody=LocalDeliveryCustody())
    spawn.assert_not_called()


@pytest.mark.parametrize("receipt", [None, "", {}, [], 42, "a" * 256, "\u00e9" * 128, "\ud800"])
def test_unretainable_start_reply_is_generic_failure_after_one_request(
    monkeypatch: pytest.MonkeyPatch, receipt
) -> None:
    process = stub_process(monkeypatch, json.dumps({"data": receipt}).encode())
    with pytest.raises(_WireFailure):
        _ProxmoxWire(connection()).request_vm_start(timeout=1, custody=LocalDeliveryCustody())
    assert process.exchange.call_count == 1
    payload = json.loads(process.exchange.call_args.args[0])
    assert payload["endpoint"] == "vm-start" and payload["method"] == "POST" and payload["body"] is None


@pytest.mark.parametrize("route", ["start", "task", "info"])
@pytest.mark.parametrize("reply", [b"invalid", b"[]", b"{}", b'{"data":null}', b'{"data":[]}'])
def test_missing_or_malformed_data_never_establishes_wire_reply(
    monkeypatch: pytest.MonkeyPatch, route: str, reply: bytes
):
    process = stub_process(monkeypatch, reply)
    with pytest.raises(_WireFailure):
        _call(_ProxmoxWire(connection()), route, timeout=1)
    assert process.exchange.call_count == 1


@pytest.mark.parametrize("receipt", ["a" * 255, "\u00e9" * 127 + "a", "arbitrary raw receipt"])
def test_bounded_start_receipt_remains_opaque(monkeypatch: pytest.MonkeyPatch, receipt: str) -> None:
    stub_process(monkeypatch, json.dumps({"data": receipt}).encode())
    assert _ProxmoxWire(connection()).request_vm_start(timeout=1, custody=LocalDeliveryCustody()) == receipt


@pytest.mark.parametrize("route,reply", [("start", b'{"data":{"status":"running"}}'), ("task", b'{"data":"raw"}')])
def test_scalar_and_dictionary_envelopes_are_distinct(monkeypatch: pytest.MonkeyPatch, route: str, reply: bytes):
    stub_process(monkeypatch, reply)
    with pytest.raises(_WireFailure):
        _call(_ProxmoxWire(connection()), route, timeout=1)


@pytest.mark.parametrize("route", ["start", "task", "info"])
def test_startup_consumes_worker_budget_and_credentials_stay_on_stdin(monkeypatch: pytest.MonkeyPatch, route: str):
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    response = json.dumps({"data": _UPID if route == "start" else {"status": "running"}}).encode()
    process = stub_process(monkeypatch, response)

    def before_exchange():
        now[0] = 102.0

    process.before_exchange.side_effect = before_exchange
    _call(_ProxmoxWire(connection()), route, timeout=5)
    assert process.exchange.call_args.kwargs["timeout"] == 3
    assert json.loads(process.exchange.call_args.args[0])["connection"]["token_secret"] == "secret-canary"
    assert "secret-canary" not in repr(process.run_process.call_args)


@pytest.mark.parametrize("route", ["start", "task", "info"])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_control_interrupt_kills_and_reaps_without_replay(monkeypatch: pytest.MonkeyPatch, route: str, interruption):
    control = interruption()
    with interrupted_worker(monkeypatch, control) as children:
        with pytest.raises(interruption) as caught:
            _call(_ProxmoxWire(connection()), route, timeout=1)
        assert caught.value is control
        assert len(children) == 1 and children[0].returncode is not None
        assert children[0].stdout is not None and children[0].stdout.closed


@pytest.mark.windows
@pytest.mark.parametrize("route", ["start", "task", "info"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 500])
def test_tls_http_failure_never_replays_or_discloses_provider_text(
    activation_endpoint: _ActivationEndpoint, route: str, status: int
) -> None:
    value = activation_endpoint
    value.status, value.response = status, b"secret-canary upstream task failure"
    with pytest.raises(_WireFailure) as caught:
        _call(value.wire, route)
    assert "secret-canary" not in str(caught.value)
    assert len(value.requests) == 1


@pytest.mark.windows
@pytest.mark.parametrize("route", ["start", "task", "info"])
@pytest.mark.parametrize("trust", ["unknown-ca", "wrong-host"])
def test_activation_tls_refuses_untrusted_peer_before_http(
    activation_endpoint: _ActivationEndpoint, endpoint: _Endpoint, route: str, trust: str
) -> None:
    untrusted = ProxmoxConnection(
        endpoint.url.replace("localhost", "127.0.0.1") if trust == "wrong-host" else endpoint.url,
        "node1",
        123,
        "user@pve!token",
        "secret-canary",
        endpoint.ca_bundle if trust == "wrong-host" else None,
    )
    with pytest.raises(_WireFailure):
        _call(_ProxmoxWire(untrusted), route)
    assert activation_endpoint.requests == []


@pytest.mark.parametrize(
    "endpoint,method,suffix,body",
    [
        ("vm-start", "GET", None, None),
        ("vm-start", "POST", "exec", None),
        ("vm-start", "POST", None, "{}"),
        ("task-status", "POST", _UPID, None),
        ("task-status", "GET", _UPID, "{}"),
        ("task-status", "GET", None, None),
        ("guest-agent", "POST", "../status/start", "{}"),
        ("guest-agent", "GET", "exec-status?pid=42&extra=1", None),
        ("guest-agent", "GET", "exec", None),
        ("guest-info", "POST", None, None),
        ("guest-info", "GET", "ping", None),
        ("guest-info", "GET", None, "{}"),
        ("guest-info", "GET", "../exec", None),
        ("unknown", "GET", None, None),
    ],
)
def test_worker_refuses_nonfixed_combinations(monkeypatch: pytest.MonkeyPatch, endpoint, method, suffix, body):
    build = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    with pytest.raises(ValueError):
        _request({**worker_payload(), "endpoint": endpoint, "method": method, "suffix": suffix, "body": body})
    build.assert_not_called()


def test_worker_rejects_old_endpoint_switch(monkeypatch: pytest.MonkeyPatch):
    build = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    payload = worker_payload()
    del payload["endpoint"]
    payload["current_config"] = True
    with pytest.raises(ValueError):
        _request(payload)
    build.assert_not_called()


@pytest.mark.parametrize(
    "endpoint,method,suffix",
    [("vm-start", "POST", None), ("task-status", "GET", _UPID), ("guest-info", "GET", None)],
)
@pytest.mark.parametrize("timeout", [None, 0, -1, float("inf"), float("nan")])
def test_worker_rejects_invalid_control_budget_before_network(
    monkeypatch: pytest.MonkeyPatch, endpoint, method, suffix, timeout
):
    build = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    with pytest.raises(ValueError):
        _request(
            {
                **worker_payload(),
                "endpoint": endpoint,
                "method": method,
                "suffix": suffix,
                "body": None,
                "timeout": timeout,
            }
        )
    build.assert_not_called()


@pytest.mark.parametrize("upid", ["", "a" * 256, "\u00e9" * 128, "\ud800"])
def test_worker_rejects_unretainable_task_before_network(monkeypatch: pytest.MonkeyPatch, upid):
    build = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    with pytest.raises(ValueError):
        _request({**worker_payload(), "endpoint": "task-status", "method": "GET", "suffix": upid, "body": None})
    build.assert_not_called()


@pytest.mark.parametrize(
    "endpoint,method,suffix",
    [("vm-start", "POST", None), ("task-status", "GET", _UPID), ("guest-info", "GET", None)],
)
def test_worker_retains_whole_reply_bound(monkeypatch: pytest.MonkeyPatch, endpoint, method, suffix):
    monkeypatch.setattr("agentworks.execution.carriers._proxmox_http._MAX_RESPONSE_BYTES", 10)
    response = io.BytesIO(b"x" * 11)
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError):
        _request({**worker_payload(), "endpoint": endpoint, "method": method, "suffix": suffix, "body": None})
    assert response.closed


@pytest.mark.windows
def test_owned_tls_guest_info_is_fixed_readonly(activation_endpoint, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    value = activation_endpoint
    response = {"result": {"version": "9.0", "supported_commands": []}}
    value.response = json.dumps({"data": response}).encode()
    assert value.wire.request_guest_info(timeout=30, custody=LocalDeliveryCustody()) == response
    assert value.requests == [
        ("GET", "/api2/json/nodes/node1/qemu/123/agent/info", b"", "PVEAPIToken=user@pve!token=secret-canary")
    ]
