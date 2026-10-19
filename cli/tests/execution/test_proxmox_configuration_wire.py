"""Fixed configuration PUTs against owned TLS, without native PVE acceptance."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, NoReturn

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers._proxmox_http import _request
from agentworks.execution.carriers.proxmox import (
    ProxmoxConfigurationRequest,
    ProxmoxConnection,
    _ProxmoxWire,
    _WireFailure,
)
from tests.execution.test_proxmox import connection, stub_process, worker_payload
from tests.execution.test_proxmox_trust import _Endpoint
from tests.execution.test_proxmox_trust import endpoint as endpoint
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY

REQUEST = ProxmoxConfigurationRequest(
    4, 8192, "admin", TEST_SSH_PUBLIC_KEY + "\n", "12345678-1234-1234-1234-123456789abc"
)
DIGEST = "a" * 40
UPID = "UPID:node1:000000AB:100000001:000000CD:resize:123:user@pve!token:"


@dataclass
class ConfigurationEndpoint:
    wire: _ProxmoxWire
    requests: list[tuple[str, str, bytes]] = field(default_factory=list)
    response: bytes = b'{"data":null}'
    status: int = 200
    custody: LocalDeliveryCustody = field(default_factory=LocalDeliveryCustody)


@pytest.fixture
def configuration_endpoint(endpoint: _Endpoint) -> Iterator[ConfigurationEndpoint]:
    value = ConfigurationEndpoint(
        _ProxmoxWire(
            ProxmoxConnection(endpoint.url, "node1", 123, "user@pve!token", "secret-canary", endpoint.ca_bundle)
        )
    )

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            value.requests.append((self.command, self.path, body))
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


@pytest.mark.windows
def test_owned_tls_configure_null_and_absolute_resize_upid(configuration_endpoint: ConfigurationEndpoint) -> None:
    value = configuration_endpoint
    assert value.wire.request_vm_configure(REQUEST, digest=DIGEST, timeout=10, custody=value.custody) is None
    value.response = json.dumps({"data": UPID}).encode()
    assert value.wire.request_vm_resize(size_gib=40, digest=DIGEST, timeout=10, custody=value.custody) == UPID
    assert [(method, path) for method, path, _ in value.requests] == [
        ("PUT", "/api2/json/nodes/node1/qemu/123/config"),
        ("PUT", "/api2/json/nodes/node1/qemu/123/resize"),
    ]
    config = json.loads(value.requests[0][2])
    assert config["memory"] == "8192" and config["cores"] == 4 and config["vmgenid"] == REQUEST.vmgenid
    assert config["sshkeys"].startswith("ssh-ed25519%20") and config["sshkeys"].endswith("%0A")
    assert json.loads(value.requests[1][2]) == {"disk": "scsi0", "size": "40G", "digest": DIGEST}
    assert all(b"secret-canary" not in body for _, _, body in value.requests)


@pytest.mark.parametrize(
    "effect,data",
    [
        ("configure", False),
        ("configure", 0),
        ("configure", {}),
        ("configure", []),
        ("configure", "UPID:foreign"),
        ("resize", None),
        ("resize", False),
        ("resize", 0),
        ("resize", {}),
        ("resize", []),
        ("resize", ""),
    ],
)
def test_wrong_reply_shape_never_counts_as_completion(
    monkeypatch: pytest.MonkeyPatch, effect: str, data: object
) -> None:
    stub_process(monkeypatch, json.dumps({"data": data}).encode())
    wire = _ProxmoxWire(connection())
    with pytest.raises(_WireFailure):
        if effect == "configure":
            wire.request_vm_configure(REQUEST, digest=DIGEST, timeout=1, custody=LocalDeliveryCustody())
        else:
            wire.request_vm_resize(size_gib=40, digest=DIGEST, timeout=1, custody=LocalDeliveryCustody())


@pytest.mark.parametrize("effect", ["configure", "resize"])
@pytest.mark.parametrize("timeout", [None, True, 0, -1, float("inf")])
def test_invalid_deadline_never_starts_worker(monkeypatch: pytest.MonkeyPatch, effect: str, timeout: Any) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        pytest.fail("worker started before validation")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    wire = _ProxmoxWire(connection())
    with pytest.raises(ValidationError):
        if effect == "configure":
            wire.request_vm_configure(REQUEST, digest=DIGEST, timeout=timeout, custody=LocalDeliveryCustody())
        else:
            wire.request_vm_resize(size_gib=40, digest=DIGEST, timeout=timeout, custody=LocalDeliveryCustody())


@pytest.mark.parametrize(
    "changes",
    [
        {"skiplock": True},
        {"tailscale_auth_key": "synthetic-forbidden"},
        {"digest": "stale"},
        {"vmgenid": "1"},
        {"memory": True},
        {"cores": True},
        {"ciuser": "bad/user"},
        {"sshkeys": "-----BEGIN%20OPENSSH%20PRIVATE%20KEY-----"},
    ],
)
def test_worker_rejects_nonpublic_configuration_before_http(changes: dict[str, object]) -> None:
    payload = worker_payload()
    payload.update(endpoint="vm-configure", method="PUT", suffix=None, timeout=1)
    payload["body"] = json.dumps({**REQUEST.body(DIGEST), **changes})
    with pytest.raises(ValueError):
        _request(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"currentdisk": "invented"},
        {"skiplock": True},
        {"disk": "scsi1"},
        {"size": "+40G"},
        {"size": "0G"},
        {"size": "40.5G"},
        {"digest": None},
    ],
)
def test_worker_resize_has_no_relative_or_provider_body_escape(changes: dict[str, object]) -> None:
    payload = worker_payload()
    payload.update(endpoint="vm-resize", method="PUT", suffix=None, timeout=1)
    payload["body"] = json.dumps({"disk": "scsi0", "size": "40G", "digest": DIGEST, **changes})
    with pytest.raises(ValueError):
        _request(payload)


@pytest.mark.parametrize("endpoint_name", ["vm-configure", "vm-resize"])
@pytest.mark.parametrize("changes", [{"method": "POST"}, {"suffix": "../config"}, {"timeout": None}])
def test_worker_mutations_require_fixed_method_route_and_deadline(
    endpoint_name: str, changes: dict[str, object]
) -> None:
    payload = worker_payload()
    body = (
        REQUEST.body(DIGEST) if endpoint_name == "vm-configure" else {"disk": "scsi0", "size": "40G", "digest": DIGEST}
    )
    payload.update(endpoint=endpoint_name, method="PUT", suffix=None, timeout=1, body=json.dumps(body))
    payload.update(changes)
    with pytest.raises(ValueError):
        _request(payload)


@pytest.mark.windows
def test_redirect_does_not_replay_configuration(configuration_endpoint: ConfigurationEndpoint) -> None:
    value = configuration_endpoint
    value.status = 307
    with pytest.raises(_WireFailure):
        value.wire.request_vm_configure(REQUEST, digest=DIGEST, timeout=10, custody=value.custody)
    assert len(value.requests) == 1
