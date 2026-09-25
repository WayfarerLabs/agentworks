"""One-row package upload capacity and checkpoint ordering."""

from __future__ import annotations

import os
import secrets
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_operation
from agentworks.execution._file_obligation import FileCallFamily, decode_file_call_obligation
from agentworks.execution._file_operation import FileOperation, PackageUploadMember
from agentworks.execution._file_publication import Create
from agentworks.execution._file_upload import FileUploadBinding, FileUploadOutcome, FileUploadStatus
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import CarrierIO, CarrierReport, ChannelFeatures, Deadline, PreparedInvocation
from agentworks.operations import OperationOwner
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._file_publication_support import install_fixture_bundle as install_publication
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage
from tests.execution.files._file_upload_support import BytesSource, new_metadata
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private file helpers require Linux")


class RowCheckingCarrier:
    def __init__(self, database: Database, owner: OperationOwner) -> None:
        self._database = database
        self._owner = owner
        self._inner = LocalCarrier()
        self.expected_index = 0

    @property
    def features(self) -> ChannelFeatures:
        return self._inner.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._inner.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        (row,) = self._database.operations.list_lifecycle_obligations(self._owner.ownership)
        call = decode_file_call_obligation(row.payload)
        assert call.family is FileCallFamily.PACKAGE_UPLOAD
        assert call.batch_index == self.expected_index
        assert call.token is not None
        return self._inner.execute(invocation, io=io, deadline=deadline)


@pytest.fixture(autouse=True)
def bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stage(monkeypatch)
    install_publication(monkeypatch)


def _plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set(os.getgroups()) | {gid}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


def test_129_member_plan_stops_before_next_failed_checkpoint(tmp_path: Path) -> None:
    database = Database(tmp_path / "owner.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "package-vm"),
        "package",
    )
    operation = FileOperation(owner, target_for_owner(owner))
    root = tmp_path / "root"
    root.mkdir()
    sources = [BytesSource(bytes([index % 256])) for index in range(129)]
    members = tuple(
        PackageUploadMember(f"member-{index}", source, 1, Create(), new_metadata())
        for index, source in enumerate(sources)
    )
    checkpoint_db = sqlite3.connect(tmp_path / "checkpoint.db")
    checkpoint_db.execute("CREATE TABLE applied (member INTEGER PRIMARY KEY)")
    stop = RuntimeError("checkpoint interrupted")
    carrier = RowCheckingCarrier(database, owner)

    def checkpoint(index: int, outcome: FileUploadOutcome) -> None:
        assert outcome.status is FileUploadStatus.COMPLETE
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1
        call = decode_file_call_obligation(rows[0].payload)
        assert call.family is FileCallFamily.PACKAGE_UPLOAD
        assert call.batch_index == index
        assert call.relative_path == f"member-{index}"
        checkpoint_db.execute("INSERT INTO applied (member) VALUES (?)", (index,))
        checkpoint_db.commit()
        carrier.expected_index = index + 1
        if index == 64:
            raise stop

    try:
        with pytest.raises(RuntimeError) as caught:
            operation.upload_package(
                carrier,
                trusted_root_path=str(root),
                members=members,
                checkpoint=checkpoint,
                plan=_plan(),
                deadline=Deadline.after(180),
                runtime_selection=runtime_selection(sys.executable),
            )
        assert caught.value is stop
        assert checkpoint_db.execute("SELECT COUNT(*) FROM applied").fetchone() == (65,)
        assert all((root / f"member-{index}").read_bytes() == bytes([index % 256]) for index in range(65))
        assert not (root / "member-65").exists()
        assert all(source.calls == 0 for source in sources[65:])
        assert operation.unfinished_package_uploads[0].index == 64
        assert operation.unfinished_package_uploads[0].checkpoint_pending
        assert len(database.operations.list_lifecycle_obligations(owner.ownership)) == 1
        with pytest.raises(StateError):
            owner.close()
    finally:
        checkpoint_db.close()


def test_4096_synthetic_members_fit_one_lifecycle_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the ledger bound without 4096 subprocesses or file writes."""
    database = Database(tmp_path / "owner.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "package-vm"),
        "package",
    )
    operation = FileOperation(owner, target_for_owner(owner))
    plan = _plan()
    selection = runtime_selection(sys.executable)
    seen: list[int] = []

    @dataclass
    class Prepared:
        binding: FileUploadBinding
        state: Token

        def run(self) -> FileUploadOutcome:
            return FileUploadOutcome(
                FileUploadStatus.COMPLETE,
                self.binding,
                self.state.token,
                0,
                0,
                publication_confirmed=True,
            )

    @dataclass
    class Token:
        token: bytes

    def prepare(*_args: object, **kwargs: object) -> Prepared:
        return Prepared(
            FileUploadBinding(
                str(kwargs["trusted_root_path"]),
                str(kwargs["relative_path"]),
                0,
                plan,
                selection,
            ),
            Token(secrets.token_bytes(16)),
        )

    monkeypatch.setattr(_file_operation, "_prepare_upload", prepare)
    members = tuple(
        PackageUploadMember(f"member-{index}", BytesSource(b""), 0, Create(), new_metadata()) for index in range(4096)
    )

    def checkpoint(index: int, outcome: FileUploadOutcome) -> None:
        assert outcome.status is FileUploadStatus.COMPLETE
        (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
        call = decode_file_call_obligation(row.payload)
        assert call.batch_index == index
        assert call.relative_path == f"member-{index}"
        seen.append(index)

    results = operation.upload_package(
        LocalCarrier(),
        trusted_root_path=str(tmp_path),
        members=members,
        checkpoint=checkpoint,
        plan=plan,
        deadline=Deadline.after(180),
        runtime_selection=selection,
    )
    assert len(results) == 4096
    assert len(seen) == 4096
    assert len(database.operations.list_lifecycle_obligations(owner.ownership)) == 1
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_later_preparation_refusal_closes_checkpointed_batch(tmp_path: Path) -> None:
    database = Database(tmp_path / "owner.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "package-vm"),
        "package",
    )
    operation = FileOperation(owner, target_for_owner(owner))
    root = tmp_path / "root"
    root.mkdir()
    committed: list[int] = []
    members = (
        PackageUploadMember("first", BytesSource(b"a"), 1, Create(), new_metadata()),
        PackageUploadMember("invalid", BytesSource(b"b"), -1, Create(), new_metadata()),
    )
    with pytest.raises(ValidationError):
        operation.upload_package(
            LocalCarrier(),
            trusted_root_path=str(root),
            members=members,
            checkpoint=lambda index, outcome: committed.append(index),
            plan=_plan(),
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )
    assert committed == [0]
    assert (root / "first").read_bytes() == b"a"
    assert not (root / "invalid").exists()
    (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
    assert row.state is LifecycleObligationState.RESOLVED
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_lost_child_cas_reply_retries_only_the_exact_intended_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "owner.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "package-vm"),
        "package",
    )
    operation = FileOperation(owner, target_for_owner(owner))
    root = tmp_path / "root"
    root.mkdir()
    original = type(database.operations).publish_lifecycle_obligation_payload
    lost = False
    attempts: list[tuple[int, bytes]] = []

    def publish(repository, ownership, obligation_id, *, expected_revision, payload_version, payload):
        nonlocal lost
        call = decode_file_call_obligation(payload)
        if call.family is FileCallFamily.PACKAGE_UPLOAD and call.batch_index == 1:
            attempts.append((expected_revision, payload))
            result = original(
                repository,
                ownership,
                obligation_id,
                expected_revision=expected_revision,
                payload_version=payload_version,
                payload=payload,
            )
            if not lost:
                lost = True
                raise RuntimeError("committed child CAS reply lost")
            return result
        return original(
            repository,
            ownership,
            obligation_id,
            expected_revision=expected_revision,
            payload_version=payload_version,
            payload=payload,
        )

    monkeypatch.setattr(type(database.operations), "publish_lifecycle_obligation_payload", publish)
    members = tuple(
        PackageUploadMember(f"member-{index}", BytesSource(bytes([index])), 1, Create(), new_metadata())
        for index in range(2)
    )
    checkpoints: list[int] = []
    results = operation.upload_package(
        LocalCarrier(),
        trusted_root_path=str(root),
        members=members,
        checkpoint=lambda index, outcome: checkpoints.append(index),
        plan=_plan(),
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert len(results) == 2
    assert checkpoints == [0, 1]
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert (root / "member-1").read_bytes() == b"\x01"
    (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
    assert row.payload_revision == 1
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_unconfirmed_child_cas_never_dispatches_that_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "owner.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "package-vm"),
        "package",
    )
    operation = FileOperation(owner, target_for_owner(owner))
    root = tmp_path / "root"
    root.mkdir()
    source = BytesSource(b"b")
    members = (
        PackageUploadMember("first", BytesSource(b"a"), 1, Create(), new_metadata()),
        PackageUploadMember("second", source, 1, Create(), new_metadata()),
    )
    original = type(database.operations).publish_lifecycle_obligation_payload
    attempts: list[tuple[int, bytes]] = []

    def publish(repository, ownership, obligation_id, *, expected_revision, payload_version, payload):
        call = decode_file_call_obligation(payload)
        if call.family is FileCallFamily.PACKAGE_UPLOAD and call.batch_index == 1:
            attempts.append((expected_revision, payload))
            raise RuntimeError("CAS unavailable")
        return original(
            repository,
            ownership,
            obligation_id,
            expected_revision=expected_revision,
            payload_version=payload_version,
            payload=payload,
        )

    monkeypatch.setattr(type(database.operations), "publish_lifecycle_obligation_payload", publish)
    checkpoints: list[int] = []
    with pytest.raises(RuntimeError, match="CAS unavailable"):
        operation.upload_package(
            LocalCarrier(),
            trusted_root_path=str(root),
            members=members,
            checkpoint=lambda index, outcome: checkpoints.append(index),
            plan=_plan(),
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )
    assert checkpoints == [0]
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
    assert decode_file_call_obligation(row.payload).batch_index == 0
    assert source.calls == 0
    assert not (root / "second").exists()
    with pytest.raises(StateError):
        owner.close()
