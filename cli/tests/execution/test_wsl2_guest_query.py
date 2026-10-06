"""Local Linux behavior and conservative reduction of WSL2 guest query output."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from agentworks.execution._wsl2_guest_query import (
    LEGACY_GUEST_QUERY_SOURCE,
    MAX_GUEST_QUERY_RESPONSE_BYTES,
    reduce_guest_query_response,
)
from agentworks.execution._wsl2_lifecycle import GuestAnchorIdentity, GuestAnchorPresence

NONCE = "a" * 32
BOOT = "12345678-1234-1234-1234-123456789abc"
OTHER_BOOT = "87654321-4321-4321-4321-cba987654321"
IDENTITY = GuestAnchorIdentity(BOOT, 137, 8192, 4096)
_PYTHON_311 = shutil.which("python3.11")
_GUEST_PYTHONS = [sys.executable, *([_PYTHON_311] if _PYTHON_311 is not None else [])]


def _wire(kind: str, *, boot: str = BOOT, init: str = "4096", ticks: str = "8192", pid: int = 137) -> bytes:
    return f"AGW_GQ2 {NONCE} {pid} {boot} {init} {kind} {ticks}\n".encode("ascii")


@pytest.mark.parametrize(
    ("response", "presence"),
    [
        (_wire("found"), GuestAnchorPresence.PRESENT),
        (_wire("found", ticks="8193"), GuestAnchorPresence.ABSENT_CONFIRMED),
        (_wire("found", boot=OTHER_BOOT), GuestAnchorPresence.ABSENT_CONFIRMED),
        (_wire("found", init="4097"), GuestAnchorPresence.ABSENT_CONFIRMED),
        (_wire("missing", ticks="-"), GuestAnchorPresence.ABSENT_CONFIRMED),
        (_wire("unknown", ticks="-"), GuestAnchorPresence.UNKNOWN),
        (_wire("found", boot="-"), GuestAnchorPresence.UNKNOWN),
        (_wire("found", init="-"), GuestAnchorPresence.UNKNOWN),
        (_wire("found", boot=OTHER_BOOT, init="-"), GuestAnchorPresence.UNKNOWN),
        (_wire("missing", init="-", ticks="-"), GuestAnchorPresence.UNKNOWN),
        (_wire("missing", init="18446744073709551616", ticks="-"), GuestAnchorPresence.UNKNOWN),
        (_wire("missing", boot="-", ticks="-"), GuestAnchorPresence.UNKNOWN),
        (_wire("unknown", boot=OTHER_BOOT, ticks="-"), GuestAnchorPresence.UNKNOWN),
        (_wire("missing"), GuestAnchorPresence.UNKNOWN),
        (_wire("found", ticks="18446744073709551616"), GuestAnchorPresence.UNKNOWN),
        (_wire("found", pid=138), GuestAnchorPresence.UNKNOWN),
        (_wire("found")[:-1], GuestAnchorPresence.UNKNOWN),
        (_wire("found") + b"\n", GuestAnchorPresence.UNKNOWN),
        (_wire("found").replace(b"\n", b"\r\n"), GuestAnchorPresence.UNKNOWN),
        (_wire("found").replace(NONCE.encode(), b"b" * 32), GuestAnchorPresence.UNKNOWN),
        (_wire("found") + b"x" * MAX_GUEST_QUERY_RESPONSE_BYTES, GuestAnchorPresence.UNKNOWN),
    ],
)
def test_reducer_requires_exact_wire(response: bytes, presence: GuestAnchorPresence) -> None:
    assert (
        reduce_guest_query_response(response, IDENTITY, NONCE, exit_status=0, complete=True, deadline_expired=False)
        is presence
    )


@pytest.mark.parametrize(
    ("exit_status", "complete", "deadline_expired"),
    [(1, True, False), (None, True, False), (0, False, False), (0, True, True)],
)
def test_reducer_requires_successful_complete_timely_client_observation(
    exit_status: int | None, complete: bool, deadline_expired: bool
) -> None:
    assert (
        reduce_guest_query_response(
            _wire("missing", ticks="-"),
            IDENTITY,
            NONCE,
            exit_status=exit_status,
            complete=complete,
            deadline_expired=deadline_expired,
        )
        is GuestAnchorPresence.UNKNOWN
    )


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
@pytest.mark.parametrize("python", _GUEST_PYTHONS)
def test_fixed_guest_source_observes_current_process_and_missing_pid(python: str) -> None:
    with open("/proc/sys/kernel/random/boot_id", "rb") as source:
        boot = source.read().decode("ascii").removesuffix("\n")
    with open(f"/proc/{os.getpid()}/stat", "rb") as source:
        fields = source.read().split(b") ")[-1].split()
    with open("/proc/1/stat", "rb") as source:
        init_fields = source.read().split(b") ")[-1].split()
    identity = GuestAnchorIdentity(boot, os.getpid(), int(fields[19]), int(init_fields[19]))
    present = subprocess.run(
        [python, "-c", LEGACY_GUEST_QUERY_SOURCE, NONCE, str(identity.pid)],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert len(present.stdout) <= MAX_GUEST_QUERY_RESPONSE_BYTES
    assert (
        reduce_guest_query_response(
            present.stdout,
            identity,
            NONCE,
            exit_status=present.returncode,
            complete=True,
            deadline_expired=False,
        )
        is GuestAnchorPresence.PRESENT
    )

    absent_pid = 2**31 - 1
    missing = subprocess.run(
        [python, "-c", LEGACY_GUEST_QUERY_SOURCE, NONCE, str(absent_pid)],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert len(missing.stdout) <= MAX_GUEST_QUERY_RESPONSE_BYTES
    assert (
        reduce_guest_query_response(
            missing.stdout,
            GuestAnchorIdentity(boot, absent_pid, 1, identity.init_start_ticks),
            NONCE,
            exit_status=missing.returncode,
            complete=True,
            deadline_expired=False,
        )
        is GuestAnchorPresence.ABSENT_CONFIRMED
    )
