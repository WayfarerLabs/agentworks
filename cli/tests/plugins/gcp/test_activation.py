"""GCE identity/recovery and fenced one-shot admission boundaries."""

from __future__ import annotations

import json
import threading
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp._activation import (
    ActivationPayload,
    GCEActivation,
    decode_acknowledgment,
    decode_activation_payload,
    encode_activation_payload,
    start_url,
)

PAYLOAD = ActivationPayload("project", "us-central1-a", "backend", "123", "12345678-1234-4234-8234-123456789abc")


def operation(payload: ActivationPayload = PAYLOAD) -> dict[str, Any]:
    return {
        "kind": "compute#operation",
        "name": "Operation_1",
        "clientOperationId": payload.request_id,
        "targetId": payload.instance_id,
        "targetLink": f"https://compute.googleapis.com/compute/v1/projects/{payload.project_id}/zones/{payload.zone}"
        f"/instances/{payload.instance_name}",
        "operationType": "start",
    }


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")


@pytest.fixture
def owned(tmp_path, monkeypatch):
    import requests  # type: ignore[import-untyped]
    from google.auth.transport import requests as auth

    handles: list[Any] = []
    sends: list[str] = []
    closes: list[str] = []

    class Session:
        def __init__(self, kind) -> None:
            self.kind = kind
            self.closed = False
            handles.append(self)

        def close(self):
            closes.append(self.kind)
            self.closed = True

    class Raw:
        def __init__(self) -> None:
            self.data = b""

        def read(self, amount, decode_content):
            assert 0 < amount <= 8192 and decode_content is False
            data, self.data = self.data[:amount], self.data[amount:]
            return data

        def isclosed(self):
            return not self.data

    class Service(Session):
        def request(self, method, url, **kwargs):
            assert method == "POST" and url == start_url(adapter.payload)
            assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
            row = owner.inspect_lifecycle_obligation(adapter.obligation_id)
            assert row is not None
            assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
            assert decode_activation_payload(row.payload).request_id == adapter.payload.request_id
            sends.append(url)
            response: Any = Session("response")
            response.status_code, response.headers, response.raw = 200, {}, Raw()
            response.raw.data = json.dumps(operation(adapter.payload)).encode()
            kwargs["hooks"]["response"](response)
            return response

    monkeypatch.setattr(requests, "Session", lambda: Session("credential"))
    monkeypatch.setattr(auth, "AuthorizedSession", lambda *args, **kwargs: Service("service"))
    with closing(Database(tmp_path / "state.db")) as database:
        owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "database-vm"), "test"
        )
        adapter = GCEActivation(
            owner,
            "database-vm",
            object(),
            "project",
            "us-central1-a",
            "backend",
            "123",
            ProviderLocator("gcp-gce:project:us-central1-a:123"),
        )
        yield SimpleNamespace(
            adapter=adapter,
            owner=owner,
            database=database,
            handles=handles,
            sends=sends,
            closes=closes,
            service=Service,
        )


def row(probe: Any) -> Any:
    result = probe.owner.inspect_lifecycle_obligation(probe.adapter.obligation_id)
    assert result is not None
    return result


def test_passive_one_post_keeps_possible_effect_and_owner(owned):
    p = owned
    assert not p.handles and not p.sends and not p.adapter.cleanup_incomplete
    assert UUID(p.adapter.payload.request_id).int != 0
    assert p.adapter.start(Deadline.after(5)) == "Operation_1"
    assert p.closes == ["response", "service", "credential"]
    assert row(p).state is LifecycleObligationState.POSSIBLE_EFFECT and row(p).payload_revision == 1
    p.adapter.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        p.adapter.start(Deadline.after(5))
    assert len(p.sends) == 1 and not p.adapter.cleanup_incomplete
    assert p.database.operations.inspect(p.owner.ownership.scope) is not None


@pytest.mark.parametrize("stage", ["register", "mark", "publish"])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_lost_ledger_reply_never_replays(owned, monkeypatch, stage, committed, error_type):
    p, primary = owned, error_type("lost ledger reply")
    target, name = {
        "register": (OperationOwner, "register_lifecycle_obligation"),
        "mark": (LifecycleObligation, "mark_possible_effect"),
        "publish": (LifecycleObligation, "publish_payload"),
    }[stage]
    original = getattr(target, name)

    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(target, name, fail)
    with pytest.raises(error_type) as caught:
        p.adapter.start(Deadline.after(5))
    assert caught.value is primary and len(p.sends) == (stage == "publish")
    monkeypatch.setattr(target, name, original)
    if stage == "register" and not committed:
        with pytest.raises(StateError):
            p.adapter.reconcile(Deadline.after(5))
    else:
        p.adapter.reconcile(Deadline.after(5))
        assert row(p).state is (
            LifecycleObligationState.RESOLVED
            if stage == "register"
            else LifecycleObligationState.REGISTERED
            if stage == "mark" and not committed
            else LifecycleObligationState.POSSIBLE_EFFECT
        )
    assert not p.adapter._lock.locked()
    with pytest.raises(StateError):
        p.adapter.start(Deadline.after(5))


@pytest.mark.parametrize("stage", ["mark", "publish"])
def test_fence_takeover_refuses_effect_or_publication(owned, monkeypatch, stage):
    p = owned
    target, name = (
        (LifecycleObligation, "mark_possible_effect")
        if stage == "mark"
        else (OperationOwner, "inspect_lifecycle_obligation")
    )
    original = getattr(target, name)
    successors: list[OperationOwner] = []

    def takeover(*args, **kwargs):
        if not successors and (stage == "mark" or p.adapter.payload.operation_name is not None):
            successors.append(OperationOwner.recover(p.database.operations, p.owner.ownership, "a" * 32))
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, takeover)
    with pytest.raises(StateError):
        p.adapter.start(Deadline.after(5))
    assert len(p.sends) == (stage == "publish")
    assert (p.adapter.payload.operation_name is not None) is (stage == "publish")
    with pytest.raises(StateError):
        p.adapter.reconcile(Deadline.after(5))


@pytest.mark.parametrize(
    "field,value",
    [
        ("obligation_kind", "foreign"),
        ("payload_version", 2),
        ("payload_revision", 2),
        ("payload", encode_activation_payload(replace(PAYLOAD, instance_id="456"))),
        ("state", LifecycleObligationState.RESOLVED),
    ],
)
def test_foreign_or_premature_receipt_refused(owned, monkeypatch, field, value):
    p = owned
    p.adapter.start(Deadline.after(5))
    altered = replace(row(p), **{field: value})
    monkeypatch.setattr(OperationOwner, "inspect_lifecycle_obligation", lambda *args: altered)
    with pytest.raises((StateError, ValidationError)):
        p.adapter.reconcile(Deadline.after(5))
    assert len(p.sends) == 1


def test_bad_deadline_and_stopped_admission_create_no_handles(owned):
    for deadline in [Deadline.after(None), Deadline.after(0)]:
        with pytest.raises((ValidationError, TimeoutError)):
            owned.adapter.start(deadline)
    owned.owner.stop_admission()
    with pytest.raises(StateError):
        owned.adapter.start(Deadline.after(5))
    assert not owned.handles and not owned.sends


def test_concurrent_admission_and_lock_expiry_send_once(owned, monkeypatch):
    p = owned
    entered, release = threading.Event(), threading.Event()
    original = p.service.request
    observed = []

    def wait(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def first():
        try:
            observed.append(p.adapter.start(Deadline.after(5)))
        except BaseException as error:
            observed.append(error)

    monkeypatch.setattr(p.service, "request", wait)
    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(TimeoutError):
            p.adapter.start(Deadline.after(0.01))
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and observed == ["Operation_1"] and len(p.sends) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "../project"),
        ("project_id", "a" * 64),
        ("zone", "UPPER"),
        ("instance_name", "vm/name"),
        ("instance_id", "0"),
        ("instance_id", "01"),
        ("instance_id", str(2**64)),
        ("instance_id", True),
        ("request_id", "00000000-0000-0000-0000-000000000000"),
        ("request_id", "bad"),
        ("request_id", PAYLOAD.request_id.upper()),
        ("operation_name", "."),
        ("operation_name", "a" * 513),
        ("operation_name", "op/name"),
        ("operation_name", "op%2fname"),
        ("operation_name", "op\x00"),
    ],
)
def test_persisted_identity_boundary(field, value):
    with pytest.raises(ValidationError):
        encode_activation_payload(replace(PAYLOAD, **{field: value}))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"{}",
        b"[]",
        b"null",
        b"\xff",
        b" " * 8193,
        b'{"project_id":"a","project_id":"b"}',
        encode_activation_payload(PAYLOAD) + b"\n",
    ],
)
def test_corrupt_recovery_boundary(data):
    with pytest.raises(ValidationError):
        decode_activation_payload(data)


def test_canonical_recovery_and_uint64_edges():
    for instance in ["1", str(2**64 - 1)]:
        selected = replace(PAYLOAD, instance_id=instance, operation_name="A_-" * 170 + "AB")
        assert decode_activation_payload(encode_activation_payload(selected)) == selected


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "foreign"),
        ("name", ""),
        ("name", "."),
        ("name", "a" * 513),
        ("name", "a/b"),
        ("clientOperationId", "foreign"),
        ("targetId", "0123"),
        ("targetId", 123),
        ("targetId", "456"),
        ("operationType", None),
        ("operationType", "START"),
        ("targetLink", "https://foreign.invalid/compute/v1/projects/project/zones/us-central1-a/instances/backend"),
        ("targetLink", operation()["targetLink"] + "?token=secret"),
        ("targetLink", operation()["targetLink"] + "#fragment"),
        ("targetLink", operation()["targetLink"].replace(".com/", ".com:443/")),
        ("targetLink", operation()["targetLink"].replace("https://", "https://user@")),
        ("targetLink", operation()["targetLink"].replace("/backend", "/%62ackend")),
    ],
)
def test_foreign_operation_rejected(field, value):
    body = operation()
    body[field] = value
    with pytest.raises(ValidationError):
        decode_acknowledgment(json.dumps(body).encode(), PAYLOAD)


@pytest.mark.parametrize("body", [b"\xff", b"[]", b"null", b"{", b" " * 65537, b'{"name":"a","name":"b"}', b"[" * 1000])
def test_body_parse_boundary(body):
    with pytest.raises(ValidationError):
        decode_acknowledgment(body, PAYLOAD)


@pytest.mark.parametrize("host", ["compute.googleapis.com", "www.googleapis.com"])
def test_ack_selects_only_identity_without_startup_interpretation(host):
    body = operation()
    del body["operationType"]
    body.update(
        targetLink=body["targetLink"].replace("compute.googleapis.com", host),
        status="DONE",
        error={"message": "discarded diagnostic"},
        progress=100,
    )
    assert decode_acknowledgment(json.dumps(body).encode(), PAYLOAD) == "Operation_1"


@pytest.mark.parametrize(
    "kind,name,locator",
    [
        (OperationResourceKind.VM, "other", "gcp-gce:project:us-central1-a:123"),
        (OperationResourceKind.PLATFORM_HOST, "database-vm", "gcp-gce:project:us-central1-a:123"),
        (OperationResourceKind.VM, "database-vm", "gcp-gce:project:us-central1-a:456"),
    ],
)
def test_owner_and_locator_boundary(tmp_path, kind, name, locator):
    with closing(Database(tmp_path / "boundary.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(kind, name), "test")
        with pytest.raises(ValidationError):
            GCEActivation(
                owner, "database-vm", object(), "project", "us-central1-a", "backend", "123", ProviderLocator(locator)
            )
