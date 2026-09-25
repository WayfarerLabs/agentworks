"""Input, source, and local-preparation behavior for private uploads."""

from __future__ import annotations

import dis
import os
import stat
import sys
from pathlib import Path

import pytest

import agentworks.execution._file_publication_exchange as publication_exchange
from agentworks.db import Database, OperationClaimState
from agentworks.errors import StateError, ValidationError
from agentworks.execution._file_publication import Create, Match
from agentworks.execution._file_publication_protocol import FilePublicationFailureCode, FilePublicationRequestError
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._file_upload import FileUploadControlFact, FileUploadFailure, FileUploadStatus, upload_file
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._scratch_receipt import scratch_name
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import NewMetadata
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._file_publication_support import install_fixture_bundle as install_publication_bundle
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage_bundle
from tests.execution.files._file_upload_support import BytesSource, new_metadata, upload
from tests.execution.files._file_upload_support import owner as operation_owner
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private upload helper requires Linux")


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
        del limit
        self.calls += 1
        if self.calls == 1:
            return self.value
        return b""


class CancelingSource:
    def __init__(self) -> None:
        self.closed = False

    def try_read(self, limit: int) -> bytes:
        del limit
        raise KeyboardInterrupt

    def close(self) -> None:
        self.closed = True


class FailingSourceGetter:
    @property
    def try_read(self):
        raise RuntimeError("getter-secret-canary")


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
    ("source", "size", "expected_consumed"),
    [
        (BytesSource(b"short"), 6, 5),
        (BytesSource(b"excess"), 5, 6),
        (ContractSource(b"x" * (12 * 1024 + 1)), 20_000, 12 * 1024 + 1),
        (ContractSource("not-bytes"), 1, 0),
    ],
    ids=["short", "excess", "oversized-response", "nonbytes-response"],
)
def test_source_contract_failures_cleanup_exact_scratch(
    tmp_path: Path,
    plan: IdentityPlan,
    source,
    size: int,
    expected_consumed: int,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = operation_owner(database)
    borrow = owner.borrow()
    try:
        outcome = upload(borrow, root, source, size, plan)

        assert outcome.status is FileUploadStatus.FAILED
        assert outcome.failure is FileUploadFailure.SOURCE_CONTRACT
        assert outcome.bytes_consumed == expected_consumed
        assert outcome.scratch_cleanup_debt is None and not outcome.requires_owner_retention
        assert not root.joinpath("target").exists()
        borrow.close()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
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
    owner = operation_owner(database)
    borrow = owner.borrow()
    source = FailingSource()
    try:
        outcome = upload(borrow, root, source, 1, plan)

        assert outcome.failure is FileUploadFailure.SOURCE
        assert "source-secret-canary" not in repr(outcome)
        assert outcome.scratch_cleanup_debt is None and not source.closed
        borrow.close()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
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
    owner = operation_owner(database)
    borrow = owner.borrow()
    source = CancelingSource()
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            upload(borrow, root, source, 1, plan)

        fact = raised.value.__cause__
        assert isinstance(fact, FileUploadControlFact)
        assert fact.outcome.scratch_cleanup_debt is None
        assert not fact.outcome.requires_owner_retention and not source.closed
        borrow.close()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("trusted_root", "source"),
    [
        ("/approved/\udcff", BytesSource(b"x")),
        ("/approved", FailingSourceGetter()),
    ],
    ids=["invalid-utf8-path", "failing-source-getter"],
)
def test_validation_failure_does_not_retain_sensitive_exception_context(
    tmp_path: Path,
    plan: IdentityPlan,
    trusted_root: str,
    source,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = operation_owner(database)
    borrow = owner.borrow()
    try:
        with pytest.raises(ValidationError) as raised:
            upload_file(
                LocalCarrier(),
                trusted_root_path=trusted_root,
                relative_path="target",
                source=source,
                size=1,
                condition=Create(),
                create_metadata=new_metadata(),
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                borrow=borrow,
            )

        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        with pytest.raises(StateError):
            owner.borrow()
        borrow.close()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("condition", "create_metadata"),
    [
        (Create(), NewMetadata("owner", "group", 0o1000)),
        (
            Match(FileRevision(FileStat(1, 0, stat.S_IFREG | 0o600, 1, 1001, 1002, 1, 1, 1))),
            NewMetadata("owner", "group", 0o600),
        ),
        (
            Match(FileRevision(FileStat(1, 2, stat.S_IFDIR | 0o700, 1, 1001, 1002, 0, 1, 1))),
            NewMetadata("owner", "group", 0o600),
        ),
    ],
    ids=["unsupported-regular-mode", "malformed-match", "nonregular-match"],
)
def test_publication_schema_refusal_preserves_caller_borrow_and_precedes_carrier_and_source(
    tmp_path: Path,
    plan: IdentityPlan,
    condition: Create | Match,
    create_metadata: NewMetadata,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = operation_owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    source = BytesSource(b"secret")
    try:
        with pytest.raises(ValidationError) as raised:
            upload_file(
                carrier,
                trusted_root_path=str(root),
                relative_path="target",
                source=source,
                size=6,
                condition=condition,
                create_metadata=create_metadata,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                borrow=borrow,
            )

        assert raised.value.__cause__ is None and raised.value.__context__ is None
        assert carrier.calls == 0 and source.calls == 0
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        with pytest.raises(StateError):
            owner.close()
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_oversized_stage_preparation_never_arms_owner_or_executes_carrier(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = operation_owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    source = BytesSource(b"x")
    gid = os.getegid()
    groups = tuple(sorted(set(range(9_000)) | {gid}))
    oversized_plan = IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)
    try:
        with pytest.raises(ValidationError) as raised:
            upload(borrow, root, source, 1, oversized_plan, carrier=carrier)

        assert raised.value.__cause__ is None
        assert carrier.calls == 0 and source.calls == 0
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_interrupt_at_owned_carrier_handoff_exports_coordination_uncertainty(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = operation_owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    target_instruction = next(
        instruction
        for instruction in dis.get_instructions(BorrowedFixedHelperCarrier.execute)
        if instruction.opname == "STORE_ATTR" and instruction.argval == "outstanding_attempt"
    )
    assert target_instruction.positions is not None
    target_line = target_instruction.positions.lineno
    assert target_line is not None
    interrupted = False

    def interrupt_at_handoff(frame, event, arg):
        nonlocal interrupted
        del arg
        if (
            frame.f_code is BorrowedFixedHelperCarrier.execute.__code__
            and event == "line"
            and frame.f_lineno == target_line
        ):
            interrupted = True
            raise KeyboardInterrupt
        return interrupt_at_handoff

    previous_trace = sys.gettrace()
    try:
        sys.settrace(interrupt_at_handoff)
        with pytest.raises(KeyboardInterrupt) as raised:
            upload(borrow, root, BytesSource(b"content"), 7, plan, carrier=carrier)
    finally:
        sys.settrace(previous_trace)

    try:
        fact = raised.value.__cause__
        assert interrupted
        assert isinstance(fact, FileUploadControlFact)
        assert carrier.calls == 0
        assert not fact.outcome.pending_remote_effects
        assert fact.outcome.coordination_uncertain
        assert fact.outcome.requires_owner_retention
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
        with pytest.raises(StateError):
            owner.close()
        borrow.handoff_unresolved()
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()


def test_local_publication_preparation_failure_cleans_completed_stage(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = operation_owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    source = BytesSource(b"content")

    def fail_encode(request):
        del request
        raise FilePublicationRequestError(FilePublicationFailureCode.OVERSIZED_REQUEST)

    monkeypatch.setattr(publication_exchange, "encode_file_publication_request", fail_encode)
    try:
        with pytest.raises(ValidationError) as raised:
            upload(borrow, root, source, 7, plan, carrier=carrier)

        fact = raised.value.__cause__
        assert isinstance(fact, FileUploadControlFact)
        assert fact.outcome.scratch_cleanup_debt is None
        assert not fact.outcome.pending_remote_effects
        assert not fact.outcome.requires_owner_retention
        assert carrier.calls == 4 and source.offset == 7
        assert not root.joinpath(scratch_name(fact.outcome.token)).exists()
        borrow.close()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_invalid_input_preserves_borrow_and_refuses_carrier_effects(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = operation_owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError):
            upload(borrow, root, BytesSource(b"x"), -1, plan, carrier=carrier)

        assert carrier.calls == 0
        claim = database.operations.inspect(owner.ownership.scope)
        assert claim is not None and claim.state is OperationClaimState.RESERVED
        with pytest.raises(StateError):
            owner.borrow()
        borrow.close()
        owner.close()
    finally:
        database.close()
