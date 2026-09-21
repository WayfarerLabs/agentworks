"""Real helper and host-reducer checks for private snapshot exchanges."""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_snapshot_exchange import (
    FileSnapshotObservationState,
    snapshot_begin,
    snapshot_chunk,
    snapshot_cleanup,
    snapshot_reconcile,
)
from agentworks.execution._file_snapshot_protocol import (
    MAX_SNAPSHOT_CHUNK_BYTES,
    FileSnapshotChunkRequest,
    FileSnapshotChunkResult,
    FileSnapshotFailureCode,
    FileSnapshotRequest,
    decode_file_snapshot_request,
    empty_file_snapshot_body,
    encode_file_snapshot_chunk_result,
    snapshot_context,
)
from agentworks.execution._file_spool import SpoolSnapshotFailureKind
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileWireError, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._scratch import ReadyScratchReference, ScratchFailureKind, ScratchReference, _cleanup_debt
from agentworks.execution._scratch_receipt import ScratchOwnership, _Identity, scratch_name
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private snapshot helper requires Linux")

_TOKEN = bytes(range(16))
_FINAL_DEADLINE_PATCH = """
clock=[guest.time.monotonic()]
def controlled_monotonic():
 return clock[0]
guest.time.monotonic=controlled_monotonic
real_operate=guest._operate
def advancing_operate(*args,**kwargs):
 result=real_operate(*args,**kwargs)
 clock[0]+=60.0
 return result
guest._operate=advancing_operate
"""
_IMMEDIATE_DEADLINE_PATCH = """
guest._expires_at=lambda remaining: guest.time.monotonic()
"""


@pytest.fixture
def plan() -> IdentityPlan:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)


@pytest.fixture
def scratch_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "scratch"
    root.mkdir()
    root.chmod(0o1777)
    install_fixture_bundle(monkeypatch, root)
    return root


def _begin(
    source_root: Path,
    relative_path: str,
    max_bytes: int,
    plan: IdentityPlan,
    *,
    token: bytes = _TOKEN,
    carrier: LocalCarrier | None = None,
    runtime_path: str = sys.executable,
):
    selected = carrier or LocalCarrier()
    result = snapshot_begin(
        selected,
        trusted_root_path=str(source_root),
        relative_path=relative_path,
        max_bytes=max_bytes,
        token=token,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=runtime_path,
    )
    return selected, result


@pytest.mark.parametrize(
    "runtime",
    [Path(sys.executable), Path("/usr/bin/python3.11")],
    ids=["current", "distribution-3.11"],
)
def test_real_helper_exchanges_binary_snapshot_recovery_and_exact_cleanup(
    tmp_path: Path,
    scratch_root: Path,
    plan: IdentityPlan,
    runtime: Path,
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    source_root = tmp_path / "source"
    source_root.mkdir()
    content = bytes(range(256)) * 113 + b"\x00\xffsnapshot-secret"
    source = source_root / "payload"
    source.write_bytes(content)
    before = source.stat()
    source_root.chmod(0o555)
    try:
        begin_carrier, begun = _begin(
            source_root,
            "payload",
            len(content),
            plan,
            runtime_path=str(runtime),
        )
    finally:
        source_root.chmod(0o755)

    assert begun.observation.state is FileSnapshotObservationState.READY
    snapshot = begun.observation.snapshot
    assert snapshot is not None
    assert snapshot.source.stat.size == len(content)
    assert snapshot.source.digest == hashlib.sha256(content).digest()
    assert snapshot.ready._digest == snapshot.source.digest
    assert begin_carrier.calls == 1
    assert begin_carrier.io is not None
    assert isinstance(begin_carrier.io.input, FiniteInput) and begin_carrier.io.input.sensitive
    assert begin_carrier.io.sensitive and isinstance(begin_carrier.io.output, SinkOutput)
    assert begin_carrier.invocation is not None
    assert str(source_root) not in begin_carrier.invocation.argv
    assert "payload" not in begin_carrier.invocation.argv

    downloaded = bytearray()
    for offset in range(0, len(content), MAX_SNAPSHOT_CHUNK_BYTES):
        length = min(MAX_SNAPSHOT_CHUNK_BYTES, len(content) - offset)
        result = snapshot_chunk(
            LocalCarrier(),
            token=_TOKEN,
            ready=snapshot.ready,
            offset=offset,
            length=length,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_path=str(runtime),
        )
        assert result.observation.state is FileSnapshotObservationState.CHUNK
        chunk = result.observation.chunk
        assert chunk is not None and chunk.offset == offset and chunk.length == length
        downloaded.extend(chunk.data)
        assert repr(chunk.data) not in repr(result)
    assert bytes(downloaded) == content
    assert hashlib.sha256(downloaded).digest() == snapshot.ready._digest

    recovered = snapshot_reconcile(
        LocalCarrier(),
        token=_TOKEN,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=str(runtime),
    )
    assert recovered.observation.state is FileSnapshotObservationState.RECOVERED
    debt = recovered.observation.cleanup_debt
    assert debt == _cleanup_debt(snapshot.ready)
    assert recovered.observation.snapshot is None and recovered.observation.chunk is None

    cleaned = snapshot_cleanup(
        LocalCarrier(),
        token=_TOKEN,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=str(runtime),
    )
    assert cleaned.observation.state is FileSnapshotObservationState.CLEANED
    assert not (scratch_root / scratch_name(_TOKEN)).exists()

    after_cleanup = snapshot_reconcile(
        LocalCarrier(),
        token=_TOKEN,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=str(runtime),
    )
    assert after_cleanup.observation.state is FileSnapshotObservationState.OWNERSHIP_UNCERTAIN
    assert after_cleanup.observation.cleanup_debt is None

    delayed = snapshot_chunk(
        LocalCarrier(),
        token=_TOKEN,
        ready=snapshot.ready,
        offset=0,
        length=1,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=str(runtime),
    )
    assert delayed.observation.state is FileSnapshotObservationState.REFUSED
    failure = delayed.observation.failure
    assert failure is not None and failure.code is FileSnapshotFailureCode.SCRATCH
    assert failure.scratch_kind is ScratchFailureKind.CONFLICT
    assert failure.cleanup_debt == debt

    after = source.stat()
    assert source.read_bytes() == content
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert tuple(source_root.iterdir()) == (source,)


def test_absence_and_maximum_refusal_create_no_scratch(
    tmp_path: Path,
    scratch_root: Path,
    plan: IdentityPlan,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()

    _, absent = _begin(source_root, "missing", 10, plan)
    assert absent.observation.state is FileSnapshotObservationState.ABSENT
    assert tuple(scratch_root.iterdir()) == ()

    (source_root / "too-large").write_bytes(b"12345")
    _, refused = _begin(source_root, "too-large", 4, plan, token=b"z" * 16)
    assert refused.observation.state is FileSnapshotObservationState.REFUSED
    failure = refused.observation.failure
    assert failure is not None and failure.code is FileSnapshotFailureCode.SPOOL
    assert failure.spool_kind is SpoolSnapshotFailureKind.LIMIT
    assert tuple(scratch_root.iterdir()) == ()

    empty_token = b"q" * 16
    (source_root / "empty").write_bytes(b"")
    _, empty = _begin(source_root, "empty", 1, plan, token=empty_token)
    snapshot = empty.observation.snapshot
    assert snapshot is not None and snapshot.source.stat.size == 0
    empty_chunk = snapshot_chunk(
        LocalCarrier(),
        token=empty_token,
        ready=snapshot.ready,
        offset=0,
        length=0,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=sys.executable,
    )
    assert empty_chunk.observation.state is FileSnapshotObservationState.CHUNK
    assert empty_chunk.observation.chunk is not None and empty_chunk.observation.chunk.data == b""
    empty_debt = _cleanup_debt(snapshot.ready)
    empty_cleanup = snapshot_cleanup(
        LocalCarrier(),
        token=empty_token,
        cleanup_debt=empty_debt,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=sys.executable,
    )
    assert empty_cleanup.observation.state is FileSnapshotObservationState.CLEANED


def test_host_refuses_out_of_range_chunk_before_dispatch(plan: IdentityPlan) -> None:
    ready = _ready(plan, length=4, digest=hashlib.sha256(b"data").digest())
    carrier = LocalCarrier()

    with pytest.raises(ValidationError):
        snapshot_chunk(
            carrier,
            token=_TOKEN,
            ready=ready,
            offset=4,
            length=1,
            plan=plan,
            deadline=Deadline.after(15),
        )

    assert carrier.calls == 0


def test_changed_scratch_is_refused_with_exact_cleanup_debt(
    tmp_path: Path,
    scratch_root: Path,
    plan: IdentityPlan,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "payload").write_bytes(b"original")
    _, begun = _begin(source_root, "payload", 8, plan)
    snapshot = begun.observation.snapshot
    assert snapshot is not None
    data_path = scratch_root / scratch_name(_TOKEN) / "data"
    data_path.write_bytes(b"tampered")

    result = snapshot_chunk(
        LocalCarrier(),
        token=_TOKEN,
        ready=snapshot.ready,
        offset=0,
        length=8,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=sys.executable,
    )

    assert result.observation.state is FileSnapshotObservationState.REFUSED
    failure = result.observation.failure
    assert failure is not None and failure.scratch_kind is ScratchFailureKind.CONFLICT
    assert failure.cleanup_debt == _cleanup_debt(snapshot.ready)


def test_identity_refusal_precedes_source_or_scratch_access(
    tmp_path: Path,
    scratch_root: Path,
    plan: IdentityPlan,
) -> None:
    wrong = IdentityExpectation(plan.expected.euid + 1, plan.expected.egid, plan.expected.groups)
    wrong_plan = IdentityPlan(wrong, IdentityMode.DIRECT)

    _, result = _begin(tmp_path / "does-not-exist", "secret", 1, wrong_plan)

    assert result.observation.state is FileSnapshotObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.code is FileSnapshotFailureCode.IDENTITY_MISMATCH
    assert tuple(scratch_root.iterdir()) == ()


def test_final_deadline_retains_created_snapshot_debt_after_descriptor_closure(
    tmp_path: Path,
    scratch_root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(monkeypatch, scratch_root, _FINAL_DEADLINE_PATCH)
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "payload").write_bytes(b"deadline")

    _, result = _begin(source_root, "payload", 8, plan)

    assert result.observation.state is FileSnapshotObservationState.REFUSED
    failure = result.observation.failure
    assert failure is not None and failure.code is FileSnapshotFailureCode.SPOOL
    assert failure.spool_kind is SpoolSnapshotFailureKind.DEADLINE
    assert failure.cleanup_debt is not None
    assert (scratch_root / scratch_name(_TOKEN)).is_dir()


def test_initial_deadline_refuses_before_any_filesystem_access(
    tmp_path: Path,
    scratch_root: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(monkeypatch, scratch_root, _IMMEDIATE_DEADLINE_PATCH)
    scratch_root.rmdir()

    _, result = _begin(tmp_path / "missing-source", "payload", 8, plan)

    assert result.observation.state is FileSnapshotObservationState.REFUSED
    failure = result.observation.failure
    assert failure is not None and failure.code is FileSnapshotFailureCode.SPOOL
    assert failure.spool_kind is SpoolSnapshotFailureKind.DEADLINE
    assert failure.cleanup_debt is None


def _ready(plan: IdentityPlan, *, length: int, digest: bytes) -> ReadyScratchReference:
    ownership = ScratchOwnership(
        _TOKEN,
        snapshot_context(plan.expected),
        _Identity(1, 2),
        _Identity(1, 3),
        _Identity(1, 4),
        plan.expected.egid,
        length,
        _Identity(1, 5),
    )
    return ReadyScratchReference(ScratchReference(ownership), digest, 7, 8)


def _write(sink: object, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)  # type: ignore[attr-defined]
        assert written is not None and written > 0
        remaining = remaining[written:]


@dataclass
class TranscriptCarrier:
    build: object
    calls: int = 0
    io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, deadline
        self.calls += 1
        self.io = io
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        request = decode_file_snapshot_request(io.input.data)
        transcript = self.build(request, io.input.data)  # type: ignore[operator]
        _write(io.output.stdout, transcript)
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=0), 0, output, output, None)


def _chunk_records(request: FileSnapshotRequest, data: bytes, *, digest: bytes | None = None) -> bytes:
    assert isinstance(request, FileSnapshotChunkRequest)
    result = b""
    sequence = 0
    for offset in range(0, len(data), 4_096):
        result += encode_file_record(
            request.nonce, FileRecord(sequence, FileRecordKind.DATA, data[offset : offset + 4_096])
        )
        sequence += 1
    control = FileSnapshotChunkResult(
        request.offset,
        len(data),
        data,
        hashlib.sha256(data).digest() if digest is None else digest,
    )
    result += encode_file_record(
        request.nonce,
        FileRecord(sequence, FileRecordKind.RESULT, encode_file_snapshot_chunk_result(control)),
    )
    result += encode_file_record(
        request.nonce,
        FileRecord(sequence + 1, FileRecordKind.FINISHED, empty_file_snapshot_body()),
    )
    return result


def _overfragmented_chunk_records(request: FileSnapshotRequest, data: bytes) -> bytes:
    assert isinstance(request, FileSnapshotChunkRequest)
    result = b"".join(
        encode_file_record(request.nonce, FileRecord(index, FileRecordKind.DATA, bytes((byte,))))
        for index, byte in enumerate(data)
    )
    control = FileSnapshotChunkResult(request.offset, len(data), data, hashlib.sha256(data).digest())
    result += encode_file_record(
        request.nonce,
        FileRecord(len(data), FileRecordKind.RESULT, encode_file_snapshot_chunk_result(control)),
    )
    return result + encode_file_record(
        request.nonce,
        FileRecord(len(data) + 1, FileRecordKind.FINISHED, empty_file_snapshot_body()),
    )


def test_tampered_or_truncated_chunk_transcript_releases_no_bytes(plan: IdentityPlan) -> None:
    data = b"private-binary\x00\xff"
    ready = _ready(plan, length=len(data), digest=hashlib.sha256(data).digest())
    carriers = (
        TranscriptCarrier(lambda request, _raw: _chunk_records(request, data, digest=b"x" * 32)),
        TranscriptCarrier(lambda request, _raw: _chunk_records(request, data)[:-1]),
        TranscriptCarrier(lambda request, _raw: _overfragmented_chunk_records(request, data)),
    )

    for carrier in carriers:
        result = snapshot_chunk(
            carrier,
            token=_TOKEN,
            ready=ready,
            offset=0,
            length=len(data),
            plan=plan,
            deadline=Deadline.after(15),
        )
        assert result.observation.state is FileSnapshotObservationState.UNCERTAIN
        assert result.observation.chunk is None
        assert repr(data) not in repr(result)


def test_wrong_response_nonce_and_reflected_request_are_rejected_without_retention(plan: IdentityPlan) -> None:
    data = b"secret"
    ready = _ready(plan, length=len(data), digest=hashlib.sha256(data).digest())

    def wrong_nonce(request: FileSnapshotRequest, _raw: bytes) -> bytes:
        transcript = _chunk_records(request, data)
        return transcript.replace(request.nonce.encode("ascii"), b"0" * 32)

    nonce_result = snapshot_chunk(
        TranscriptCarrier(wrong_nonce),
        token=_TOKEN,
        ready=ready,
        offset=0,
        length=len(data),
        plan=plan,
        deadline=Deadline.after(15),
    )
    canary = "reflected-source-secret"
    reflected = snapshot_begin(
        TranscriptCarrier(lambda _request, raw: raw),
        trusted_root_path="/" + canary,
        relative_path=canary,
        max_bytes=10,
        token=_TOKEN,
        plan=plan,
        deadline=Deadline.after(15),
    )

    assert nonce_result.observation.state is FileSnapshotObservationState.UNCERTAIN
    assert nonce_result.observation.error is FileWireError.NONCE
    assert reflected.observation.state is FileSnapshotObservationState.UNCERTAIN
    assert reflected.observation.error in {FileWireError.TRUNCATED, FileWireError.MALFORMED}
    assert canary not in repr(reflected)
