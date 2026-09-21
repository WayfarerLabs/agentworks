"""Real-helper checks for private scratch-backed publication exchanges."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import agentworks.execution._file_publication_exchange as publication_exchange
from agentworks.execution._file_publication import (
    Create,
    CreateMetadata,
    Match,
    PublicationCleanupDebt,
    PublicationFailureKind,
    PublicationPhase,
    Replace,
)
from agentworks.execution._file_publication_bundle import FIXED_BUNDLE as PRODUCTION_BUNDLE
from agentworks.execution._file_publication_exchange import (
    FilePublicationObservationState,
    publication_cleanup,
    publication_reconcile,
    publish,
)
from agentworks.execution._file_publication_protocol import (
    FilePublicationFailureCode,
    PublicationCleanupState,
    publication_context,
)
from agentworks.execution._file_publication_wire import bind_publication_cleanup_debt
from agentworks.execution._file_snapshot import read_snapshot
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._publication_receipt import (
    PublicationReceiptFailureKind,
    PublicationStageCleanupDebt,
    _Identity,
    cleanup_publication_stage,
    publication_stage_name,
)
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution._scratch import ScratchReference, cleanup_scratch
from agentworks.execution._scratch_receipt import scratch_name
from agentworks.execution.carrier import CarrierIO, Deadline, SinkOutput
from tests.execution.files._file_publication_support import LocalCarrier, install_fixture_bundle
from tests.execution.files._publication_test_support import open_parent, ready_scratch, record_stage
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private publication helper requires Linux")

_TOKEN = bytes(range(16))
_ADVANCE_AFTER_OPERATE = """
clock=[guest.time.monotonic()]
def controlled_monotonic():
 return clock[0]
guest.time.monotonic=controlled_monotonic
real_operate=guest._operate
def advancing_operate(*args,**kwargs):
 try:
  return real_operate(*args,**kwargs)
 finally:
  clock[0]+=10.0
guest._operate=advancing_operate
"""
_EXPIRE_BEFORE_MUTATION = """
clock=iter((0.0,100.0))
guest.time.monotonic=lambda:next(clock,100.0)
"""
_EXPIRE_BEFORE_CLEANUP = """
clock=iter((0.0,0.0,100.0))
guest.time.monotonic=lambda:next(clock,100.0)
"""
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
_UNCERTAIN_PUBLICATION_WITH_UNIDENTIFIED_DEBT = """
def uncertain_publish(*args,**kwargs):
 raise guest.FilePublicationError(
  guest.PublicationFailureKind.UNCERTAIN,
  guest.PublicationPhase.PUBLICATION,
  cleanup_debt=guest.PublicationCleanupDebt('untrusted-name',None,None),
 )
guest.publish_file=uncertain_publish
"""
_FAIL_CLEANUP_BINDING_IDENTITY = """
def fail_cleanup_binding_identity(*args,**kwargs):
 raise guest._SafeFailure(
  guest.FilePublicationFailureControl(guest.FilePublicationFailureCode.PARENT_REFUSED)
 )
guest._parent_identity=fail_cleanup_binding_identity
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
_FAIL_AFTER_RECORD_REMOVAL = """
publication=sys.modules['_agw_file_publication._file_publication']
real_remove_record=publication.remove_publication_record
def remove_record_then_fail(*args,**kwargs):
 real_remove_record(*args,**kwargs)
 raise publication.PublicationReceiptError(publication.PublicationReceiptFailureKind.IO)
publication.remove_publication_record=remove_record_then_fail
"""


@pytest.fixture
def plan() -> IdentityPlan:
    identity = IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()})))
    return IdentityPlan(identity, IdentityMode.DIRECT)


def _metadata(mode: int = 0o600) -> CreateMetadata:
    return CreateMetadata(os.geteuid(), os.getegid(), mode)


def _publish(
    root: Path,
    relative: str,
    reference: ScratchReference,
    content: bytes,
    condition: Create | Replace | Match,
    plan: IdentityPlan,
    *,
    runtime: str = sys.executable,
    carrier: LocalCarrier | None = None,
    deadline: Deadline | None = None,
    mode: int = 0o600,
):
    carrier = carrier or LocalCarrier()
    result = publish(
        carrier,
        trusted_root_path=str(root),
        relative_path=relative,
        token=_TOKEN,
        reference=reference,
        digest=hashlib.sha256(content).digest(),
        condition=condition,
        create_metadata=_metadata(mode),
        plan=plan,
        deadline=deadline or Deadline.after(15),
        runtime_selection=runtime_selection(runtime),
    )
    return carrier, result


def _cleanup_scratch(parent: Path, ready: object) -> None:
    parent_fd = open_parent(parent)
    try:
        cleanup_scratch(parent_fd, ready)  # type: ignore[arg-type]
    finally:
        os.close(parent_fd)


def test_missing_runtime_yields_no_publication_observation(tmp_path: Path, plan: IdentityPlan) -> None:
    content = b"publication"
    parent_fd = open_parent(tmp_path)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)

    carrier, result = _publish(
        tmp_path,
        "target",
        ready._reference,
        content,
        Create(),
        plan,
        runtime="/missing/agentworks-python",
    )

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
    assert result.observation is None
    _cleanup_scratch(tmp_path, ready)


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "system-3.11"])
@pytest.mark.parametrize("condition_name", ["create", "replace", "match"])
def test_production_bundle_publishes_each_condition_with_content_revision_and_metadata(
    tmp_path: Path,
    plan: IdentityPlan,
    runtime: Path,
    condition_name: str,
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    root = tmp_path / "approved"
    parent = root / "nested"
    parent.mkdir(parents=True)
    target = parent / "target"
    content = b"private-binary\0content\xff"
    condition: Create | Replace | Match
    expected_mode = 0o640
    if condition_name == "create":
        condition = Create()
    else:
        target.write_bytes(b"old-content")
        target.chmod(0o604)
        expected_mode = 0o604
        if condition_name == "replace":
            condition = Replace()
        else:
            parent_fd = open_parent(parent)
            try:
                observed = read_snapshot(parent_fd, "target", 1024)
            finally:
                os.close(parent_fd)
            assert observed is not None
            condition = Match(observed.revision)
    parent_fd = open_parent(parent)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)

    carrier, result = _publish(
        root,
        "nested/target",
        ready._reference,
        content,
        condition,
        plan,
        runtime=str(runtime),
        mode=0o640,
    )

    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.observation.state is FilePublicationObservationState.PUBLISHED
    assert result.observation.deadline_exceeded is False
    revision = result.observation.revision
    assert revision is not None
    assert revision.stat.size == len(content)
    assert revision.digest == hashlib.sha256(content).digest()
    assert target.read_bytes() == content
    assert stat.S_IMODE(target.stat().st_mode) == expected_mode
    assert carrier.calls == 1 and carrier.io is not None and carrier.io.sensitive
    assert content not in carrier.io.input.data  # type: ignore[union-attr]
    assert content not in PRODUCTION_BUNDLE.prefix
    assert carrier.invocation is not None
    assert str(root) not in carrier.invocation.argv and content.decode("latin1") not in carrier.invocation.argv
    assert content.hex() not in repr(result)

    _cleanup_scratch(parent, ready)


@pytest.mark.parametrize(
    ("case", "condition", "expected_kind", "expected_phase"),
    [
        ("create-existing", Create(), PublicationFailureKind.CONFLICT, PublicationPhase.PUBLICATION),
        ("replace-absent", Replace(), PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION),
    ],
)
def test_conditional_conflicts_preserve_closed_failure_and_destination(
    tmp_path: Path,
    plan: IdentityPlan,
    case: str,
    condition: Create | Replace,
    expected_kind: PublicationFailureKind,
    expected_phase: PublicationPhase,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    if case == "create-existing":
        target.write_bytes(b"old")
    content = b"new"
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)

    _, result = _publish(root, "target", ready._reference, content, condition, plan)

    assert result.observation.state is FilePublicationObservationState.REFUSED
    failure = result.observation.failure
    assert failure is not None and failure.code is FilePublicationFailureCode.PUBLICATION
    assert failure.publication_kind is expected_kind
    assert failure.publication_phase is expected_phase
    assert failure.cleanup_state is PublicationCleanupState.NONE
    assert not target.exists() if case == "replace-absent" else target.read_bytes() == b"old"
    _cleanup_scratch(root, ready)


def test_match_digest_conflict_and_whole_digest_mismatch_refuse_without_publication(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"old")
    parent_fd = open_parent(root)
    try:
        observed = read_snapshot(parent_fd, "target", 1024)
        assert observed is not None
        stale = replace(observed.revision, digest=hashlib.sha256(b"different").digest())
        ready_match = ready_scratch(parent_fd, _TOKEN, b"new")
    finally:
        os.close(parent_fd)

    _, conflict = _publish(root, "target", ready_match._reference, b"new", Match(stale), plan)
    assert conflict.observation.state is FilePublicationObservationState.REFUSED
    assert conflict.observation.failure is not None
    assert conflict.observation.failure.publication_kind is PublicationFailureKind.CONFLICT
    assert target.read_bytes() == b"old"
    _cleanup_scratch(root, ready_match)

    second_token = bytes(reversed(_TOKEN))
    parent_fd = open_parent(root)
    try:
        ready_digest = ready_scratch(parent_fd, second_token, b"actual")
    finally:
        os.close(parent_fd)
    carrier = LocalCarrier()
    mismatch = publish(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        token=second_token,
        reference=ready_digest._reference,
        digest=hashlib.sha256(b"claimed").digest(),
        condition=Replace(),
        create_metadata=_metadata(),
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )
    assert mismatch.observation.state is FilePublicationObservationState.REFUSED
    assert mismatch.observation.failure is not None
    assert mismatch.observation.failure.code is FilePublicationFailureCode.SCRATCH
    assert mismatch.observation.failure.scratch_phase is not None
    assert target.read_bytes() == b"old"
    _cleanup_scratch(root, ready_digest)


def test_identity_refuses_before_missing_root_or_path_access(tmp_path: Path, plan: IdentityPlan) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    parent_fd = open_parent(parent)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
    finally:
        os.close(parent_fd)
    wrong_identity = IdentityExpectation(plan.expected.euid + 1, plan.expected.egid, plan.expected.groups)
    wrong_plan = IdentityPlan(wrong_identity, IdentityMode.DIRECT)
    wrong_ownership = replace(ready._reference._ownership, _context=publication_context(wrong_identity))
    wrong_reference = ScratchReference(wrong_ownership)
    carrier = LocalCarrier()

    result = publish(
        carrier,
        trusted_root_path=str(tmp_path / "missing-root"),
        relative_path="missing/target",
        token=_TOKEN,
        reference=wrong_reference,
        digest=hashlib.sha256(b"content").digest(),
        condition=Create(),
        create_metadata=_metadata(),
        plan=wrong_plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )

    assert result.observation.state is FilePublicationObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.code is FilePublicationFailureCode.IDENTITY_MISMATCH
    _cleanup_scratch(parent, ready)


def test_missing_parent_and_removed_scratch_payload_are_closed_refusals(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
    finally:
        os.close(parent_fd)

    _, missing_parent = _publish(root, "missing/target", ready._reference, b"content", Create(), plan)
    assert missing_parent.observation.state is FilePublicationObservationState.REFUSED
    assert missing_parent.observation.failure is not None
    assert missing_parent.observation.failure.code is FilePublicationFailureCode.PARENT_REFUSED

    (root / scratch_name(_TOKEN) / "data").unlink()
    _, missing_source = _publish(root, "target", ready._reference, b"content", Create(), plan)
    assert missing_source.observation.state is FilePublicationObservationState.REFUSED
    assert missing_source.observation.failure is not None
    assert missing_source.observation.failure.code is FilePublicationFailureCode.SCRATCH
    (root / scratch_name(_TOKEN) / "receipt").unlink()
    (root / scratch_name(_TOKEN)).rmdir()


def test_reconcile_recovers_historical_debt_after_payload_removal_then_cleanup_is_exact(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
        ownership, stage_fd = record_stage(parent_fd, ready, parent_fd)
        os.close(stage_fd)
    finally:
        os.close(parent_fd)
    (root / scratch_name(_TOKEN) / "data").unlink()

    recovered = publication_reconcile(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )

    assert recovered.observation.state is FilePublicationObservationState.RECOVERED
    assert recovered.observation.deadline_exceeded is False
    debt = recovered.observation.cleanup_debt
    assert debt is not None
    cleaned = publication_cleanup(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )
    assert cleaned.observation.state is FilePublicationObservationState.CLEANED
    assert cleaned.observation.deadline_exceeded is False
    assert not (root / publication_stage_name(_TOKEN)).exists()
    assert not (root / scratch_name(_TOKEN) / "publication-receipt").exists()
    assert ownership._stage_name == publication_stage_name(_TOKEN)
    (root / scratch_name(_TOKEN) / "receipt").unlink()
    (root / scratch_name(_TOKEN)).rmdir()


def test_reconcile_never_infers_publication_or_quiescence_from_missing_record(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
    finally:
        os.close(parent_fd)

    result = publication_reconcile(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )

    assert result.observation.state is FilePublicationObservationState.OWNERSHIP_UNCERTAIN
    assert result.observation.revision is None and result.observation.cleanup_debt is None
    _cleanup_scratch(root, ready)


@pytest.mark.parametrize("case", ["partial-record", "missing-stage", "foreign-parent"])
def test_reconcile_partial_missing_and_foreign_ownership_stays_uncertain(
    tmp_path: Path,
    plan: IdentityPlan,
    case: str,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
        ownership, stage_fd = record_stage(parent_fd, ready, parent_fd)
        os.close(stage_fd)
    finally:
        os.close(parent_fd)
    record = root / scratch_name(_TOKEN) / "publication-receipt"
    record_content = record.read_bytes()
    if case == "partial-record":
        record.chmod(0o600)
        record.write_bytes(record_content[:5])
        record.chmod(0o400)
    elif case == "missing-stage":
        (root / publication_stage_name(_TOKEN)).unlink()
    else:
        foreign = tmp_path / "foreign"
        foreign.mkdir()

    result = publication_reconcile(
        LocalCarrier(),
        trusted_root_path=str(tmp_path / "foreign") if case == "foreign-parent" else str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )

    assert result.observation.state is FilePublicationObservationState.OWNERSHIP_UNCERTAIN
    assert result.observation.cleanup_debt is None and result.observation.revision is None

    if case == "partial-record":
        record.chmod(0o600)
        record.write_bytes(record_content)
        record.chmod(0o400)
    elif case == "missing-stage":
        record.unlink()
        _cleanup_scratch(root, ready)
        return
    parent_fd = open_parent(root)
    try:
        cleanup_publication_stage(parent_fd, parent_fd, PublicationStageCleanupDebt(ownership, False))
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_cleanup_accepts_record_only_and_identified_generic_debt(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
        ownership, stage_fd = record_stage(parent_fd, ready, parent_fd)
        os.close(stage_fd)
        os.unlink(ownership._stage_name, dir_fd=parent_fd)
        parent_stat = os.fstat(parent_fd)
        record_only = bind_publication_cleanup_debt(
            ready._reference,
            _Identity(parent_stat.st_dev, parent_stat.st_ino),
            PublicationStageCleanupDebt(ownership, True),
        )
    finally:
        os.close(parent_fd)

    record_cleaned = publication_cleanup(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        cleanup_debt=record_only,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )
    assert record_cleaned.observation.state is FilePublicationObservationState.CLEANED

    generic_name = publication_stage_name(_TOKEN)
    generic_path = root / generic_name
    generic_path.write_bytes(b"staged")
    observed = generic_path.stat()
    generic = bind_publication_cleanup_debt(
        ready._reference,
        _Identity(root.stat().st_dev, root.stat().st_ino),
        PublicationCleanupDebt(generic_name, observed.st_dev, observed.st_ino),
    )
    generic_cleaned = publication_cleanup(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        cleanup_debt=generic,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )
    assert generic_cleaned.observation.state is FilePublicationObservationState.CLEANED
    assert not generic_path.exists()
    _cleanup_scratch(root, ready)


def test_cleanup_refuses_replaced_generic_stage_without_deleting_foreign_object(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
    finally:
        os.close(parent_fd)
    stage = root / publication_stage_name(_TOKEN)
    stage.write_bytes(b"owned")
    observed = stage.stat()
    debt = bind_publication_cleanup_debt(
        ready._reference,
        _Identity(root.stat().st_dev, root.stat().st_ino),
        PublicationCleanupDebt(stage.name, observed.st_dev, observed.st_ino),
    )
    held = root / "held-owned-stage"
    stage.rename(held)
    stage.write_bytes(b"foreign")

    result = publication_cleanup(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )

    assert result.observation.state is FilePublicationObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.publication_kind is PublicationFailureKind.CONFLICT
    assert result.observation.cleanup_debt == debt
    assert stage.read_bytes() == b"foreign"
    stage.unlink()
    held.unlink()
    _cleanup_scratch(root, ready)


def test_publication_failure_returns_exact_debt_that_authorizes_only_bounded_cleanup(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(monkeypatch, _LEAVE_EXACT_PUBLICATION_DEBT)
    root = tmp_path / "approved"
    root.mkdir()
    content = b"content"
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)

    _, failed = _publish(root, "target", ready._reference, content, Create(), plan)

    assert failed.observation.state is FilePublicationObservationState.REFUSED
    failure = failed.observation.failure
    assert failure is not None and failure.code is FilePublicationFailureCode.PUBLICATION
    assert failure.publication_kind is PublicationFailureKind.IO
    assert failure.publication_phase is PublicationPhase.CONTENT
    assert failure.cleanup_state is PublicationCleanupState.EXACT
    debt = failed.observation.cleanup_debt
    assert debt is not None
    assert (root / publication_stage_name(_TOKEN)).exists()

    monkeypatch.setattr(publication_exchange, "FIXED_BUNDLE", PRODUCTION_BUNDLE)
    cleaned = publication_cleanup(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )
    assert cleaned.observation.state is FilePublicationObservationState.CLEANED
    _cleanup_scratch(root, ready)


def test_uncertain_primitive_failure_never_becomes_publication_or_cleanup_authority(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(
        monkeypatch,
        _UNCERTAIN_PUBLICATION_WITH_UNIDENTIFIED_DEBT + _FAIL_CLEANUP_BINDING_IDENTITY,
    )
    root = tmp_path / "approved"
    root.mkdir()
    content = b"content"
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)

    _, result = _publish(root, "target", ready._reference, content, Create(), plan)

    assert result.observation.state is FilePublicationObservationState.UNCERTAIN
    assert result.observation.revision is None and result.observation.cleanup_debt is None
    failure = result.observation.failure
    assert failure is not None and failure.publication_kind is PublicationFailureKind.UNCERTAIN
    assert failure.publication_phase is PublicationPhase.PUBLICATION
    assert failure.cleanup_state is PublicationCleanupState.OWNERSHIP_UNCERTAIN
    assert not (root / "target").exists()
    _cleanup_scratch(root, ready)


def test_progressed_cleanup_binding_loss_preserves_receipt_failure_and_effect(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(monkeypatch, _PROGRESS_CLEANUP_THEN_LOSE_BINDING_IDENTITY)
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
        ownership, stage_fd = record_stage(parent_fd, ready, parent_fd)
        os.close(stage_fd)
        parent_stat = os.fstat(parent_fd)
        debt = bind_publication_cleanup_debt(
            ready._reference,
            _Identity(parent_stat.st_dev, parent_stat.st_ino),
            PublicationStageCleanupDebt(ownership, False),
        )
    finally:
        os.close(parent_fd)

    result = publication_cleanup(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )

    assert result.observation.state is FilePublicationObservationState.REFUSED
    failure = result.observation.failure
    assert failure is not None and failure.code is FilePublicationFailureCode.RECEIPT
    assert failure.receipt_kind is PublicationReceiptFailureKind.IO
    assert failure.cleanup_state is PublicationCleanupState.OWNERSHIP_UNCERTAIN
    assert result.observation.cleanup_debt is None
    assert not (root / publication_stage_name(_TOKEN)).exists()
    parent_fd = open_parent(root)
    try:
        cleanup_publication_stage(parent_fd, parent_fd, PublicationStageCleanupDebt(ownership, True))
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_post_rename_record_failure_preserves_written_destination_as_uncertain(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(monkeypatch, _FAIL_AFTER_RECORD_REMOVAL)
    root = tmp_path / "approved"
    root.mkdir()
    content = b"published-before-record-failure"
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)

    _, result = _publish(root, "target", ready._reference, content, Create(), plan)

    assert result.observation.state is FilePublicationObservationState.UNCERTAIN
    assert result.observation.revision is None and result.observation.cleanup_debt is None
    failure = result.observation.failure
    assert failure is not None
    assert failure.publication_kind is PublicationFailureKind.UNCERTAIN
    assert failure.publication_phase is PublicationPhase.PUBLICATION
    assert (root / "target").read_bytes() == content
    _cleanup_scratch(root, ready)


class _DiscardSink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


class _LostStdoutCarrier(LocalCarrier):
    def execute(self, invocation, *, io, deadline):
        assert isinstance(io.output, SinkOutput)
        hidden = CarrierIO(
            input=io.input,
            output=SinkOutput(_DiscardSink(), io.output.stderr, require_live=False),
            sensitive=io.sensitive,
        )
        return super().execute(invocation, io=hidden, deadline=deadline)


def test_lost_publish_reply_is_uncertain_and_never_replayed(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    content = b"published-once"
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)
    carrier = _LostStdoutCarrier()

    _, result = _publish(root, "target", ready._reference, content, Create(), plan, carrier=carrier)

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.UNKNOWN
    assert result.observation is None
    assert (root / "target").read_bytes() == content
    reconciled = publication_reconcile(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )
    assert reconciled.observation.state is FilePublicationObservationState.OWNERSHIP_UNCERTAIN
    _cleanup_scratch(root, ready)


def test_late_deadline_retains_published_effect_and_operational_failure(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(monkeypatch, _ADVANCE_AFTER_OPERATE)
    root = tmp_path / "approved"
    root.mkdir()
    content = b"late-effect"
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)

    _, published = _publish(
        root,
        "target",
        ready._reference,
        content,
        Create(),
        plan,
        deadline=Deadline.after(1),
    )
    assert published.observation.state is FilePublicationObservationState.PUBLISHED
    assert published.observation.deadline_exceeded is True
    assert (root / "target").read_bytes() == content
    _cleanup_scratch(root, ready)

    second_token = bytes(reversed(_TOKEN))
    parent_fd = open_parent(root)
    try:
        conflicting = ready_scratch(parent_fd, second_token, b"new")
    finally:
        os.close(parent_fd)
    carrier = LocalCarrier()
    refused = publish(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        token=second_token,
        reference=conflicting._reference,
        digest=hashlib.sha256(b"new").digest(),
        condition=Create(),
        create_metadata=_metadata(),
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )
    assert refused.observation.state is FilePublicationObservationState.REFUSED
    assert refused.observation.failure is not None
    assert refused.observation.failure.publication_kind is PublicationFailureKind.CONFLICT
    _cleanup_scratch(root, conflicting)


def test_late_deadline_retains_recovered_debt_and_completed_cleanup(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(monkeypatch, _ADVANCE_AFTER_OPERATE)
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
        _, stage_fd = record_stage(parent_fd, ready, parent_fd)
        os.close(stage_fd)
    finally:
        os.close(parent_fd)

    recovered = publication_reconcile(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )
    assert recovered.observation.state is FilePublicationObservationState.RECOVERED
    assert recovered.observation.deadline_exceeded is True
    debt = recovered.observation.cleanup_debt
    assert debt is not None

    cleaned = publication_cleanup(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )
    assert cleaned.observation.state is FilePublicationObservationState.CLEANED
    assert cleaned.observation.deadline_exceeded is True
    _cleanup_scratch(root, ready)


def test_early_expiry_refuses_before_mutation(
    tmp_path: Path, plan: IdentityPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fixture_bundle(monkeypatch, _EXPIRE_BEFORE_MUTATION)
    root = tmp_path / "approved"
    root.mkdir()
    content = b"early"
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, content)
    finally:
        os.close(parent_fd)

    _, result = _publish(root, "target", ready._reference, content, Create(), plan)

    assert result.observation.state is FilePublicationObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.code is FilePublicationFailureCode.DEADLINE
    assert not (root / "target").exists() and not (root / publication_stage_name(_TOKEN)).exists()
    _cleanup_scratch(root, ready)


def test_cleanup_expiry_before_deletion_preserves_input_debt(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fixture_bundle(monkeypatch, _EXPIRE_BEFORE_CLEANUP)
    root = tmp_path / "approved"
    root.mkdir()
    parent_fd = open_parent(root)
    try:
        ready = ready_scratch(parent_fd, _TOKEN, b"content")
    finally:
        os.close(parent_fd)
    stage = root / publication_stage_name(_TOKEN)
    stage.write_bytes(b"stage")
    observed = stage.stat()
    debt = bind_publication_cleanup_debt(
        ready._reference,
        _Identity(root.stat().st_dev, root.stat().st_ino),
        PublicationCleanupDebt(stage.name, observed.st_dev, observed.st_ino),
    )

    result = publication_cleanup(
        LocalCarrier(),
        trusted_root_path=str(root),
        relative_path="target",
        token=_TOKEN,
        reference=ready._reference,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(),
    )

    assert result.observation.state is FilePublicationObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.publication_kind is PublicationFailureKind.DEADLINE
    assert result.observation.cleanup_debt == debt
    assert stage.exists()
    stage.unlink()
    _cleanup_scratch(root, ready)
