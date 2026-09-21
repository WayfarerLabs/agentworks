"""Complete private upload composition under core operation ownership."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

import agentworks.execution._file_publication_exchange as publication_exchange
import agentworks.execution._file_stage_exchange as stage_exchange
from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.execution._file_publication import Match, PublicationFailureKind, Replace
from agentworks.execution._file_publication_protocol import (
    FilePublicationFailureCode,
    FilePublicationFailureControl,
    PublicationCleanupState,
    empty_file_publication_body,
    encode_file_publication_failure,
)
from agentworks.execution._file_stage_protocol import (
    FileStageFailureCode,
    FileStageFailureControl,
    empty_file_stage_body,
    encode_file_stage_failure,
)
from agentworks.execution._file_upload import (
    FileUploadControlFact,
    FileUploadFailure,
    FileUploadStatus,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._publication_receipt import PublicationReceiptFailureKind, publication_stage_name
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
)
from agentworks.execution._scratch import ScratchFailureKind, ScratchPhase
from agentworks.execution._scratch_receipt import scratch_name
from agentworks.execution.carrier import (
    ByteSink,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Retention,
    SinkOutput,
)
from agentworks.operations import OperationOwner
from tests.execution.files._file_deadline_support import AdmittedTimeoutCarrier
from tests.execution.files._file_publication_support import (
    LocalCarrier,
)
from tests.execution.files._file_publication_support import (
    install_fixture_bundle as install_publication_bundle,
)
from tests.execution.files._file_stage_support import fixture_source as stage_fixture_source
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage_bundle
from tests.execution.files._file_upload_support import BytesSource, LostCallStdoutCarrier
from tests.execution.files._file_upload_support import owner as _owner
from tests.execution.files._file_upload_support import upload as _upload
from tests.execution.files._runtime_support import runtime_nonce, runtime_ready_record

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private upload helper requires Linux")

_LEAVE_EXACT_PUBLICATION_DEBT = """
publication=sys.modules['_agw_file_publication._file_publication']
def fail_write(descriptor,content):
 raise publication.FilePublicationError(
  publication.PublicationFailureKind.IO,
  publication.PublicationPhase.CONTENT,
 )
publication._write=fail_write
real_unlink=publication.os.unlink
def fail_stage_unlink(path,*args,**kwargs):
 if isinstance(path,str) and path.startswith('.agentworks-stage-'):
  raise OSError
 return real_unlink(path,*args,**kwargs)
publication.os.unlink=fail_stage_unlink
"""
_PARTIAL_STAGE_BEGIN = """
real_begin=guest.begin_scratch
def partial_begin(*args,**kwargs):
 result=real_begin(*args,**kwargs)
 raise guest.ScratchTransferError(
  guest.ScratchFailureKind.IO,
  guest.ScratchPhase.BEGIN,
  cleanup_debt=guest._cleanup_debt(result),
 )
guest.begin_scratch=partial_begin
"""
_FAIL_STAGE_CLEANUP = """
real_cleanup=guest.cleanup_scratch
def failed_cleanup(parent_fd,debt):
 raise guest.ScratchTransferError(
  guest.ScratchFailureKind.IO,
  guest.ScratchPhase.CLEANUP,
  cleanup_debt=debt,
 )
guest.cleanup_scratch=failed_cleanup
"""
_PUBLICATION_DEADLINE = """
def deadline_publish(*args,**kwargs):
 raise guest.FilePublicationError(
  guest.PublicationFailureKind.DEADLINE,
  guest.PublicationPhase.CONTENT,
 )
guest.publish_file=deadline_publish
"""
_PROGRESS_CLEANUP_THEN_LOSE_BINDING_IDENTITY = """
real_parent_identity=guest._parent_identity
parent_identity_calls=[0]
def fail_second_parent_identity(*args,**kwargs):
 parent_identity_calls[0]+=1
 if parent_identity_calls[0]==2:
  raise guest._SafeFailure(
   guest.FilePublicationFailureControl(guest.FilePublicationFailureCode.PARENT_REFUSED)
  )
 return real_parent_identity(*args,**kwargs)
guest._parent_identity=fail_second_parent_identity
def progressed_cleanup(scratch_parent_fd,publication_parent_fd,debt,*args,**kwargs):
 guest.os.unlink(debt.name,dir_fd=publication_parent_fd)
 raise guest.PublicationReceiptError(
  guest.PublicationReceiptFailureKind.IO,
  cleanup_debt=guest.PublicationStageCleanupDebt(debt._ownership,True),
 )
guest.cleanup_publication_stage=progressed_cleanup
"""
_PUBLICATION_CLEANUP_DEADLINE = """
def deadline_cleanup(scratch_parent_fd,publication_parent_fd,debt,*args,**kwargs):
 raise guest.PublicationReceiptError(
  guest.PublicationReceiptFailureKind.DEADLINE,
  cleanup_debt=debt,
 )
guest.cleanup_publication_stage=deadline_cleanup
"""


class NonzeroCallCarrier:
    def __init__(self, nonzero_call: int) -> None:
        self._carrier = LocalCarrier()
        self._nonzero_call = nonzero_call
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        report = self._carrier.execute(invocation, io=io, deadline=deadline)
        if self.calls == self._nonzero_call:
            return replace(report, completion=ExitStatus(code=19))
        return report


class MissingCompletionCallCarrier:
    def __init__(self, incomplete_call: int) -> None:
        self._carrier = LocalCarrier()
        self._incomplete_call = incomplete_call
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        report = self._carrier.execute(invocation, io=io, deadline=deadline)
        if self.calls == self._incomplete_call:
            return replace(report, completion=None)
        return report


class RaisingCallCarrier:
    def __init__(self, raising_call: int) -> None:
        self._carrier = LocalCarrier()
        self._raising_call = raising_call
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        if self.calls == self._raising_call:
            raise RuntimeError("carrier-secret-canary")
        return self._carrier.execute(invocation, io=io, deadline=deadline)


class DeadlineInterruptingCarrier:
    calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        del invocation, io
        self.calls += 1
        object.__setattr__(deadline, "expires_at", 0.0)
        raise KeyboardInterrupt("carrier-secret-canary")


class RuntimeRefusalOnCallCarrier:
    def __init__(self, refusal_call: int, state: RuntimePrerequisiteState) -> None:
        self._carrier = LocalCarrier()
        self._refusal_call = refusal_call
        self._state = state
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        if self.calls != self._refusal_call:
            return self._carrier.execute(invocation, io=io, deadline=deadline)
        del deadline
        assert isinstance(io.output, SinkOutput)
        token = "-" if self._state is RuntimePrerequisiteState.MISSING else "0"
        record = f"AGW_RUNTIME_1:{runtime_nonce(invocation)}:{self._state.value}:{token}\n".encode("ascii")
        _write(io.output.stdout, record)
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(
            Dispatch.SENT,
            ExitStatus(code=0),
            0,
            output,
            output,
        )


class LostThenRuntimeRefusalCarrier:
    def __init__(self) -> None:
        self._carrier = LocalCarrier()
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        assert isinstance(io.output, SinkOutput)
        if self.calls == 1:
            hidden = CarrierIO(
                input=io.input,
                output=SinkOutput(_DiscardSink(), io.output.stderr, require_live=False),
                sensitive=io.sensitive,
            )
            return self._carrier.execute(invocation, io=hidden, deadline=deadline)
        del deadline
        record = f"AGW_RUNTIME_1:{runtime_nonce(invocation)}:missing:-\n".encode("ascii")
        _write(io.output.stdout, record)
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=0), 0, output, output)


class ClosedFailureOnCallCarrier:
    def __init__(self, failure_call: int, failure_body: bytes, finished_body: bytes) -> None:
        self._carrier = LocalCarrier()
        self._failure_call = failure_call
        self._failure_body = failure_body
        self._finished_body = finished_body
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        if self.calls != self._failure_call:
            return self._carrier.execute(invocation, io=io, deadline=deadline)
        del deadline
        assert isinstance(io.output, SinkOutput)
        nonce = runtime_nonce(invocation)
        transcript = encode_file_record(nonce, FileRecord(0, FileRecordKind.FAILED, self._failure_body))
        transcript += encode_file_record(nonce, FileRecord(1, FileRecordKind.FINISHED, self._finished_body))
        _write(io.output.stdout, runtime_ready_record(invocation) + transcript)
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=0), 0, output, output)


class ClaimInspectingCarrier:
    def __init__(self, database: Database, scope: OperationScope) -> None:
        self._carrier = LocalCarrier()
        self._database = database
        self._scope = scope
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        claim = self._database.operations.inspect(self._scope)
        assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
        self.calls += 1
        return self._carrier.execute(invocation, io=io, deadline=deadline)


class RestorePublicationBundleCarrier:
    def __init__(self, restore_after_call: int, normal_bundle, monkeypatch: pytest.MonkeyPatch) -> None:
        self._carrier = LocalCarrier()
        self._restore_after_call = restore_after_call
        self._normal_bundle = normal_bundle
        self._monkeypatch = monkeypatch
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        report = self._carrier.execute(invocation, io=io, deadline=deadline)
        if self.calls == self._restore_after_call:
            self._monkeypatch.setattr(publication_exchange, "FIXED_BUNDLE", self._normal_bundle)
        return report


class _DiscardSink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


def _write(sink: ByteSink, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)
        assert written is not None and written > 0
        remaining = remaining[written:]


class ExpiringSource:
    def __init__(self, deadline: Deadline) -> None:
        self.deadline = deadline

    def try_read(self, limit: int) -> None:
        del limit
        object.__setattr__(self.deadline, "expires_at", time.monotonic())
        return None


@pytest.fixture
def plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set((*os.getgroups(), gid))))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


@pytest.fixture(autouse=True)
def fixture_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stage_bundle(monkeypatch)
    install_publication_bundle(monkeypatch)


@pytest.mark.parametrize(
    "content",
    [b"", bytes(range(256)) * 97],
    ids=["empty", "binary-multichunk"],
)
def test_real_helper_create_streams_exact_bytes_once(
    tmp_path: Path,
    plan: IdentityPlan,
    content: bytes,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    source = BytesSource(content, pieces=(1, 37, 8191, 12_288))
    try:
        outcome = _upload(borrow, root, source, len(content), plan)

        assert outcome.status is FileUploadStatus.COMPLETE
        assert outcome.publication_confirmed and not outcome.requires_owner_retention
        assert outcome.bytes_consumed == len(content)
        assert outcome.staged_bytes == len(content)
        assert outcome.runtime_prerequisite is not None
        assert outcome.runtime_prerequisite.state is RuntimePrerequisiteState.READY
        assert root.joinpath("target").read_bytes() == content
        assert source.offset == len(content) and not source.closed
        assert all(limit <= 12 * 1024 for limit in source.limits)
        borrow.close()
        owner.close()
        assert database.operations.inspect(owner.ownership.scope) is None
    finally:
        database.close()


def test_owner_is_durably_marked_once_before_each_real_carrier_execute(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    repository = database.operations
    scope = OperationScope(OperationResourceKind.VM, "upload-vm")
    owner = OperationOwner.acquire(repository, scope, "file-upload")
    borrow = owner.borrow()
    carrier = ClaimInspectingCarrier(database, owner.ownership.scope)
    marks = 0
    original = repository.mark_possible_dispatch

    def mark_possible_dispatch(ownership):
        nonlocal marks
        marks += 1
        return original(ownership)

    monkeypatch.setattr(repository, "mark_possible_dispatch", mark_possible_dispatch)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert outcome.status is FileUploadStatus.COMPLETE
        assert carrier.calls == 4
        assert marks == 1
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_real_helper_replace_and_match_use_the_same_owner_without_overlapping_borrows(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        created = _upload(borrow, root, BytesSource(b"first"), 5, plan)
        replaced = _upload(borrow, root, BytesSource(b"second"), 6, plan, condition=Replace())
        assert replaced.revision is not None
        matched = _upload(
            borrow,
            root,
            BytesSource(b"third"),
            5,
            plan,
            condition=Match(replaced.revision),
        )

        assert created.status is replaced.status is matched.status is FileUploadStatus.COMPLETE
        assert root.joinpath("target").read_bytes() == b"third"
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_lost_creation_reply_reconciles_and_cleans_without_replaying_begin(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LostCallStdoutCarrier(1)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 3
        assert outcome.status is FileUploadStatus.FAILED
        assert outcome.failure is FileUploadFailure.OBSERVATION
        assert outcome.scratch_cleanup_debt is None
        assert not outcome.requires_owner_retention and not root.joinpath("target").exists()
        borrow.close()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize(
    "state",
    [
        RuntimePrerequisiteState.MISSING,
        RuntimePrerequisiteState.SHIM,
        RuntimePrerequisiteState.UNUSABLE,
        RuntimePrerequisiteState.UNSUPPORTED_VERSION,
        RuntimePrerequisiteState.MISSING_MODULES,
    ],
)
def test_known_runtime_refusal_stops_before_pointless_reconciliation(
    tmp_path: Path,
    plan: IdentityPlan,
    state: RuntimePrerequisiteState,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = RuntimeRefusalOnCallCarrier(1, state)
    selected_runtime = RuntimeSelection(
        RuntimeTargetOS.DARWIN if state is RuntimePrerequisiteState.SHIM else RuntimeTargetOS.LINUX,
        sys.executable,
    )
    try:
        outcome = _upload(
            borrow,
            root,
            BytesSource(b"content"),
            7,
            plan,
            carrier=carrier,
            selected_runtime=selected_runtime,
        )

        assert carrier.calls == 1
        assert outcome.failure is FileUploadFailure.RUNTIME_PREREQUISITE
        assert outcome.runtime_prerequisite is not None
        assert outcome.runtime_prerequisite.state is state
        assert not outcome.stage_ownership_uncertain
        assert not outcome.requires_owner_retention
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_known_runtime_refusal_replaces_prior_unknown_without_erasing_ownership_uncertainty(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LostThenRuntimeRefusalCarrier()
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 2
        assert outcome.runtime_prerequisite is not None
        assert outcome.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
        assert outcome.stage_ownership_uncertain
        assert outcome.requires_owner_retention
    finally:
        database.close()


def test_runtime_refusal_after_source_read_preserves_debt_and_distinct_counters(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = RuntimeRefusalOnCallCarrier(2, RuntimePrerequisiteState.MISSING)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 2
        assert outcome.failure is FileUploadFailure.RUNTIME_PREREQUISITE
        assert outcome.bytes_consumed == 7
        assert outcome.staged_bytes == 0
        assert outcome.runtime_prerequisite is not None
        assert outcome.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
        assert outcome.scratch_cleanup_debt is not None
        assert outcome.requires_owner_retention
    finally:
        database.close()


def test_lost_publish_reply_never_replays_mutation_and_preserves_unknown_cleanup(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LostCallStdoutCarrier(3)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 4
        assert root.joinpath("target").read_bytes() == b"content"
        assert outcome.status is FileUploadStatus.UNCERTAIN
        assert outcome.publication_uncertain and not outcome.publication_confirmed
        assert outcome.publication_ownership_uncertain
        assert outcome.reference is not None and outcome.scratch_cleanup_debt is not None
        assert outcome.requires_owner_retention
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    finally:
        database.close()


def test_nonzero_wrapper_exit_records_effect_but_stops_all_follow_on_calls(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = NonzeroCallCarrier(3)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 3
        assert root.joinpath("target").read_bytes() == b"content"
        assert outcome.publication_confirmed
        assert outcome.pending_remote_effects
        assert outcome.requires_owner_retention
        with pytest.raises(StateError):
            owner.borrow()
        with pytest.raises(StateError):
            borrow.close()
            owner.close()
    finally:
        database.close()


def test_missing_completion_with_valid_transcript_stops_follow_on_calls_and_retains_attempt(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = MissingCompletionCallCarrier(2)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 2
        assert not root.joinpath("target").exists()
        assert outcome.failure is FileUploadFailure.TERMINATION
        assert outcome.pending_remote_effects
        assert outcome.bytes_consumed == 7
        assert outcome.staged_bytes == 7
        assert outcome.reference is not None and outcome.scratch_cleanup_debt is not None
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


def test_carrier_exception_after_execute_boundary_stops_follow_on_and_retains_owner(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = RaisingCallCarrier(2)
    try:
        with pytest.raises(RuntimeError) as raised:
            _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        fact = raised.value.__cause__
        assert isinstance(fact, FileUploadControlFact)
        assert carrier.calls == 2
        assert fact.outcome.pending_remote_effects
        assert fact.outcome.scratch_cleanup_debt is not None
        assert fact.outcome.requires_owner_retention
        with pytest.raises(StateError):
            borrow.close()
            owner.close()
    finally:
        database.close()


def test_interrupted_dispatch_records_expired_deadline_fact(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = DeadlineInterruptingCarrier()
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        fact = raised.value.__cause__
        assert isinstance(fact, FileUploadControlFact)
        assert carrier.calls == 1
        assert fact.outcome.deadline_exceeded
        assert fact.outcome.pending_remote_effects
        assert fact.outcome.requires_owner_retention
    finally:
        database.close()


def test_deadline_exhaustion_after_stage_uses_no_fresh_cleanup_budget(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    deadline = Deadline.after(30)
    try:
        outcome = _upload(borrow, root, ExpiringSource(deadline), 1, plan, deadline=deadline)

        assert outcome.failure is FileUploadFailure.DEADLINE
        assert outcome.deadline_exceeded and outcome.scratch_cleanup_debt is not None
        assert outcome.requires_owner_retention
        assert not root.joinpath("target").exists()
    finally:
        database.close()


def test_real_carrier_timeout_records_deadline_with_unresolved_stage_begin(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_exchange, "FIXED_BUNDLE", stage_fixture_source("import time; time.sleep(1)"))
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = AdmittedTimeoutCarrier(monkeypatch, startup_delay=0.15)
    try:
        outcome = _upload(
            borrow,
            root,
            BytesSource(b"content"),
            7,
            plan,
            carrier=carrier,
            deadline=Deadline.after(30),
        )

        assert carrier.calls == 1 and carrier.admitted
        assert outcome.status is FileUploadStatus.UNCERTAIN
        assert outcome.failure is FileUploadFailure.TERMINATION
        assert outcome.deadline_exceeded and outcome.pending_remote_effects
        assert outcome.bytes_consumed == 0
        assert outcome.requires_owner_retention
        assert not root.joinpath("target").exists()
    finally:
        database.close()


@pytest.mark.parametrize(
    "failure",
    [
        FileStageFailureControl(FileStageFailureCode.DEADLINE),
        FileStageFailureControl(
            FileStageFailureCode.SCRATCH,
            ScratchFailureKind.DEADLINE,
            ScratchPhase.BEGIN,
        ),
    ],
    ids=["stage-code", "scratch-kind"],
)
def test_closed_stage_deadline_transcript_stops_without_fresh_cleanup(
    tmp_path: Path,
    plan: IdentityPlan,
    failure: FileStageFailureControl,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = ClosedFailureOnCallCarrier(
        1,
        encode_file_stage_failure(failure),
        empty_file_stage_body(),
    )
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 1
        assert outcome.failure is FileUploadFailure.DEADLINE
        assert outcome.deadline_exceeded
        assert outcome.stage_failure == failure
        assert not outcome.requires_owner_retention
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_closed_publication_deadline_code_retains_scratch_without_cleanup(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    failure = FilePublicationFailureControl(FilePublicationFailureCode.DEADLINE)
    carrier = ClosedFailureOnCallCarrier(
        3,
        encode_file_publication_failure(failure),
        empty_file_publication_body(),
    )
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 3
        assert outcome.failure is FileUploadFailure.DEADLINE
        assert outcome.deadline_exceeded
        assert outcome.publication_failure == failure
        assert outcome.scratch_cleanup_debt is not None
        assert outcome.requires_owner_retention
    finally:
        database.close()


def test_real_helper_publication_deadline_retains_scratch_without_cleanup(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_publication_bundle(monkeypatch, _PUBLICATION_DEADLINE)
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 3
        assert outcome.failure is FileUploadFailure.DEADLINE
        assert outcome.deadline_exceeded
        assert outcome.publication_failure is not None
        assert outcome.publication_failure.publication_kind is PublicationFailureKind.DEADLINE
        assert outcome.scratch_cleanup_debt is not None
        assert outcome.requires_owner_retention
    finally:
        database.close()


def test_publish_failure_cleans_publication_debt_before_ordinary_scratch(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_bundle = install_publication_bundle(monkeypatch)
    install_publication_bundle(monkeypatch, _LEAVE_EXACT_PUBLICATION_DEBT)
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = RestorePublicationBundleCarrier(3, normal_bundle, monkeypatch)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 5
        assert outcome.failure is FileUploadFailure.PUBLICATION
        assert outcome.publication_cleanup_debt is None
        assert outcome.scratch_cleanup_debt is None
        assert not outcome.requires_owner_retention and not root.joinpath("target").exists()
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_failed_publication_cleanup_preserves_both_bound_debts(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_publication_bundle(monkeypatch, _LEAVE_EXACT_PUBLICATION_DEBT)
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 4
        assert outcome.failure is FileUploadFailure.PUBLICATION
        assert outcome.publication_cleanup_debt is not None
        assert outcome.scratch_cleanup_debt is not None
        assert outcome.reference is not None and outcome.requires_owner_retention
        assert not root.joinpath("target").exists()
    finally:
        database.close()


def test_progressed_publication_cleanup_binding_loss_clears_stale_debt(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progressed_bundle = install_publication_bundle(monkeypatch, _PROGRESS_CLEANUP_THEN_LOSE_BINDING_IDENTITY)
    install_publication_bundle(monkeypatch, _LEAVE_EXACT_PUBLICATION_DEBT)
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = RestorePublicationBundleCarrier(3, progressed_bundle, monkeypatch)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 4
        assert outcome.publication_cleanup_debt is None
        assert outcome.publication_ownership_uncertain
        assert outcome.publication_failure is not None
        assert outcome.publication_failure.cleanup_state is PublicationCleanupState.OWNERSHIP_UNCERTAIN
        assert not root.joinpath(publication_stage_name(outcome.token)).exists()
        assert root.joinpath(scratch_name(outcome.token)).is_dir()
        assert outcome.requires_owner_retention
    finally:
        database.close()


def test_publication_cleanup_receipt_deadline_preserves_exact_debt_and_stops(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_bundle = install_publication_bundle(monkeypatch, _PUBLICATION_CLEANUP_DEADLINE)
    install_publication_bundle(monkeypatch, _LEAVE_EXACT_PUBLICATION_DEBT)
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = RestorePublicationBundleCarrier(3, deadline_bundle, monkeypatch)
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 4
        assert outcome.failure is FileUploadFailure.PUBLICATION
        assert outcome.deadline_exceeded
        assert outcome.publication_failure is not None
        assert outcome.publication_failure.receipt_kind is PublicationReceiptFailureKind.DEADLINE
        assert outcome.publication_cleanup_debt is not None
        assert outcome.scratch_cleanup_debt is not None
        assert outcome.requires_owner_retention
    finally:
        database.close()


def test_partial_stage_creation_failure_uses_failure_debt_for_cleanup(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_exchange, "FIXED_BUNDLE", stage_fixture_source(_PARTIAL_STAGE_BEGIN))
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 2
        assert outcome.failure is FileUploadFailure.STAGE
        assert outcome.stage_failure is not None
        assert outcome.stage_failure.cleanup_debt is not None
        assert outcome.scratch_cleanup_debt is None
        assert not root.joinpath(scratch_name(outcome.token)).exists()
        assert not outcome.requires_owner_retention
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_failed_cleanup_after_partial_stage_creation_preserves_failure_debt(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_exchange,
        "FIXED_BUNDLE",
        stage_fixture_source(_PARTIAL_STAGE_BEGIN + _FAIL_STAGE_CLEANUP),
    )
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 2
        assert outcome.failure is FileUploadFailure.STAGE
        assert outcome.stage_failure is not None
        assert outcome.stage_failure.cleanup_debt == outcome.scratch_cleanup_debt
        assert root.joinpath(scratch_name(outcome.token)).is_dir()
        assert outcome.requires_owner_retention
    finally:
        database.close()
