"""Real installed-OpenSSH evidence for private file helper delivery."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from agentworks.execution._file_read import FileReadObservationState, read_file
from agentworks.execution._file_read_protocol import FileReadFailure
from agentworks.execution._file_stage_exchange import (
    FileStageObservationState,
    stage_begin,
    stage_chunk,
    stage_cleanup,
    stage_reconcile,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._scratch_receipt import scratch_name
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
)
from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="the file helpers require Linux"),
]

_RUNTIME = "/usr/bin/python3"


class _ObservedSSHCarrier(SSHCarrier):
    """Retain safe call-shape evidence around the real SSH carrier."""

    def __init__(self, connection: SSHConnection) -> None:
        super().__init__(connection)
        self.attempts: list[tuple[PreparedInvocation, CarrierIO]] = []

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.attempts.append((invocation, io))
        report = super().execute(invocation, io=io, deadline=deadline)
        assert report.stdout.retention is Retention.DELIVERED
        assert report.stderr.retention is Retention.DELIVERED
        assert report.stdout.data == report.stderr.data == b""
        return report


def _direct_plan() -> IdentityPlan:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)


def _assert_sensitive_attempt(carrier: _ObservedSSHCarrier, attempt: int, *secrets: bytes | str) -> None:
    invocation, io = carrier.attempts[attempt]
    assert io.sensitive
    assert isinstance(io.input, FiniteInput) and io.input.sensitive
    argv = repr(invocation.argv)
    diagnostic = repr(io)
    for secret in secrets:
        text = secret.decode("ascii") if isinstance(secret, bytes) else secret
        assert text not in argv
        assert text not in diagnostic


def test_file_read_delivers_binary_and_typed_noncontent_outcomes_over_real_ssh(
    tmp_path: Path,
    local_sshd: SSHConnection,
) -> None:
    root = tmp_path / "read-root"
    root.mkdir()
    payload = bytes(range(256)) * 17 + b"file-read-payload-canary"
    (root / "payload").write_bytes(payload)
    oversized = b"oversized-content-canary"
    (root / "oversized").write_bytes(oversized)
    carrier = _ObservedSSHCarrier(local_sshd)
    plan = _direct_plan()

    present = read_file(
        carrier,
        trusted_root_path=str(root),
        relative_path="payload",
        max_bytes=len(payload),
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=_RUNTIME,
    )
    absent = read_file(
        carrier,
        trusted_root_path=str(root),
        relative_path="absent",
        max_bytes=1,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=_RUNTIME,
    )
    refused = read_file(
        carrier,
        trusted_root_path=str(root),
        relative_path="oversized",
        max_bytes=1,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=_RUNTIME,
    )

    assert len(carrier.attempts) == 3
    assert present.dispatch is Dispatch.SENT and present.carrier_completion == ExitStatus(code=0)
    assert present.carrier_failure is None
    assert present.observation.state is FileReadObservationState.PRESENT
    snapshot = present.observation.snapshot
    assert snapshot is not None
    assert snapshot.data == payload
    assert snapshot.digest == hashlib.sha256(payload).digest()
    assert snapshot.metadata.size == len(payload)
    assert absent.dispatch is Dispatch.SENT and absent.carrier_completion == ExitStatus(code=0)
    assert absent.carrier_failure is None
    assert absent.observation.state is FileReadObservationState.ABSENT
    assert refused.dispatch is Dispatch.SENT and refused.carrier_completion == ExitStatus(code=0)
    assert refused.carrier_failure is None
    assert refused.observation.state is FileReadObservationState.REFUSED
    assert refused.observation.failure is FileReadFailure.LIMIT
    assert refused.observation.snapshot is None
    _assert_sensitive_attempt(carrier, 0, str(root), "payload", b"file-read-payload-canary")
    _assert_sensitive_attempt(carrier, 1, str(root), "absent")
    _assert_sensitive_attempt(carrier, 2, str(root), "oversized", oversized)
    assert "file-read-payload-canary" not in repr(present)
    assert "oversized-content-canary" not in repr(refused)


def test_file_stage_round_trip_and_exact_cleanup_over_real_ssh(
    tmp_path: Path,
    local_sshd: SSHConnection,
) -> None:
    root = tmp_path / "stage-root"
    root.mkdir()
    token = bytes(range(16))
    first = bytes(range(256)) * 9
    second = b"stage-payload-canary\x00\xff\r\n"
    payload = first + second
    carrier = _ObservedSSHCarrier(local_sshd)
    plan = _direct_plan()

    begun = stage_begin(
        carrier,
        trusted_root_path=str(root),
        relative_path="destination",
        token=token,
        expected_length=len(payload),
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=_RUNTIME,
    )
    assert begun.dispatch is Dispatch.SENT and begun.carrier_completion == ExitStatus(code=0)
    assert begun.carrier_failure is None
    assert begun.observation.state is FileStageObservationState.CREATED
    reference = begun.observation.reference
    assert reference is not None

    chunks = []
    for offset, data in ((0, first), (len(first), second)):
        result = stage_chunk(
            carrier,
            trusted_root_path=str(root),
            relative_path="destination",
            token=token,
            reference=reference,
            offset=offset,
            data=data,
            chunk_digest=hashlib.sha256(data).digest(),
            plan=plan,
            deadline=Deadline.after(15),
            runtime_path=_RUNTIME,
        )
        assert result.dispatch is Dispatch.SENT and result.carrier_completion == ExitStatus(code=0)
        assert result.carrier_failure is None
        assert result.observation.state is FileStageObservationState.ACCEPTED
        chunks.append(result)

    scratch = root / scratch_name(token)
    readback = read_file(
        carrier,
        trusted_root_path=str(scratch),
        relative_path="data",
        max_bytes=len(payload),
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=_RUNTIME,
    )
    assert readback.dispatch is Dispatch.SENT and readback.carrier_completion == ExitStatus(code=0)
    assert readback.carrier_failure is None
    assert readback.observation.state is FileReadObservationState.PRESENT
    assert readback.observation.snapshot is not None
    assert readback.observation.snapshot.data == payload
    assert readback.observation.snapshot.digest == hashlib.sha256(payload).digest()

    recovered = stage_reconcile(
        carrier,
        trusted_root_path=str(root),
        relative_path="destination",
        token=token,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=_RUNTIME,
    )
    assert recovered.dispatch is Dispatch.SENT and recovered.carrier_completion == ExitStatus(code=0)
    assert recovered.carrier_failure is None
    assert recovered.observation.state is FileStageObservationState.RECOVERED
    debt = recovered.observation.cleanup_debt
    assert debt is not None
    cleaned = stage_cleanup(
        carrier,
        trusted_root_path=str(root),
        relative_path="destination",
        token=token,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=_RUNTIME,
    )

    assert cleaned.dispatch is Dispatch.SENT and cleaned.carrier_completion == ExitStatus(code=0)
    assert cleaned.carrier_failure is None
    assert cleaned.observation.state is FileStageObservationState.CLEANED
    assert len(carrier.attempts) == 6
    assert not scratch.exists()
    assert list(root.iterdir()) == []
    assert not (root / "destination").exists()
    for attempt in range(6):
        _assert_sensitive_attempt(carrier, attempt, str(root), "destination", b"stage-payload-canary")
    assert "stage-payload-canary" not in repr(chunks)
    assert "stage-payload-canary" not in repr(readback)
