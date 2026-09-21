"""Request-boundary checks for one-shot private file reads."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_read import read_file
from agentworks.execution._file_read_bundle import FIXED_BUNDLE
from agentworks.execution._file_read_protocol import (
    FileReadFailure,
    FileReadRequestError,
    decode_file_read_request,
    encode_file_read_request,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
    Retention,
)


@dataclass
class CaptureCarrier:
    calls: int = 0
    io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, deadline
        self.calls += 1
        self.io = io
        delivered = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=0), 0, delivered, delivered)


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)


def _request_manifest(remaining_seconds: object) -> bytes:
    return json.dumps(
        {
            "identity": {"egid": 2, "euid": 1, "groups": [2]},
            "max_bytes": 1,
            "nonce": "0123456789abcdef0123456789abcdef",
            "operation": "read",
            "path": base64.b64encode(b"file").decode("ascii"),
            "remaining_seconds": remaining_seconds,
            "root": base64.b64encode(b"/root").decode("ascii"),
            "version": 1,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


@pytest.mark.parametrize("remaining", [True, 0, -1, float("nan"), float("inf"), -float("inf"), 10**4000])
def test_remaining_seconds_rejects_noncanonical_or_nonfinite_values_without_overflow(remaining: object) -> None:
    with pytest.raises(FileReadRequestError) as raised:
        decode_file_read_request(_request_manifest(remaining))

    assert raised.value.failure is FileReadFailure.INVALID_REQUEST


@pytest.mark.parametrize("remaining", [None, 0.0, 1.5])
def test_remaining_seconds_accepts_only_explicit_unbounded_or_finite_nonnegative_values(remaining: object) -> None:
    request = decode_file_read_request(_request_manifest(remaining))

    if remaining is None:
        assert request.remaining_seconds is None
    else:
        assert request.remaining_seconds == float(remaining)


def test_host_computes_remaining_immediately_before_its_only_attempt(
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    carrier = CaptureCarrier()

    def remaining(_deadline: Deadline) -> float:
        events.append("remaining")
        return 4.25

    def encode(request):
        events.append("encode")
        assert request.remaining_seconds == 4.25
        return encode_file_read_request(request)

    monkeypatch.setattr(Deadline, "remaining", remaining)
    monkeypatch.setattr("agentworks.execution._file_read.encode_file_read_request", encode)

    read_file(
        carrier,
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=1,
        plan=plan,
        deadline=Deadline(None),
    )

    assert events == ["remaining", "encode"]
    assert carrier.calls == 1
    assert carrier.io is not None
    data = carrier.io.input.data  # type: ignore[union-attr]
    assert data.startswith(FIXED_BUNDLE.prefix)
    assert decode_file_read_request(data[len(FIXED_BUNDLE.prefix) :]).remaining_seconds == 4.25


def test_validation_precedes_the_single_carrier_attempt(plan: IdentityPlan) -> None:
    carrier = CaptureCarrier()

    with pytest.raises(ValidationError):
        read_file(
            carrier,
            trusted_root_path="relative",
            relative_path="leaf",
            max_bytes=1,
            plan=plan,
            deadline=Deadline.after(1),
        )

    assert carrier.calls == 0


def test_invalid_request_exception_does_not_retain_raw_manifest_fields() -> None:
    value = json.loads(_request_manifest(None))
    value["root"] = base64.b64encode(b"/secret-\xff-canary").decode("ascii")
    manifest = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")

    with pytest.raises(FileReadRequestError) as raised:
        decode_file_read_request(manifest)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "canary" not in repr(raised.value)
