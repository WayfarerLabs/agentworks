"""Final GCE instance request and operation-ownership boundaries."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import cast

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.errors import AlreadyExistsError
from agentworks.plugins.gcp.errors import GCEOperationError
from agentworks.plugins.gcp.instance import (
    InstanceInsertAttempt,
    build_instance_resource,
    insert_instance_reconciled,
)
from agentworks.plugins.gcp.network import NetworkSelection

_REQUEST_ID = "d1486754-0828-49ff-b788-66938c44a5ea"
_NETWORK = "projects/project-a/global/networks/default"


def _api_error(kind: type[Exception], message: str) -> Exception:
    return cast("Callable[[str], Exception]", kind)(message)


class _Operation:
    error_code = None

    def __init__(
        self,
        *,
        request_id: str = _REQUEST_ID,
        target_id: int = 201,
        target_link: str = "projects/project-a/zones/us-central1-a/instances/vm-a",
        operation_type: str = "insert",
        failure: BaseException | None = None,
    ) -> None:
        self.client_operation_id = request_id
        self.target_id = target_id
        self.target_link = target_link
        self.operation_type = operation_type
        self.failure = failure
        self.waits: list[float] = []

    def result(self, *, timeout: float) -> None:
        self.waits.append(timeout)
        if self.failure is not None:
            raise self.failure


class _Instances:
    def __init__(
        self,
        states: Iterator[compute_v1.Instance | None],
        *,
        results: Iterator[_Operation | Exception] | None = None,
        operation: _Operation | None = None,
    ) -> None:
        self.states = states
        self.results = results
        self.operation = operation or _Operation()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def insert(self, **kwargs: object) -> _Operation:
        self.calls.append(("insert", kwargs))
        result = next(self.results) if self.results is not None else self.operation
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, **kwargs: object) -> compute_v1.Instance:
        self.calls.append(("get", kwargs))
        state = next(self.states)
        if state is None:
            raise _api_error(api_exceptions.NotFound, "gone")
        return state


def _resource() -> compute_v1.Instance:
    return build_instance_resource(
        instance_name="vm-a",
        machine_type_url="projects/project-a/zones/us-central1-a/machineTypes/e2-standard-2",
        image_url="projects/debian-cloud/global/images/debian-12-v1",
        disk_type_url="projects/project-a/zones/us-central1-a/diskTypes/pd-balanced",
        disk_gib=40,
        network=NetworkSelection("us-central1", _NETWORK, None),
        network_tag="vm-a-tag",
        admin_username="admin",
        ssh_public_key="ssh-ed25519 PUBLIC",
        startup_script="#!/bin/bash\necho ready\n",
    )


def test_final_instance_body_is_key_free_and_pins_security_fields() -> None:
    resource = _resource()
    body = compute_v1.Instance.to_dict(resource)
    assert body["service_accounts"] == []
    assert body["disks"][0]["boot"] is True
    assert body["disks"][0]["auto_delete"] is True
    assert body["disks"][0]["initialize_params"]["disk_size_gb"] == "40"
    assert body["disks"][0]["initialize_params"]["disk_type"].endswith("/pd-balanced")
    interface = body["network_interfaces"][0]
    assert interface["stack_type"] == "IPV4_ONLY"
    assert interface["network"] == _NETWORK
    assert interface["access_configs"] == [
        {"name": "External NAT", "type_": "ONE_TO_ONE_NAT", "network_tier": "PREMIUM"}
    ]
    metadata = {item["key"]: item["value"] for item in body["metadata"]["items"]}
    assert metadata["block-project-ssh-keys"] == "TRUE"
    assert metadata["enable-oslogin"] == "FALSE"
    assert metadata["ssh-keys"] == "admin:ssh-ed25519 PUBLIC"
    assert "TAILSCALE_HOSTILE_'\"$()" not in repr(body)
    assert "SERVICE_ACCOUNT_HOSTILE_'\"$()" not in repr(body)


def test_insert_retains_typed_request_and_ownership_before_wait() -> None:
    realized = _resource()
    realized.id = 201
    operation = _Operation(failure=KeyboardInterrupt("stop"))
    client = _Instances(iter([]), operation=operation)
    attempt = InstanceInsertAttempt("vm-a", _REQUEST_ID)
    with pytest.raises(KeyboardInterrupt, match="stop"):
        insert_instance_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            instance=realized,
            attempt=attempt,
            timeout=17,
        )
    assert attempt.ownership is not None
    assert attempt.ownership.resource_id == "201"
    request = client.calls[0][1]["request"]
    assert isinstance(request, compute_v1.InsertInstanceRequest)
    assert request.request_id == _REQUEST_ID
    assert request.instance_resource == realized


def test_indeterminate_wait_reconciles_only_matching_provider_id() -> None:
    expected = _resource()
    realized = _resource()
    realized.id = 201
    attempt = InstanceInsertAttempt("vm-a", _REQUEST_ID)
    result, ownership = insert_instance_reconciled(
        _Instances(iter([realized]), operation=_Operation(failure=TimeoutError("provider detail"))),
        project_id="project-a",
        zone="us-central1-a",
        instance=expected,
        attempt=attempt,
        timeout=17,
    )
    assert result is realized
    assert ownership.resource_id == "201"

    collision = _resource()
    collision.id = 999
    with pytest.raises(AlreadyExistsError, match="different provider identity"):
        insert_instance_reconciled(
            _Instances(iter([collision]), operation=_Operation(failure=TimeoutError("provider detail"))),
            project_id="project-a",
            zone="us-central1-a",
            instance=expected,
            attempt=InstanceInsertAttempt("vm-a", _REQUEST_ID),
            timeout=17,
        )


@pytest.mark.parametrize(
    "operation",
    [
        _Operation(request_id="wrong"),
        _Operation(target_id=0),
        _Operation(target_link="projects/project-a/zones/us-central1-a/instances/other"),
        _Operation(operation_type="start"),
    ],
)
def test_incomplete_operation_identity_is_never_owned(operation: _Operation) -> None:
    attempt = InstanceInsertAttempt("vm-a", _REQUEST_ID)
    with pytest.raises(GCEOperationError, match="incomplete ownership identity"):
        insert_instance_reconciled(
            _Instances(iter([]), operation=operation),
            project_id="project-a",
            zone="us-central1-a",
            instance=_resource(),
            attempt=attempt,
            timeout=17,
        )
    assert attempt.ownership is None


def test_definite_already_exists_never_shape_reconciles() -> None:
    client = _Instances(
        iter([]),
        results=iter([_api_error(api_exceptions.AlreadyExists, "same shape provider reflection")]),
    )
    with pytest.raises(AlreadyExistsError):
        insert_instance_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            instance=_resource(),
            attempt=InstanceInsertAttempt("vm-a", _REQUEST_ID),
            timeout=17,
        )
    assert [name for name, _kwargs in client.calls] == ["insert"]
