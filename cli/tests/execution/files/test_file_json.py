"""End-to-end behavior for private JSON-file composition."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import ExternalError, StateError, ValidationError
from agentworks.execution._account import FileOwnershipObservationState
from agentworks.execution._account_protocol import FileOwnershipFailure
from agentworks.execution._file_json import (
    FileJsonChange,
    FileJsonControlFact,
    FileJsonFailure,
    FileJsonStatus,
    JsonFileStrategy,
    update_json_file,
)
from agentworks.execution._file_object_exchange import stat_file
from agentworks.execution._file_read_protocol import FileReadFailure
from agentworks.execution._file_result_transfer import reduce_file_json
from agentworks.execution._file_upload import FileUploadFailure
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import (
    Carrier,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Failure,
    PreparedInvocation,
)
from agentworks.execution.files import FileFailureReason, FileOperationPhase, NewMetadata
from agentworks.operations import OperationBorrow, OperationOwner
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._file_publication_support import install_fixture_bundle as install_publication_bundle
from tests.execution.files._file_read_support import install_fixture_bundle as install_read_bundle
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage_bundle
from tests.execution.files._file_upload_support import LostCallStdoutCarrier, new_metadata
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private file helpers require Linux")

_ALWAYS_MATCH_CONFLICT = """
publication=sys.modules['_agw_file_publication._file_publication']
def conflict(*args,**kwargs):
 raise publication.FilePublicationError(
  publication.PublicationFailureKind.CONFLICT,
  publication.PublicationPhase.CONDITION,
 )
publication._observe_replacement=conflict
"""
_METADATA_CONFLICT = """
publication=sys.modules['_agw_file_publication._file_publication']
def conflict(*args,**kwargs):
 raise publication.FilePublicationError(
  publication.PublicationFailureKind.CONFLICT,
  publication.PublicationPhase.METADATA,
 )
publication._prepare_replacement=conflict
"""


@dataclass
class AfterCallCarrier:
    after_call: Callable[[int, Deadline], None]
    calls: int = 0

    def __post_init__(self) -> None:
        self._carrier = LocalCarrier()

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
    ) -> CarrierReport:
        self.calls += 1
        report = self._carrier.execute(invocation, io=io, deadline=deadline)
        self.after_call(self.calls, deadline)
        return report


class InterruptingCarrier:
    def __init__(self, *, expire_deadline: bool = False) -> None:
        self.calls = 0
        self.expire_deadline = expire_deadline

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
    ) -> CarrierReport:
        del invocation, io
        self.calls += 1
        if self.expire_deadline:
            object.__setattr__(deadline, "expires_at", 0.0)
        raise KeyboardInterrupt


class ExpiredMissingCompletionCarrier:
    def __init__(self) -> None:
        self._carrier = LocalCarrier()
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
    ) -> CarrierReport:
        self.calls += 1
        report = self._carrier.execute(invocation, io=io, deadline=deadline)
        object.__setattr__(deadline, "expires_at", 0.0)
        return replace(report, completion=None, failure=Failure.DEADLINE)


class ConflictThenDeadlineCarrier:
    def __init__(self, target: Path) -> None:
        self._carrier = LocalCarrier()
        self._target = target
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
    ) -> CarrierReport:
        self.calls += 1
        report = self._carrier.execute(invocation, io=io, deadline=deadline)
        if self.calls == 1:
            self._target.write_text('{"concurrent":true}')
        elif self.calls == 6:
            object.__setattr__(deadline, "expires_at", 0.0)
            return replace(report, completion=None, failure=Failure.DEADLINE)
        return report


@pytest.fixture
def plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set((*os.getgroups(), gid))))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


@pytest.fixture(autouse=True)
def fixture_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stage_bundle(monkeypatch)
    install_publication_bundle(monkeypatch)
    install_read_bundle(monkeypatch)


def _owner(database: Database) -> OperationOwner:
    return OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "json-vm"),
        "file-json",
    )


def _update(
    borrow: OperationBorrow,
    root: Path,
    plan: IdentityPlan,
    source: bytes,
    strategy: JsonFileStrategy,
    *,
    carrier: Carrier | None = None,
    create: bool = True,
    max_bytes: int = 64 * 1024,
    max_depth: int = 64,
    runtime: str = sys.executable,
    metadata: NewMetadata | None = None,
):
    return update_json_file(
        carrier or LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        source=source,
        strategy=strategy,
        create=create,
        create_metadata=metadata or new_metadata(),
        max_bytes=max_bytes,
        max_depth=max_depth,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(runtime),
        borrow=borrow,
    )


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_invalid_source_refuses_before_target_io(
    tmp_path: Path,
    plan: IdentityPlan,
    strategy: JsonFileStrategy,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    (root / "target").write_bytes(b"private-existing-content")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError) as raised:
            _update(borrow, root, plan, b'{"duplicate":1,"duplicate":2}', strategy, carrier=carrier)

        assert raised.value.__context__ is None and raised.value.__cause__ is None
        assert "duplicate" not in repr(raised.value)
        assert carrier.calls == 0
        assert (root / "target").read_bytes() == b"private-existing-content"
        borrow.close()
        owner.close()
    finally:
        database.close()


def test_replace_uses_stat_without_parsing_existing_bytes(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"malformed \xff")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b'{"value":null}',
            "replace",
            carrier=carrier,
            metadata=NewMetadata("unused-json-owner", "unused-json-group", 0o600),
        )

        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.change is FileJsonChange.CHANGED and outcome.revision is not None
        assert outcome.publication_attempts == 1 and carrier.calls == 5
        assert outcome.upload_outcome is not None and outcome.upload_outcome.ownership_result is None
        assert json.loads(target.read_bytes()) == {"value": None}
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_publication_metadata_is_canonical_before_target_observation(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError):
            update_json_file(
                carrier,
                trusted_root_path=str(root),
                relative_path="target",
                source=b'{"source":true}',
                strategy="replace",
                create=True,
                create_metadata=new_metadata(0o1000),
                max_bytes=64 * 1024,
                max_depth=64,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                borrow=borrow,
            )

        assert carrier.calls == 0 and json.loads(target.read_bytes()) == {"existing": True}
        with pytest.raises(StateError):
            owner.borrow()
        borrow.close()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("existing", [b"", b"malformed \xff"])
def test_skip_existing_leaves_any_regular_file_unchanged(
    tmp_path: Path,
    plan: IdentityPlan,
    existing: bytes,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_bytes(existing)
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b'{"new":true}',
            "skip-existing",
            carrier=carrier,
            metadata=NewMetadata("unused-json-owner", "unused-json-group", 0o600),
        )

        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.change is FileJsonChange.UNCHANGED and outcome.revision is not None
        assert outcome.publication_attempts == 0 and carrier.calls == 1
        assert target.read_bytes() == existing
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_absent_destination_respects_create(
    tmp_path: Path,
    plan: IdentityPlan,
    strategy: JsonFileStrategy,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        outcome = _update(borrow, root, plan, b'{"created":true}', strategy, create=False)

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.ABSENT
        assert outcome.publication_attempts == 0 and not (root / "target").exists()
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_each_strategy_creates_an_absent_destination(
    tmp_path: Path,
    plan: IdentityPlan,
    strategy: JsonFileStrategy,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b'{"created":[null,{"nested":true}]}',
            strategy,
            carrier=carrier,
        )

        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.change is FileJsonChange.CHANGED and outcome.revision is not None
        assert outcome.upload_outcome is not None
        assert outcome.upload_outcome.ownership_result is not None
        assert outcome.upload_outcome.ownership_result.observation is not None
        assert outcome.upload_outcome.ownership_result.observation.state is FileOwnershipObservationState.RESOLVED
        assert json.loads((root / "target").read_bytes()) == {"created": [None, {"nested": True}]}
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_absent_creation_retains_nested_ownership_failure(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b'{"created":true}',
            "replace",
            metadata=NewMetadata("missing-json-owner", "missing-json-group", 0o600),
        )

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.UPLOAD
        upload = outcome.upload_outcome
        assert upload is not None and upload.failure is FileUploadFailure.OWNERSHIP
        assert upload.ownership_result is not None
        observation = upload.ownership_result.observation
        assert observation is not None and observation.failure is FileOwnershipFailure.MISSING_OWNER
        with pytest.raises(StateError) as raised:
            reduce_file_json(outcome, entity_kind="workspace file", entity_name="settings")
        assert raised.value.details is not None
        assert raised.value.details.phase is FileOperationPhase.OWNERSHIP_LOOKUP
        assert raised.value.details.reason is FileFailureReason.MISSING_OWNER
        assert not (root / "target").exists()
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (
            "merge-overwrite",
            {
                "array": ["source"],
                "nested": {"existing": True, "source": True, "value": None},
                "scalar": {"source": True},
            },
        ),
        (
            "merge-preserve",
            {
                "array": ["existing"],
                "nested": {"existing": True, "source": True, "value": 3},
                "scalar": None,
            },
        ),
    ],
)
def test_merge_uses_bounded_snapshot_and_atomic_leaf_semantics(
    tmp_path: Path,
    plan: IdentityPlan,
    strategy: JsonFileStrategy,
    expected: dict[str, object],
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_text(
        json.dumps(
            {
                "array": ["existing"],
                "nested": {"existing": True, "value": 3},
                "scalar": None,
            }
        )
    )
    source = json.dumps(
        {
            "array": ["source"],
            "nested": {"source": True, "value": None},
            "scalar": {"source": True},
        }
    ).encode()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            source,
            strategy,
            metadata=NewMetadata("unused-json-owner", "unused-json-group", 0o600),
        )

        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.change is FileJsonChange.CHANGED and outcome.revision is not None
        assert outcome.upload_outcome is not None and outcome.upload_outcome.ownership_result is None
        assert json.loads(target.read_bytes()) == expected
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("existing", [b"", b"[]", b'{"x":1,"x":2}', b"not-json"])
def test_merge_rejects_invalid_existing_without_publication(
    tmp_path: Path,
    plan: IdentityPlan,
    existing: bytes,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_bytes(existing)
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _update(borrow, root, plan, b"{}", "merge-overwrite", carrier=carrier)

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.EXISTING_VALIDATION
        assert outcome.publication_attempts == 0 and carrier.calls == 1
        assert target.read_bytes() == existing
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_valid_existing_json_beyond_depth_bound_does_not_publish(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    existing = b'{"nested":[[[[0]]]]}'
    target.write_bytes(existing)
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b"{}",
            "merge-overwrite",
            carrier=carrier,
            max_depth=4,
        )

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.EXISTING_VALIDATION
        assert outcome.publication_attempts == 0 and carrier.calls == 1
        assert target.read_bytes() == existing
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_valid_existing_json_beyond_integer_parser_capacity_does_not_publish(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    integer_limit = sys.get_int_max_str_digits()
    if integer_limit == 0:
        pytest.skip("interpreter integer conversion limit is disabled")
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    existing = b'{"integer":' + (b"9" * (integer_limit + 1)) + b"}"
    target.write_bytes(existing)
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b"{}",
            "merge-preserve",
            carrier=carrier,
            max_bytes=len(existing),
        )

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.EXISTING_VALIDATION
        assert outcome.publication_attempts == 0 and carrier.calls == 1
        assert target.read_bytes() == existing
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_merge_enforces_snapshot_byte_bound_without_partial_publication(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_text('{"existing":"value beyond bound"}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b"{}",
            "merge-overwrite",
            carrier=carrier,
            max_bytes=16,
        )

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.READ
        assert outcome.read_failure is FileReadFailure.LIMIT
        assert outcome.publication_attempts == 0 and carrier.calls == 1
        assert target.read_text() == '{"existing":"value beyond bound"}'
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_merge_reports_result_capacity_separately_from_invalid_existing(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_text('{"existing":"1234567890"}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b'{"source":"1234567890"}',
            "merge-overwrite",
            max_bytes=48,
        )

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.TRANSFORM
        assert outcome.publication_attempts == 0
        assert json.loads(target.read_bytes()) == {"existing": "1234567890"}
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_known_runtime_refusal_stops_before_upload(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b'{"value":1}',
            "merge-overwrite",
            carrier=carrier,
            runtime="/missing/agentworks-python",
        )

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.RUNTIME_PREREQUISITE
        assert outcome.runtime_prerequisite is not None
        assert outcome.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
        assert outcome.publication_attempts == 0 and carrier.calls == 1
        assert not outcome.requires_owner_retention
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_merge_retries_a_later_exact_match_conflict_with_one_deadline_and_borrow(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_text('{"initial":true}')
    seen_deadlines: list[Deadline] = []

    def race_once(call: int, deadline: Deadline) -> None:
        seen_deadlines.append(deadline)
        if call == 1:
            target.write_text('{"concurrent":true}')

    carrier = AfterCallCarrier(race_once)
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b'{"source":{"nested":null}}',
            "merge-overwrite",
            carrier=carrier,
        )

        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.publication_attempts == 2
        assert json.loads(target.read_bytes()) == {"concurrent": True, "source": {"nested": None}}
        assert len({id(deadline) for deadline in seen_deadlines}) == 1
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_merge_retry_read_keeps_its_deadline_over_the_prior_conflict(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_text('{"initial":true}')
    carrier = ConflictThenDeadlineCarrier(target)
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        outcome = _update(borrow, root, plan, b'{"source":true}', "merge-overwrite", carrier=carrier)

        assert carrier.calls == 6 and outcome.publication_attempts == 1
        assert outcome.status is FileJsonStatus.UNCERTAIN
        assert outcome.failure is FileJsonFailure.TERMINATION
        assert outcome.carrier_failure is Failure.DEADLINE
        assert outcome.deadline_exceeded and outcome.pending_remote_effects
        assert outcome.requires_owner_retention
        upload = outcome.upload_outcome
        assert upload is not None and upload.failure is FileUploadFailure.PUBLICATION
        with pytest.raises(ExternalError) as raised:
            reduce_file_json(outcome, entity_kind="workspace file", entity_name="settings")
        assert raised.value.details is not None
        assert raised.value.details.phase is FileOperationPhase.OBSERVATION
        assert raised.value.details.reason is FileFailureReason.DEADLINE
        assert target.read_text() == '{"concurrent":true}'
    finally:
        database.close()


def test_merge_stops_after_eight_total_condition_conflicts(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_publication_bundle(monkeypatch, _ALWAYS_MATCH_CONFLICT)
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        outcome = _update(borrow, root, plan, b'{"source":true}', "merge-overwrite")

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.CONFLICT
        assert outcome.publication_attempts == 8
        assert not outcome.requires_owner_retention
        assert json.loads(target.read_bytes()) == {"existing": True}
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_merge_does_not_retry_a_noncondition_publication_conflict(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_publication_bundle(monkeypatch, _METADATA_CONFLICT)
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    try:
        outcome = _update(borrow, root, plan, b'{"source":true}', "merge-overwrite")

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.UPLOAD
        assert outcome.publication_attempts == 1
        assert json.loads(target.read_bytes()) == {"existing": True}
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_whole_json_call_holds_borrow_against_sibling_upload(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    sibling_refused = False

    def try_sibling(call: int, deadline: Deadline) -> None:
        nonlocal sibling_refused
        del deadline
        if call != 1:
            return
        with pytest.raises(StateError):
            owner.borrow()
        sibling_refused = True

    try:
        outcome = _update(
            borrow,
            root,
            plan,
            b'{"primary":true}',
            "replace",
            carrier=AfterCallCarrier(try_sibling),
        )

        assert outcome.status is FileJsonStatus.COMPLETE and sibling_refused
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_read_and_upload_share_one_durable_dispatch_transition(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    (root / "target").write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    repository = database.operations
    original = repository.mark_possible_dispatch
    marks = 0

    def record_mark(ownership):
        nonlocal marks
        marks += 1
        return original(ownership)

    monkeypatch.setattr(repository, "mark_possible_dispatch", record_mark)
    owner = OperationOwner.acquire(
        repository,
        OperationScope(OperationResourceKind.VM, "json-vm"),
        "file-json",
    )
    borrow = owner.borrow()
    try:
        outcome = _update(borrow, root, plan, b'{"source":true}', "merge-overwrite")

        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.publication_attempts == 1 and marks == 1
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_lost_read_observation_stops_without_replay_or_owner_retention(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    (root / "target").write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LostCallStdoutCarrier(1)
    try:
        outcome = _update(borrow, root, plan, b'{"source":true}', "merge-overwrite", carrier=carrier)

        assert outcome.status is FileJsonStatus.FAILED
        assert outcome.failure is FileJsonFailure.OBSERVATION
        assert outcome.publication_attempts == 0 and carrier.calls == 1
        assert not outcome.requires_owner_retention
        borrow.close()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_lost_publication_observation_preserves_cleanup_and_never_replays(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    (root / "target").write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LostCallStdoutCarrier(4)
    try:
        outcome = _update(borrow, root, plan, b'{"source":true}', "merge-overwrite", carrier=carrier)

        assert outcome.status is FileJsonStatus.UNCERTAIN
        assert outcome.publication_attempts == 1
        assert outcome.upload_outcome is not None
        assert outcome.upload_outcome.publication_uncertain
        assert outcome.upload_outcome.scratch_cleanup_debt is not None
        assert outcome.requires_owner_retention and carrier.calls == 5
        assert json.loads((root / "target").read_bytes()) == {"existing": True, "source": True}
    finally:
        database.close()


def test_interrupted_dispatch_exports_bounded_retention_fact(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = InterruptingCarrier()
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _update(borrow, root, plan, b'{"source":true}', "replace", carrier=carrier)

        fact = raised.value.__cause__
        assert isinstance(fact, FileJsonControlFact)
        outcome = fact.outcome
        assert outcome.pending_remote_effects and outcome.requires_owner_retention
        assert outcome.publication_attempts == 0 and carrier.calls == 1
        assert "source" not in repr(outcome)
        with pytest.raises(StateError):
            owner.close()
        borrow.close()
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite"], ids=["stat", "read"])
def test_expired_stat_or_read_result_preserves_termination_and_deadline_facts(
    tmp_path: Path,
    plan: IdentityPlan,
    strategy: JsonFileStrategy,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    root.joinpath("target").write_text('{"existing":true}')
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = ExpiredMissingCompletionCarrier()
    try:
        outcome = _update(borrow, root, plan, b'{"source":true}', strategy, carrier=carrier)

        assert carrier.calls == 1 and outcome.publication_attempts == 0
        assert outcome.status is FileJsonStatus.UNCERTAIN
        assert outcome.failure is FileJsonFailure.TERMINATION
        assert outcome.carrier_failure is Failure.DEADLINE
        assert outcome.deadline_exceeded and outcome.pending_remote_effects
        assert outcome.requires_owner_retention
        with pytest.raises(ExternalError) as raised:
            reduce_file_json(outcome, entity_kind="workspace file", entity_name="settings")
        assert raised.value.details is not None
        assert raised.value.details.phase is FileOperationPhase.OBSERVATION
        assert raised.value.details.reason is FileFailureReason.DEADLINE
    finally:
        database.close()


def test_interrupted_json_exchange_records_expired_deadline_fact(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = InterruptingCarrier(expire_deadline=True)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _update(borrow, root, plan, b'{"source":true}', "replace", carrier=carrier)

        fact = raised.value.__cause__
        assert isinstance(fact, FileJsonControlFact)
        assert fact.outcome.deadline_exceeded
        assert fact.outcome.pending_remote_effects
        assert fact.outcome.requires_owner_retention
    finally:
        database.close()


def test_shared_file_carrier_refuses_an_inactive_borrow_before_dispatch(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    borrow = owner.borrow()
    carrier = LocalCarrier()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    borrow.close()
    try:
        with pytest.raises(StateError):
            stat_file(
                operation,
                trusted_root_path=str(root),
                relative_path="target",
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert carrier.calls == 0
        owner.close()
    finally:
        database.close()
