"""Fixed public clone routes against owned local TLS, without provider acceptance."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from typing import Any, NoReturn

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carriers._proxmox_http import _request
from agentworks.execution.carriers.proxmox import ProxmoxCloneRequest, _ProxmoxWire, _WireFailure
from tests.execution.test_proxmox import connection, stub_process, worker_payload
from tests.execution.test_proxmox_activation_wire import _ActivationEndpoint
from tests.execution.test_proxmox_activation_wire import activation_endpoint as activation_endpoint
from tests.execution.test_proxmox_trust import endpoint as endpoint

REQUEST = ProxmoxCloneRequest(456, "test-vm", "agentworks-instance:" + "a" * 32, "local-lvm", "agentworks")
UPID = "UPID:node1:00000001:100000001:00000001:qmclone:123:user@pve!token:"


@pytest.mark.windows
def test_next_id_is_observation_and_clone_posts_only_public_exact_fields(
    activation_endpoint: _ActivationEndpoint,
) -> None:
    value = activation_endpoint
    value.response = b'{"data":456}'
    assert value.wire.request_next_id(timeout=10, custody=value.custody) == 456
    value.response = json.dumps({"data": UPID}).encode()
    assert value.wire.request_vm_clone(REQUEST, timeout=10, custody=value.custody) == UPID
    assert [(method, path) for method, path, _, _ in value.requests] == [
        ("GET", "/api2/json/cluster/nextid"),
        ("POST", "/api2/json/nodes/node1/qemu/123/clone"),
    ]
    body = json.loads(value.requests[1][2])
    assert body == {
        "newid": 456,
        "name": "test-vm",
        "description": REQUEST.description,
        "storage": "local-lvm",
        "pool": "agentworks",
        "full": 1,
    }
    assert b"secret-canary" not in value.requests[1][2]


@pytest.mark.parametrize("candidate", [None, True, 0, -1, "456", "123/../456", []])
def test_invalid_candidate_response_is_not_a_reservation(monkeypatch: pytest.MonkeyPatch, candidate: object) -> None:
    stub_process(monkeypatch, json.dumps({"data": candidate}).encode())
    with pytest.raises(_WireFailure):
        _ProxmoxWire(connection()).request_next_id(timeout=1, custody=LocalDeliveryCustody())


@pytest.mark.parametrize("timeout", [None, 0, -1, True, float("inf")])
@pytest.mark.parametrize("operation", ["candidate", "clone"])
def test_invalid_deadline_never_starts_worker(monkeypatch: pytest.MonkeyPatch, timeout: Any, operation: str) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        pytest.fail("worker started before validation")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    wire = _ProxmoxWire(connection())
    with pytest.raises(ValidationError):
        if operation == "candidate":
            wire.request_next_id(timeout=timeout, custody=LocalDeliveryCustody())
        else:
            wire.request_vm_clone(REQUEST, timeout=timeout, custody=LocalDeliveryCustody())


def test_clone_destination_equal_to_source_refuses_before_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        pytest.fail("worker started before validation")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    request = ProxmoxCloneRequest(123, "test", REQUEST.description, "local-lvm", "agentworks")
    with pytest.raises(ValidationError):
        _ProxmoxWire(connection()).request_vm_clone(request, timeout=1, custody=LocalDeliveryCustody())


@pytest.mark.parametrize(
    "changes",
    [
        {"newid": True},
        {"newid": 123},
        {"full": True},
        {"full": 0},
        {"description": "unassociated"},
        {"storage": "../other"},
        {"tailscale_auth_key": "synthetic-forbidden"},
    ],
)
def test_worker_rejects_nonpublic_or_ambiguous_clone_body_before_http(changes: dict[str, object]) -> None:
    payload = worker_payload()
    payload.update(endpoint="vm-clone", method="POST", suffix=None, timeout=1)
    payload["body"] = json.dumps({**asdict(REQUEST), **changes})
    with pytest.raises(ValueError):
        _request(payload)


@pytest.mark.parametrize("change", [{"method": "GET"}, {"suffix": "../clone"}, {"timeout": None}])
def test_worker_clone_route_has_no_general_http_escape(change: dict[str, object]) -> None:
    payload = worker_payload()
    payload.update(endpoint="vm-clone", method="POST", suffix=None, timeout=1, body=json.dumps(asdict(REQUEST)))
    payload.update(change)
    with pytest.raises(ValueError):
        _request(payload)
