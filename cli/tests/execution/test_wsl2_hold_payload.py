"""Canonical persisted v3/v4 wire records preserve launch and body accounts."""

from __future__ import annotations

import json

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._wsl2_platform_hold import decode_hold_payload, encode_hold_payload


def _fields(version: int) -> dict[str, object]:
    fields: dict[str, object] = {
        "controller_creation_ticks": 123456789,
        "controller_pid": 42,
        "distribution": "Ubuntu",
        "instance_marker": "c" * 32,
        "locator_sha256": "d" * 64,
        "nonce": "a" * 32,
        "query_may_have_been_admitted": False,
        "user": "configured-agent",
        "version": version,
    }
    if version == 4:
        fields["launch_user"] = "root"
    return fields


def _wire(fields: dict[str, object]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("ascii")


@pytest.mark.parametrize("version", [3, 4])
def test_canonical_roundtrip_preserves_exact_persisted_version(version: int) -> None:
    fields = _fields(version)
    encoded = _wire(fields)
    payload = decode_hold_payload(encoded)
    assert encode_hold_payload(payload) == encoded
    assert payload.version == version and payload.user == "configured-agent"
    assert payload.launch_user == ("root" if version == 4 else None)


@pytest.mark.parametrize("version,launch", [(3, "root"), (4, None), (4, "agent"), (4, True)])
def test_launch_account_and_version_cannot_disagree(version: int, launch: object) -> None:
    fields = _fields(version)
    fields["launch_user"] = launch
    with pytest.raises(ValidationError):
        decode_hold_payload(_wire(fields))


def test_v4_missing_launch_account_refuses() -> None:
    fields = _fields(4)
    del fields["launch_user"]
    with pytest.raises(ValidationError):
        decode_hold_payload(_wire(fields))
