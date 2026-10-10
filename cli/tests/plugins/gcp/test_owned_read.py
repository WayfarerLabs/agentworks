"""Exact GCE identity, passive construction and original retirement behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import VMStatus
from agentworks.errors import LimitExceededError, StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.gcp._native_access import MAX_BODY_BYTES, GCEOwnedRead
from agentworks.plugins.gcp.config import GcpAmbientAuth


def make_reader(*, incarnation: str = "123", auth: Any = None, secret: Any = None) -> tuple[GCEOwnedRead, Any]:
    from agentworks.plugins.gcp.platform import GCEPlatform

    vm = SimpleNamespace(
        name="selected-vm",
        platform_metadata={
            "project_id": "project",
            "zone": "us-central1-a",
            "instance_name": "backend",
            "instance_id": incarnation,
        },
    )
    platform = SimpleNamespace(
        site_name="gcp-site",
        config=SimpleNamespace(auth=auth or GcpAmbientAuth(mode="ambient")),
        _locator_metadata=GCEPlatform._locator_metadata,
    )
    context = SimpleNamespace(secret=secret or (lambda name: pytest.fail("ambient auth requested a secret")))
    return GCEOwnedRead(vm, platform, context), vm


def instance(**changes: Any) -> dict[str, Any]:
    zone = "https://compute.googleapis.com/compute/v1/projects/project/zones/us-central1-a"
    return {
        "kind": "compute#instance",
        "id": "123",
        "name": "backend",
        "zone": zone,
        "selfLink": f"{zone}/instances/backend",
        "status": "RUNNING",
        **changes,
    }


@pytest.fixture
def reader() -> GCEOwnedRead:
    selected, _ = make_reader()
    return selected


def project(reader: GCEOwnedRead, value: Any) -> tuple[ProviderLocator, VMStatus]:
    return reader._project(json.dumps(value).encode())


def test_constructor_is_passive_and_snapshots_recorded_identity(monkeypatch):
    monkeypatch.setattr("requests.Session", lambda: pytest.fail("session constructed in passive constructor"))
    monkeypatch.setattr("google.auth.default", lambda **kwargs: pytest.fail("ADC in passive constructor"))
    selected, vm = make_reader()
    vm.platform_metadata["instance_name"] = "replacement"
    assert selected._url().endswith("/instances/backend")
    assert not selected.cleanup_incomplete
    assert selected.close(Deadline.after(5))


@pytest.mark.parametrize("incarnation", ["0", "01", "-1", "+1", "1.0", " 1", "18446744073709551616", "9" * 200])
def test_constructor_refuses_noncanonical_or_overflow_identity(incarnation):
    with pytest.raises(StateError):
        make_reader(incarnation=incarnation)


def test_uint64_upper_boundary():
    selected, _ = make_reader(incarnation="18446744073709551615")
    locator, _ = project(selected, instance(id="18446744073709551615"))
    assert locator.token == "gcp-gce:project:us-central1-a:18446744073709551615"


@pytest.mark.parametrize(
    "status, expected",
    [
        ("RUNNING", VMStatus.RUNNING),
        ("STOPPED", VMStatus.STOPPED),
        ("TERMINATED", VMStatus.STOPPED),
        ("STOPPING", VMStatus.UNKNOWN),
        ("PROVISIONING", VMStatus.UNKNOWN),
        (None, VMStatus.UNKNOWN),
        ([], VMStatus.UNKNOWN),
        (42, VMStatus.UNKNOWN),
    ],
)
def test_power_mapping(reader, status, expected):
    locator, power = project(reader, instance(status=status))
    assert locator.token == "gcp-gce:project:us-central1-a:123"
    assert power is expected


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "compute#operation"},
        {"name": "replacement"},
        {"id": "124"},
        {"id": 123},
        {"id": True},
        {"id": "0123"},
        {"id": "18446744073709551616"},
        {"zone": "https://foreign.invalid/compute/v1/projects/project/zones/us-central1-a"},
        {"zone": "https://compute.googleapis.com/compute/v1/projects/other/zones/us-central1-a"},
        {"selfLink": "https://compute.googleapis.com/compute/v1/projects/project/zones/us-central1-a/instances/other"},
        {"selfLink": None},
    ],
)
def test_refuse_foreign_or_malformed_identity(reader, change):
    with pytest.raises(ValidationError):
        project(reader, instance(**change))


@pytest.mark.parametrize("key", ["kind", "name", "id", "zone", "selfLink"])
def test_missing_identity_fields_refused(reader, key):
    value = instance()
    value.pop(key)
    with pytest.raises(ValidationError):
        project(reader, value)


def test_supported_public_selflink_spelling(reader):
    value = instance()
    for key in ("zone", "selfLink"):
        value[key] = value[key].replace("compute.googleapis.com", "www.googleapis.com")
    assert project(reader, value)[1] is VMStatus.RUNNING


@pytest.mark.parametrize("body", [b"[]", b"null", b"{}", b'{"id":"123","id":"123"}', b'{"status":NaN}', b"\xff"])
def test_strict_json_boundary(reader, body):
    with pytest.raises(ValidationError):
        reader._project(body)


@pytest.mark.parametrize("deadline,error", [(Deadline(None), ValidationError), (Deadline(0), LimitExceededError)])
def test_deadline_refusal_before_acquisition(reader, monkeypatch, deadline, error):
    monkeypatch.setattr("requests.Session", lambda: pytest.fail("session after expired admission"))
    with pytest.raises(error):
        reader.observe_power(deadline)
    assert not reader.cleanup_incomplete


def test_raw_ingestion_bound(reader):
    amounts = []

    class Raw:
        def read(self, amount, *, decode_content):
            assert decode_content is False
            amounts.append(amount)
            return b"x" * amount

        def isclosed(self):
            return False

    reader._response = SimpleNamespace(raw=Raw())
    with pytest.raises(ValidationError):
        reader._read_body(Deadline.after(5))
    assert sum(amounts) == MAX_BODY_BYTES + 1
    assert all(0 < amount <= 8192 for amount in amounts)


@pytest.mark.parametrize("chunk", ["text", bytearray(b"x"), b"x" * 8193])
def test_invalid_raw_read(reader, chunk):
    reader._response = SimpleNamespace(raw=SimpleNamespace(read=lambda *args, **kwargs: chunk))
    with pytest.raises(ValidationError):
        reader._read_body(Deadline.after(5))


def test_explicit_close_retries_only_same_originals(reader):
    calls = []

    class Original:
        def close(self):
            calls.append(self)
            if len(calls) == 1:
                raise RuntimeError("ordinary close failed")

    original = Original()
    reader._response = original
    assert not reader.close(Deadline.after(5))
    assert reader.cleanup_incomplete
    assert reader.close(Deadline.after(5))
    assert calls == [original, original]
    with pytest.raises(StateError):
        reader.observe_locator(Deadline.after(5))


def test_active_auth_session_is_dependency_until_final_close(reader):
    original = SimpleNamespace(close=lambda: None)
    reader._auth._session = original
    assert not reader.cleanup_incomplete
    assert reader.close(Deadline.after(5))
    assert not reader.cleanup_incomplete


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_cleanup_control_retains_original_and_attempts_independent_close(reader, control_type):
    control = control_type("original-control")
    calls = []

    def interrupt():
        calls.append("response")
        raise control

    response = SimpleNamespace(close=interrupt)
    reader._response = response
    reader._service_session = SimpleNamespace(close=lambda: calls.append("service"))
    reader._auth._session = SimpleNamespace(close=lambda: calls.append("credential"))
    with pytest.raises(control_type) as caught:
        reader.close(Deadline.after(5))
    assert caught.value is control
    assert calls == ["response", "service", "credential"]
    assert reader._response is response
    assert reader._lock.acquire(blocking=False)
    reader._lock.release()
