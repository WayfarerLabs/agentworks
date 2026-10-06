"""Closed Python 3.11 operation-lease records on the guest boot clock."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import cast

from . import _managed_job_wire as wire

WINDOW_NS = 60_000_000_000
MAX_CLOCK_NS = 2**63 - 1  # Fixed signed-64-bit wire bound, not a claim about native clock width.
MAX_LEASE_BYTES = 512


class LeaseError(ValueError):
    """Invalid or unavailable operation-lease control."""


@dataclass(frozen=True, slots=True)
class OperationLease:
    run_id: str
    receipt_sha256: str
    boot_id: str
    sampled_ns: int
    expires_ns: int


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def checked_clock(value: object) -> int:
    """Validate external nanosecond samples within the bounded wire range."""
    if type(value) is not int or not 0 <= value <= MAX_CLOCK_NS:
        raise LeaseError("invalid guest clock")
    return value


def boottime_ns() -> int:
    try:
        return checked_clock(time.clock_gettime_ns(time.CLOCK_BOOTTIME))
    except (AttributeError, OSError, OverflowError):
        raise LeaseError("guest boot clock unavailable") from None


def operation_launch(data: bytes) -> dict[str, object]:
    try:
        launch = wire.decode_fact(data)
        owner = cast("dict[str, object]", launch["owner"])
        target = cast("dict[str, object]", launch["target"])
        if launch["kind"] != "launch" or launch["lifetime"] != "operation" or owner["kind"] != "operation":
            raise LeaseError("operation launch required")
        if target["kind"] != "vm":
            raise LeaseError("VM operation launch required")
        return launch
    except (wire.ManagedJobWireError, KeyError, TypeError):
        raise LeaseError("invalid operation launch") from None


def sampled_lease(expected_launch: bytes, sampled_ns: int) -> OperationLease:
    launch = operation_launch(expected_launch)
    sample = checked_clock(sampled_ns)
    if sample > MAX_CLOCK_NS - WINDOW_NS:
        raise LeaseError("lease expiry overflow")
    target = cast("dict[str, object]", launch["target"])
    return OperationLease(
        cast("str", launch["run_id"]),
        wire.launch_sha256(launch),
        cast("str", target["boot_id"]),
        sample,
        sample + WINDOW_NS,
    )


def encode_lease(lease: OperationLease) -> bytes:
    data = _json({"version": 1, **asdict(lease)})
    decode_lease(data)
    return data


def decode_lease(data: bytes) -> OperationLease:
    """Validate persisted or delivered control, including exact fields and bounds."""
    if type(data) is not bytes or len(data) > MAX_LEASE_BYTES:
        raise LeaseError("invalid lease size")
    try:
        value = json.loads(data.decode("ascii"))
        if type(value) is not dict or _json(value) != data:
            raise ValueError
        if set(value) != {"version", "run_id", "receipt_sha256", "boot_id", "sampled_ns", "expires_ns"}:
            raise ValueError
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError
        for name, size in (("run_id", 32), ("receipt_sha256", 64)):
            text = value[name]
            if type(text) is not str or len(text) != size or any(c not in "0123456789abcdef" for c in text):
                raise ValueError
        from uuid import UUID

        boot = value["boot_id"]
        if type(boot) is not str or str(UUID(boot)) != boot:
            raise ValueError
        sample, expiry = checked_clock(value["sampled_ns"]), checked_clock(value["expires_ns"])
        if expiry != sample + WINDOW_NS:
            raise ValueError
        return OperationLease(value["run_id"], value["receipt_sha256"], boot, sample, expiry)
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise LeaseError("invalid lease record") from None


def checked_lease(lease: OperationLease, expected_launch: bytes, now_ns: int | None = None) -> OperationLease:
    if encode_lease(lease) != encode_lease(sampled_lease(expected_launch, lease.sampled_ns)):
        raise LeaseError("lease binding mismatch")
    if now_ns is not None and not lease.sampled_ns <= checked_clock(now_ns) < lease.expires_ns:
        raise LeaseError("lease sample is future or expired")
    return lease
