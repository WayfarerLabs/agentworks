"""Complete private upload composition under core operation ownership."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

import agentworks.execution._file_publication_exchange as publication_exchange
from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._file_publication import Create, CreateMetadata, Match, Replace
from agentworks.execution._file_upload import (
    FileUploadControlFact,
    FileUploadFailure,
    FileUploadStatus,
    upload_file,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    ExitStatus,
    SinkOutput,
)
from agentworks.operations import OperationOwner
from tests.execution.files._file_publication_support import (
    LocalCarrier,
)
from tests.execution.files._file_publication_support import (
    install_fixture_bundle as install_publication_bundle,
)
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage_bundle
from tests.execution.files._runtime_support import runtime_selection

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


class BytesSource:
    def __init__(self, data: bytes, *, pieces: tuple[int, ...] = ()) -> None:
        self.data = data
        self.pieces = pieces
        self.offset = 0
        self.calls = 0
        self.limits: list[int] = []
        self.closed = False

    def try_read(self, limit: int) -> bytes:
        self.calls += 1
        self.limits.append(limit)
        if self.offset == len(self.data):
            return b""
        piece = self.pieces[self.calls - 1] if self.calls <= len(self.pieces) else limit
        amount = min(limit, piece, len(self.data) - self.offset)
        result = self.data[self.offset : self.offset + amount]
        self.offset += amount
        return result

    def close(self) -> None:
        self.closed = True


class LostFirstStdoutCarrier:
    def __init__(self) -> None:
        self._carrier = LocalCarrier()
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        if self.calls != 1:
            return self._carrier.execute(invocation, io=io, deadline=deadline)
        assert isinstance(io.output, SinkOutput)
        hidden = CarrierIO(
            input=io.input,
            output=SinkOutput(_DiscardSink(), io.output.stderr, require_live=False),
            sensitive=io.sensitive,
        )
        return self._carrier.execute(invocation, io=hidden, deadline=deadline)


class LostCallStdoutCarrier:
    def __init__(self, lost_call: int) -> None:
        self._carrier = LocalCarrier()
        self._lost_call = lost_call
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation, *, io, deadline) -> CarrierReport:
        self.calls += 1
        if self.calls != self._lost_call:
            return self._carrier.execute(invocation, io=io, deadline=deadline)
        assert isinstance(io.output, SinkOutput)
        hidden = CarrierIO(
            input=io.input,
            output=SinkOutput(_DiscardSink(), io.output.stderr, require_live=False),
            sensitive=io.sensitive,
        )
        return self._carrier.execute(invocation, io=hidden, deadline=deadline)


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


class FailingSource:
    def __init__(self) -> None:
        self.closed = False

    def try_read(self, limit: int) -> bytes:
        del limit
        raise RuntimeError("source-secret-canary")

    def close(self) -> None:
        self.closed = True


class ContractSource:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    def try_read(self, limit: int):
        self.calls += 1
        if self.calls == 1:
            return self.value
        return b""


class ExpiringSource:
    def __init__(self, deadline: Deadline) -> None:
        self.deadline = deadline

    def try_read(self, limit: int) -> None:
        del limit
        object.__setattr__(self.deadline, "expires_at", time.monotonic())
        return None


class CancelingSource:
    def __init__(self) -> None:
        self.closed = False

    def try_read(self, limit: int) -> bytes:
        del limit
        raise KeyboardInterrupt

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set((*os.getgroups(), gid))))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


@pytest.fixture(autouse=True)
def fixture_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stage_bundle(monkeypatch)
    install_publication_bundle(monkeypatch)


def _owner(database: Database) -> OperationOwner:
    return OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "upload-vm"),
        "file-upload",
    )


def _upload(
    owner: OperationOwner,
    root: Path,
    source,
    size: int,
    plan: IdentityPlan,
    *,
    carrier=None,
    condition=None,
    deadline: Deadline | None = None,
):
    return upload_file(
        carrier or LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        source=source,
        size=size,
        condition=condition or Create(),
        create_metadata=CreateMetadata(os.geteuid(), os.getegid(), 0o640),
        plan=plan,
        deadline=deadline or Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        owner=owner,
    )


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
    source = BytesSource(content, pieces=(1, 37, 8191, 12_288))
    try:
        outcome = _upload(owner, root, source, len(content), plan)

        assert outcome.status is FileUploadStatus.COMPLETE
        assert outcome.publication_confirmed and not outcome.owner_retained
        assert outcome.bytes_consumed == len(content)
        assert root.joinpath("target").read_bytes() == content
        assert source.offset == len(content) and not source.closed
        assert all(limit <= 12 * 1024 for limit in source.limits)
        owner.close()
        assert database.operations.inspect(owner.ownership.scope) is None
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
    try:
        created = _upload(owner, root, BytesSource(b"first"), 5, plan)
        replaced = _upload(owner, root, BytesSource(b"second"), 6, plan, condition=Replace())
        assert replaced.revision is not None
        matched = _upload(
            owner,
            root,
            BytesSource(b"third"),
            5,
            plan,
            condition=Match(replaced.revision),
        )

        assert created.status is replaced.status is matched.status is FileUploadStatus.COMPLETE
        assert root.joinpath("target").read_bytes() == b"third"
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
    carrier = LostFirstStdoutCarrier()
    try:
        outcome = _upload(owner, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 3
        assert outcome.status is FileUploadStatus.FAILED
        assert outcome.failure is FileUploadFailure.OBSERVATION
        assert outcome.scratch_cleanup_debt is None
        assert not outcome.owner_retained and not root.joinpath("target").exists()
        owner.close()
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
    carrier = LostCallStdoutCarrier(3)
    try:
        outcome = _upload(owner, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 4
        assert root.joinpath("target").read_bytes() == b"content"
        assert outcome.status is FileUploadStatus.UNCERTAIN
        assert outcome.publication_uncertain and not outcome.publication_confirmed
        assert outcome.publication_ownership_uncertain
        assert outcome.reference is not None and outcome.scratch_cleanup_debt is not None
        assert outcome.owner_retained
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
    carrier = NonzeroCallCarrier(3)
    try:
        outcome = _upload(owner, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 3
        assert root.joinpath("target").read_bytes() == b"content"
        assert outcome.publication_confirmed
        assert outcome.pending_remote_effects and outcome.outstanding_attempt is not None
        assert outcome.owner_retained
        with pytest.raises(StateError):
            owner.borrow()
        with pytest.raises(StateError):
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
    carrier = MissingCompletionCallCarrier(2)
    try:
        outcome = _upload(owner, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 2
        assert not root.joinpath("target").exists()
        assert outcome.failure is FileUploadFailure.TERMINATION
        assert outcome.pending_remote_effects and outcome.outstanding_attempt is not None
        assert outcome.reference is not None and outcome.scratch_cleanup_debt is not None
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("source", "size"),
    [
        (BytesSource(b"short"), 6),
        (BytesSource(b"excess"), 5),
        (ContractSource(b"x" * (12 * 1024 + 1)), 20_000),
        (ContractSource("not-bytes"), 1),
    ],
    ids=["short", "excess", "oversized-response", "nonbytes-response"],
)
def test_source_contract_failures_cleanup_exact_scratch(
    tmp_path: Path,
    plan: IdentityPlan,
    source,
    size: int,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    try:
        outcome = _upload(owner, root, source, size, plan)

        assert outcome.status is FileUploadStatus.FAILED
        assert outcome.failure is FileUploadFailure.SOURCE_CONTRACT
        assert outcome.scratch_cleanup_debt is None and not outcome.owner_retained
        assert not root.joinpath("target").exists()
        owner.close()
    finally:
        database.close()


def test_source_exception_is_sanitized_and_borrowed_source_is_not_closed(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    source = FailingSource()
    try:
        outcome = _upload(owner, root, source, 1, plan)

        assert outcome.failure is FileUploadFailure.SOURCE
        assert "source-secret-canary" not in repr(outcome)
        assert outcome.scratch_cleanup_debt is None and not source.closed
        owner.close()
    finally:
        database.close()


def test_source_cancellation_propagates_with_bounded_cleanup_facts(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    source = CancelingSource()
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _upload(owner, root, source, 1, plan)

        fact = raised.value.__cause__
        assert isinstance(fact, FileUploadControlFact)
        assert fact.outcome.scratch_cleanup_debt is None
        assert not fact.outcome.owner_retained and not source.closed
        owner.close()
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
    deadline = Deadline.after(30)
    try:
        outcome = _upload(owner, root, ExpiringSource(deadline), 1, plan, deadline=deadline)

        assert outcome.failure is FileUploadFailure.DEADLINE
        assert outcome.deadline_exceeded and outcome.scratch_cleanup_debt is not None
        assert outcome.owner_retained
        assert not root.joinpath("target").exists()
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
    carrier = RestorePublicationBundleCarrier(3, normal_bundle, monkeypatch)
    try:
        outcome = _upload(owner, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 5
        assert outcome.failure is FileUploadFailure.PUBLICATION
        assert outcome.publication_cleanup_debt is None
        assert outcome.scratch_cleanup_debt is None
        assert not outcome.owner_retained and not root.joinpath("target").exists()
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
    carrier = LocalCarrier()
    try:
        outcome = _upload(owner, root, BytesSource(b"content"), 7, plan, carrier=carrier)

        assert carrier.calls == 4
        assert outcome.failure is FileUploadFailure.PUBLICATION
        assert outcome.publication_cleanup_debt is not None
        assert outcome.scratch_cleanup_debt is not None
        assert outcome.reference is not None and outcome.owner_retained
        assert not root.joinpath("target").exists()
    finally:
        database.close()


def test_invalid_input_refuses_before_borrow_or_carrier_effects(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError):
            _upload(owner, root, BytesSource(b"x"), -1, plan, carrier=carrier)

        assert carrier.calls == 0
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        borrow = owner.borrow()
        borrow.close()
        owner.close()
    finally:
        database.close()
