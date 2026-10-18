"""Closed activation controls never accept source, paths, or recovered authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._scratch import ReadyScratchReference, ScratchReference
from agentworks.execution._scratch_receipt import (
    ScratchHistoricalOwnership,
    ScratchOperation,
    ScratchOwnership,
    ScratchReceiptContext,
    _Identity,
)
from agentworks.execution._staged_helper_protocol import (
    MAX_ENVELOPE_BYTES,
    MAX_REMAINING_NS,
    StagedHelperError,
    StagedHelperRequest,
    decode_request,
    encode_request,
)

NONCE = "b" * 32
TOKEN = bytes(range(16))
IDENTITY = IdentityExpectation(0, 0, (0,))


def _request() -> StagedHelperRequest:
    context = ScratchReceiptContext(ScratchOperation.STAGE, IDENTITY)
    reference = ScratchReference(
        ScratchOwnership(TOKEN, context, _Identity(1, 2), _Identity(1, 3), _Identity(1, 4), 0, 100000, _Identity(1, 5))
    )
    return StagedHelperRequest(NONCE, IDENTITY, TOKEN, reference, hashlib.sha256(b"prepared").digest(), 10_000_000_000)


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def test_canonical_roundtrip_carries_only_control_and_active_ownership() -> None:
    request = _request()
    encoded = encode_request(request)
    assert decode_request(encoded) == request
    assert len(encoded) < MAX_ENVELOPE_BYTES
    assert NONCE not in repr(request) and TOKEN.hex() not in repr(request)
    assert set(json.loads(encoded)) == {"version", "nonce", "identity", "token", "reference", "sha256", "remaining_ns"}


@pytest.mark.parametrize("budget", [None, False, True, 0, -1, 1.0, "1", MAX_REMAINING_NS + 1])
def test_required_positive_signed64_budget_at_constructor_and_wire(budget: object) -> None:
    request = _request()
    with pytest.raises(StagedHelperError):
        replace(request, remaining_ns=budget)
    value = json.loads(encode_request(request))
    value["remaining_ns"] = budget
    with pytest.raises(StagedHelperError):
        decode_request(_json(value))


@pytest.mark.parametrize("budget", [1, MAX_REMAINING_NS])
def test_budget_boundaries(budget: int) -> None:
    request = replace(_request(), remaining_ns=budget)
    assert decode_request(encode_request(request)) == request


@pytest.mark.parametrize("field", ["version", "nonce", "identity", "token", "reference", "sha256", "remaining_ns"])
def test_missing_fields_refuse(field: str) -> None:
    value = json.loads(encode_request(_request()))
    del value[field]
    with pytest.raises(StagedHelperError):
        decode_request(_json(value))


@pytest.mark.parametrize("field", ["path", "bootstrap", "module", "guest", "ready", "cleanup"])
def test_extra_authority_fields_refuse(field: str) -> None:
    value = json.loads(encode_request(_request()))
    value[field] = "unexpected"
    with pytest.raises(StagedHelperError):
        decode_request(_json(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("nonce", "B" * 32),
        ("nonce", "b" * 31),
        ("token", "A" * 32),
        ("token", "01"),
        ("sha256", "A" * 64),
        ("sha256", "f" * 62),
        ("identity", {"euid": True, "egid": 0, "groups": [0]}),
        ("identity", {"euid": 0, "egid": 0, "groups": [0, 0]}),
        ("reference", {"parent": {"device": 1, "inode": 2}}),
    ],
)
def test_malformed_fields_refuse(field: str, value: object) -> None:
    request = json.loads(encode_request(_request()))
    request[field] = value
    with pytest.raises(StagedHelperError):
        decode_request(_json(request))


@pytest.mark.parametrize("variant", ["duplicate", "spacing", "truncated", "binary", "oversized"])
def test_noncanonical_or_oversized_envelope_refuses(variant: str) -> None:
    data = encode_request(_request())
    if variant == "duplicate":
        data = data.replace(b'"version":1', b'"version":1,"version":1')
    elif variant == "spacing":
        data = data.replace(b'"version":1', b'"version": 1')
    elif variant == "truncated":
        data = data[:-1]
    elif variant == "binary":
        data = b"\xff"
    else:
        data = b" " * (MAX_ENVELOPE_BYTES + 1)
    with pytest.raises(StagedHelperError):
        decode_request(data)


@pytest.mark.parametrize("variant", ["token", "identity", "snapshot", "ready", "historical", "reference-number"])
def test_constructor_rejects_mismatched_context_and_nonactive_authority(variant: str) -> None:
    request = _request()
    ownership = request.reference._ownership
    change: dict[str, object]
    if variant == "token":
        change = {"token": b"x" * 16}
    elif variant == "identity":
        change = {"identity": IdentityExpectation(1, 1, (1,))}
    elif variant == "snapshot":
        change = {
            "reference": ScratchReference(
                replace(ownership, _context=ScratchReceiptContext(ScratchOperation.SNAPSHOT, IDENTITY))
            )
        }
    elif variant == "ready":
        change = {"reference": ReadyScratchReference(request.reference, request.sha256, 1, 1)}
    elif variant == "historical":
        change = {"reference": ScratchHistoricalOwnership(ownership)}
    else:
        change = {"reference": ScratchReference(replace(ownership, _parent=_Identity(1, -1)))}
    with pytest.raises(StagedHelperError):
        replace(request, **change)
