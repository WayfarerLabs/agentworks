"""Private owned-download composition tests."""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationClaimState
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_download
from agentworks.execution._file_download import (
    FileDownloadControlFact,
    FileDownloadFailure,
    FileDownloadFailurePhase,
    FileDownloadStatus,
    download_file,
)
from agentworks.execution._file_snapshot_exchange import FileSnapshotObservationState, snapshot_begin
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import (
    Carrier,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    SinkOutput,
)
from tests.execution.files._file_deadline_support import AdmittedTimeoutCarrier
from tests.execution.files._file_download_support import (
    BytesSink,
    LostCallStdoutCarrier,
    download,
    owner,
)
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private snapshot helper requires Linux")


@pytest.fixture
def plan() -> IdentityPlan:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    source = tmp_path / "source-root"
    source.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    return source, scratch


@pytest.mark.parametrize(
    "content",
    [b"", bytes(range(256)) * 113 + b"\x00\xffdownload"],
    ids=["empty", "binary-multichunk"],
)
def test_real_helper_downloads_verified_content_and_cleans_snapshot(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    content: bytes,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(content)
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    sink = BytesSink(writes=(1, None, 7)) if content else BytesSink()
    try:
        outcome = download(borrow, source, sink, max(1, len(content)), plan)

        assert outcome.status is FileDownloadStatus.COMPLETE
        assert outcome.stream_verified and outcome.accepted_bytes == len(content)
        assert outcome.source_revision is not None
        assert outcome.source_revision.digest == hashlib.sha256(content).digest()
        assert bytes(sink.data) == content and not sink.closed
        assert outcome.cleanup_debt is None and not outcome.requires_owner_retention
        assert not tuple(scratch.iterdir())
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_proven_absence_writes_nothing(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, _ = roots
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    sink = BytesSink()
    try:
        outcome = download(borrow, source, sink, 1, plan)

        assert outcome.status is FileDownloadStatus.ABSENT
        assert outcome.accepted_bytes == 0 and sink.calls == 0
        assert outcome.source_revision is None and not outcome.requires_owner_retention
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


class _ZeroSink:
    def try_write(self, data: memoryview) -> int:
        return 0


class _OversizedSink:
    def try_write(self, data: memoryview) -> int:
        return len(data) + 1


class _FailingSink:
    def try_write(self, data: memoryview) -> int:
        raise RuntimeError("sink-secret-canary")


class _CancelingSink:
    def try_write(self, data: memoryview) -> int:
        raise KeyboardInterrupt("sink-secret-canary")


class _StalledSink:
    def try_write(self, data: memoryview) -> None:
        return None


class _DiscardSink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


class _CarrierFactInjector:
    def __init__(
        self,
        inner: Carrier,
        failures: dict[int, Failure],
        *,
        lost_calls: frozenset[int] = frozenset(),
    ) -> None:
        self.inner = inner
        self.failures = failures
        self.lost_calls = lost_calls
        self.calls = 0

    @property
    def features(self):
        return self.inner.features

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        selected_io = io
        if self.calls in self.lost_calls:
            assert isinstance(io.output, SinkOutput)
            selected_io = CarrierIO(
                input=io.input,
                output=SinkOutput(_DiscardSink(), io.output.stderr, require_live=False),
                sensitive=io.sensitive,
            )
        report = self.inner.execute(invocation, io=selected_io, deadline=deadline)
        return replace(report, failure=self.failures.get(self.calls))


@pytest.mark.parametrize(
    ("sink", "failure"),
    [
        (_ZeroSink(), FileDownloadFailure.SINK_CONTRACT),
        (_OversizedSink(), FileDownloadFailure.SINK_CONTRACT),
        (_FailingSink(), FileDownloadFailure.SINK),
    ],
    ids=["zero", "oversized", "exception"],
)
def test_bad_sink_is_sanitized_and_snapshot_is_cleaned(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    sink,
    failure: FileDownloadFailure,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"secret-payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    try:
        outcome = download(borrow, source, sink, 64, plan)

        assert outcome.status is FileDownloadStatus.FAILED
        assert outcome.failure is failure and outcome.accepted_bytes == 0
        assert outcome.failure_phase is None
        assert outcome.failure_dispatch is None
        assert outcome.carrier_failure is None
        assert "sink-secret-canary" not in repr(outcome)
        assert outcome.cleanup_debt is None and not outcome.requires_owner_retention
        assert not tuple(scratch.iterdir())
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_sink_control_flow_propagates_with_bounded_clean_state(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            download(borrow, source, _CancelingSink(), 64, plan)

        fact = raised.value.__cause__
        assert isinstance(fact, FileDownloadControlFact)
        assert fact.outcome.cleanup_debt is None
        assert fact.outcome.accepted_bytes == 0 and not fact.outcome.requires_owner_retention
        assert not tuple(scratch.iterdir())
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_real_carrier_timeout_records_deadline_with_unresolved_begin(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    install_fixture_bundle(monkeypatch, scratch, "import time; time.sleep(1)")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = AdmittedTimeoutCarrier(monkeypatch, startup_delay=0.15)
    sink = BytesSink()
    try:
        outcome = download(
            borrow,
            source,
            sink,
            64,
            plan,
            carrier=carrier,
            deadline=Deadline.after(30),
        )

        assert carrier.calls == 1 and carrier.admitted and sink.calls == 0
        assert outcome.status is FileDownloadStatus.UNCERTAIN
        assert outcome.failure is FileDownloadFailure.TERMINATION
        assert outcome.deadline_exceeded and outcome.pending_remote_effects
        assert outcome.requires_owner_retention
        assert not tuple(scratch.iterdir())
    finally:
        database.close()


def test_expired_deadline_before_dispatch_has_no_exchange_facts(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = download(
            borrow,
            source,
            BytesSink(),
            64,
            plan,
            carrier=carrier,
            deadline=Deadline.after(0),
        )

        assert carrier.calls == 0
        assert outcome.failure is FileDownloadFailure.DEADLINE
        assert outcome.failure_phase is None
        assert outcome.failure_dispatch is None
        assert outcome.carrier_failure is None
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_deadline_during_sink_stall_retains_exact_cleanup_debt(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    sink = _StalledSink()
    try:
        outcome = download(borrow, source, sink, 64, plan, deadline=Deadline.after(0.5))

        assert outcome.failure is FileDownloadFailure.DEADLINE
        assert outcome.deadline_exceeded and outcome.cleanup_debt is not None
        assert outcome.accepted_bytes == 0 and outcome.requires_owner_retention
        assert tuple(scratch.iterdir())
    finally:
        database.close()


def test_runtime_refusal_stops_without_reconciliation(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = download(
            borrow,
            source,
            BytesSink(),
            64,
            plan,
            carrier=carrier,
            selected_runtime=runtime_selection("/missing/agentworks-python"),
        )

        assert carrier.calls == 1
        assert outcome.failure is FileDownloadFailure.RUNTIME_PREREQUISITE
        assert outcome.runtime_prerequisite is not None
        assert outcome.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
        assert not outcome.requires_owner_retention
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_source_exceeding_bound_is_refused_without_sink_bytes(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"too-large")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    sink = BytesSink()
    try:
        outcome = download(borrow, source, sink, 3, plan)

        assert outcome.failure is FileDownloadFailure.SNAPSHOT
        assert outcome.snapshot_failure is not None
        assert sink.calls == 0 and outcome.accepted_bytes == 0
        assert outcome.cleanup_debt is None and not tuple(scratch.iterdir())
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_lost_creation_observation_reconciles_and_cleans_without_replay(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LostCallStdoutCarrier(1)
    sink = BytesSink()
    try:
        outcome = download(borrow, source, sink, 64, plan, carrier=carrier)

        assert carrier.calls == 3
        assert outcome.status is FileDownloadStatus.FAILED
        assert outcome.failure is FileDownloadFailure.OBSERVATION
        assert sink.calls == 0 and outcome.cleanup_debt is None
        assert not outcome.requires_owner_retention and not tuple(scratch.iterdir())
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_begin_failure_facts_survive_distinct_reconcile_and_cleanup_failures(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = _CarrierFactInjector(
        LocalCarrier(),
        {
            1: Failure.DISPATCH,
            2: Failure.OBSERVATION,
            3: Failure.OUTPUT,
        },
        lost_calls=frozenset({1, 3}),
    )
    try:
        outcome = download(borrow, source, BytesSink(), 64, plan, carrier=carrier)

        assert carrier.calls == 3
        assert outcome.failure is FileDownloadFailure.OBSERVATION
        assert outcome.failure_phase is FileDownloadFailurePhase.SNAPSHOT_BEGIN
        assert outcome.failure_dispatch is Dispatch.SENT
        assert outcome.carrier_failure is Failure.DISPATCH
        assert outcome.cleanup_debt is not None and not tuple(scratch.iterdir())
    finally:
        database.close()


def test_lost_chunk_observation_hands_no_bytes_then_cleans_known_snapshot(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LostCallStdoutCarrier(2)
    sink = BytesSink()
    try:
        outcome = download(borrow, source, sink, 64, plan, carrier=carrier)

        assert carrier.calls == 3 and sink.calls == 0
        assert outcome.failure is FileDownloadFailure.OBSERVATION
        assert outcome.cleanup_debt is None and not outcome.requires_owner_retention
        assert not tuple(scratch.iterdir())
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_chunk_failure_facts_survive_later_cleanup_failure(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    install_fixture_bundle(
        monkeypatch,
        scratch,
        """
def failed_cleanup(_scratch_fd,debt):
 raise guest.ScratchTransferError(
  guest.ScratchFailureKind.IO,
  guest.ScratchPhase.CLEANUP,
  cleanup_debt=debt,
 )
guest.cleanup_scratch=failed_cleanup
""",
    )
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = _CarrierFactInjector(
        LocalCarrier(),
        {2: Failure.OUTPUT, 3: Failure.INPUT},
        lost_calls=frozenset({2}),
    )
    try:
        outcome = download(borrow, source, BytesSink(), 64, plan, carrier=carrier)

        assert carrier.calls == 3
        assert outcome.failure is FileDownloadFailure.OBSERVATION
        assert outcome.failure_phase is FileDownloadFailurePhase.SNAPSHOT_CHUNK
        assert outcome.failure_dispatch is Dispatch.SENT
        assert outcome.carrier_failure is Failure.OUTPUT
        assert outcome.snapshot_failure is None
        assert outcome.cleanup_debt is not None and tuple(scratch.iterdir())
    finally:
        database.close()


def test_lost_cleanup_observation_retains_exact_debt(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LostCallStdoutCarrier(3)
    sink = BytesSink()
    try:
        outcome = download(borrow, source, sink, 64, plan, carrier=carrier)

        assert bytes(sink.data) == b"payload" and outcome.stream_verified
        assert outcome.status is FileDownloadStatus.FAILED
        assert outcome.failure is FileDownloadFailure.CLEANUP
        assert outcome.cleanup_debt is not None and outcome.requires_owner_retention
        assert not tuple(scratch.iterdir())
    finally:
        database.close()


def test_cleanup_failure_retains_its_exchange_facts(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = _CarrierFactInjector(
        LocalCarrier(),
        {3: Failure.OUTPUT_LIMIT},
        lost_calls=frozenset({3}),
    )
    try:
        outcome = download(borrow, source, BytesSink(), 64, plan, carrier=carrier)

        assert carrier.calls == 3 and outcome.stream_verified
        assert outcome.failure is FileDownloadFailure.CLEANUP
        assert outcome.failure_phase is FileDownloadFailurePhase.SNAPSHOT_CLEANUP
        assert outcome.failure_dispatch is Dispatch.SENT
        assert outcome.carrier_failure is Failure.OUTPUT_LIMIT
        assert outcome.cleanup_debt is not None and not tuple(scratch.iterdir())
    finally:
        database.close()


def test_reconciliation_ownership_uncertainty_clears_stale_actionable_debt(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    install_fixture_bundle(
        monkeypatch,
        scratch,
        "guest.reconcile_scratch_ownership=lambda *args,**kwargs: guest.ScratchOwnershipUncertainty()",
    )
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LostCallStdoutCarrier(1)
    try:
        outcome = download(borrow, source, BytesSink(), 64, plan, carrier=carrier)

        assert carrier.calls == 2
        assert outcome.status is FileDownloadStatus.UNCERTAIN
        assert outcome.snapshot_ownership_uncertain
        assert outcome.cleanup_debt is None and outcome.requires_owner_retention
        assert tuple(scratch.iterdir())
    finally:
        database.close()


def test_closed_cleanup_failure_preserves_exact_debt(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    install_fixture_bundle(
        monkeypatch,
        scratch,
        """
def failed_cleanup(_scratch_fd,debt):
 raise guest.ScratchTransferError(
  guest.ScratchFailureKind.IO,
  guest.ScratchPhase.CLEANUP,
  cleanup_debt=debt,
 )
guest.cleanup_scratch=failed_cleanup
""",
    )
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    try:
        outcome = download(borrow, source, BytesSink(), 64, plan)

        assert outcome.stream_verified
        assert outcome.failure is FileDownloadFailure.CLEANUP
        assert outcome.cleanup_debt is not None and outcome.requires_owner_retention
        assert tuple(scratch.iterdir())
    finally:
        database.close()


class _NonzeroSecondCarrier:
    def __init__(self, completion: ExitStatus) -> None:
        self.inner = LocalCarrier()
        self.calls = 0
        self.completion = completion

    @property
    def features(self):
        return self.inner.features

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        report = self.inner.execute(invocation, io=io, deadline=deadline)
        if self.calls == 2:
            return replace(report, completion=self.completion)
        return report


class _NonzeroCleanupCarrier:
    def __init__(self) -> None:
        self.inner = LocalCarrier()
        self.calls = 0

    @property
    def features(self):
        return self.inner.features

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        report = self.inner.execute(invocation, io=io, deadline=deadline)
        if self.calls == 3:
            return replace(report, completion=ExitStatus(code=9))
        return report


class _ExpiringSink(BytesSink):
    def __init__(self, deadline: Deadline) -> None:
        super().__init__()
        self.deadline = deadline

    def try_write(self, data: memoryview) -> int | None:
        written = super().try_write(data)
        object.__setattr__(self.deadline, "expires_at", 0.0)
        return written


def test_cleanup_entry_expiry_records_cleanup_phase(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LocalCarrier()
    deadline = Deadline.after(30)
    sink = _ExpiringSink(deadline)
    try:
        outcome = download(borrow, source, sink, 64, plan, carrier=carrier, deadline=deadline)

        assert carrier.calls == 2 and bytes(sink.data) == b"payload"
        assert outcome.stream_verified and outcome.deadline_exceeded
        assert outcome.failure is FileDownloadFailure.DEADLINE
        assert outcome.failure_phase is FileDownloadFailurePhase.SNAPSHOT_CLEANUP
        assert outcome.cleanup_debt is not None and outcome.requires_owner_retention
        assert tuple(scratch.iterdir())
    finally:
        database.close()


@pytest.mark.parametrize(
    "completion",
    [ExitStatus(code=9), ExitStatus(signal=9)],
    ids=["nonzero", "signal"],
)
def test_abnormal_chunk_wrapper_exit_hands_no_bytes_and_stops_followons(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    completion: ExitStatus,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = _NonzeroSecondCarrier(completion)
    sink = BytesSink()
    try:
        outcome = download(borrow, source, sink, 64, plan, carrier=carrier)

        assert carrier.calls == 2 and sink.calls == 0
        assert outcome.failure is FileDownloadFailure.TERMINATION
        assert outcome.pending_remote_effects and outcome.requires_owner_retention
        with pytest.raises(StateError):
            operation_owner.close()
        borrow.close()
        with pytest.raises(StateError):
            operation_owner.close()
    finally:
        database.close()


def test_helper_declared_deadline_stops_before_sink_delivery(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    install_fixture_bundle(
        monkeypatch,
        scratch,
        "guest._expires_at=lambda remaining: guest.time.monotonic()",
    )
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    sink = BytesSink()
    carrier = _CarrierFactInjector(LocalCarrier(), {1: Failure.DEADLINE})
    try:
        outcome = download(borrow, source, sink, 64, plan, carrier=carrier)

        assert outcome.failure is FileDownloadFailure.DEADLINE
        assert outcome.failure_phase is FileDownloadFailurePhase.SNAPSHOT_BEGIN
        assert outcome.failure_dispatch is Dispatch.SENT
        assert outcome.carrier_failure is Failure.DEADLINE
        assert outcome.deadline_exceeded and sink.calls == 0
        assert outcome.cleanup_debt is None and not outcome.requires_owner_retention
        assert not tuple(scratch.iterdir())
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_nonzero_cleanup_exit_preserves_debt_despite_cleaned_transcript(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = _NonzeroCleanupCarrier()
    try:
        outcome = download(borrow, source, BytesSink(), 64, plan, carrier=carrier)

        assert carrier.calls == 3 and outcome.stream_verified
        assert outcome.failure is FileDownloadFailure.CLEANUP
        assert outcome.pending_remote_effects
        assert outcome.cleanup_debt is not None and outcome.requires_owner_retention
        assert not tuple(scratch.iterdir())
    finally:
        database.close()


class _InterruptingCarrier:
    def __init__(self, *, expire_deadline: bool = False) -> None:
        self.expire_deadline = expire_deadline

    @property
    def features(self):
        return LocalCarrier().features

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        if self.expire_deadline:
            object.__setattr__(deadline, "expires_at", 0.0)
        raise KeyboardInterrupt("carrier-secret-canary")


def test_interrupted_dispatch_exports_pending_effect_facts_and_preserves_borrow(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            download(borrow, source, BytesSink(), 64, plan, carrier=_InterruptingCarrier())

        fact = raised.value.__cause__
        assert isinstance(fact, FileDownloadControlFact)
        assert fact.outcome.pending_remote_effects
        assert fact.outcome.failure_phase is None
        assert fact.outcome.failure_dispatch is None
        assert fact.outcome.carrier_failure is None
        assert fact.outcome.requires_owner_retention
        with pytest.raises(StateError):
            operation_owner.borrow()
        borrow.close()
        with pytest.raises(StateError):
            operation_owner.close()
    finally:
        database.close()


def test_interrupted_dispatch_records_expired_deadline_fact(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            download(
                borrow,
                source,
                BytesSink(),
                64,
                plan,
                carrier=_InterruptingCarrier(expire_deadline=True),
            )

        fact = raised.value.__cause__
        assert isinstance(fact, FileDownloadControlFact)
        assert fact.outcome.deadline_exceeded
        assert fact.outcome.pending_remote_effects
        assert fact.outcome.requires_owner_retention
    finally:
        database.close()


class _BorrowCheckingSink(BytesSink):
    def __init__(self, operation_owner) -> None:
        super().__init__()
        self.owner = operation_owner
        self.refused = False

    def try_write(self, data: memoryview) -> int:
        with pytest.raises(StateError):
            self.owner.borrow()
        self.refused = True
        result = super().try_write(data)
        assert isinstance(result, int)
        return result


def test_one_borrow_spans_snapshot_delivery_and_cleanup(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    sink = _BorrowCheckingSink(operation_owner)
    try:
        outcome = download(borrow, source, sink, 64, plan)

        assert outcome.status is FileDownloadStatus.COMPLETE and sink.refused
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


class _FailingSinkGetter:
    @property
    def try_write(self):
        raise RuntimeError("sink-getter-secret-canary")


@pytest.mark.parametrize(
    ("trusted_root", "sink", "max_bytes"),
    [
        ("/approved/\udcff", BytesSink(), 1),
        ("/approved", _FailingSinkGetter(), 1),
        ("/approved", BytesSink(), 0),
    ],
    ids=["invalid-utf8", "sink-getter", "zero-bound"],
)
def test_validation_preserves_caller_borrow_and_precedes_carrier_without_exception_context(
    tmp_path: Path,
    plan: IdentityPlan,
    trusted_root: str,
    sink,
    max_bytes: int,
) -> None:
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError) as raised:
            download_file(
                carrier,
                trusted_root_path=trusted_root,
                relative_path="source",
                sink=sink,
                max_bytes=max_bytes,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                borrow=borrow,
            )

        assert raised.value.__cause__ is None and raised.value.__context__ is None
        assert carrier.calls == 0
        with pytest.raises(StateError):
            operation_owner.borrow()
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_combined_begin_request_rejection_never_arms_dispatch_and_preserves_borrow(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    carrier = LocalCarrier()
    expected = IdentityExpectation(1, 1, tuple(range(9_000)))
    oversized_plan = IdentityPlan(expected, IdentityMode.DIRECT)
    sink = BytesSink()
    try:
        with pytest.raises(ValidationError) as raised:
            download_file(
                carrier,
                trusted_root_path="/approved",
                relative_path="source",
                sink=sink,
                max_bytes=1,
                plan=oversized_plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                borrow=borrow,
            )

        fact = raised.value.__cause__
        assert isinstance(fact, FileDownloadControlFact)
        assert carrier.calls == 0 and sink.calls == 0
        assert not fact.outcome.pending_remote_effects
        assert not fact.outcome.coordination_uncertain
        assert not fact.outcome.requires_owner_retention
        claim = database.operations.inspect(operation_owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        with pytest.raises(StateError):
            operation_owner.close()
        borrow.close()
        operation_owner.close()
    finally:
        database.close()


def test_whole_digest_mismatch_is_not_complete_and_still_cleans(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, scratch = roots
    source.joinpath("source").write_bytes(b"payload")
    real_begin = snapshot_begin

    def mismatched_begin(*args, **kwargs):
        result = real_begin(*args, **kwargs)
        observation = result.observation
        assert observation is not None and observation.state is FileSnapshotObservationState.READY
        snapshot = observation.snapshot
        assert snapshot is not None
        changed = replace(snapshot.source, digest=b"\0" * 32)
        return replace(result, observation=replace(observation, snapshot=replace(snapshot, source=changed)))

    monkeypatch.setattr(_file_download, "snapshot_begin", mismatched_begin)
    database = Database(tmp_path / "state.db")
    operation_owner = owner(database)
    borrow = operation_owner.borrow()
    sink = BytesSink()
    try:
        outcome = download(borrow, source, sink, 64, plan)

        assert outcome.failure is FileDownloadFailure.INTEGRITY
        assert outcome.failure_phase is None
        assert outcome.failure_dispatch is None
        assert outcome.carrier_failure is None
        assert not outcome.stream_verified and bytes(sink.data) == b"payload"
        assert outcome.cleanup_debt is None and not tuple(scratch.iterdir())
        borrow.close()
        operation_owner.close()
    finally:
        database.close()
