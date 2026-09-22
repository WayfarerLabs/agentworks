"""Real installed-OpenSSH evidence for the owned file-upload workflow."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import ConflictError
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_publication import Create, Match
from agentworks.execution._file_result_transfer import reduce_file_upload
from agentworks.execution._file_upload import FileUploadFailure, FileUploadOutcome, FileUploadStatus
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch_receipt import scratch_name
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHConnection
from agentworks.execution.files import Change, FileFailureReason
from agentworks.operations import OperationOwner
from tests.execution.carriers.ssh.test_file_delivery import (
    _all_attempts_succeeded,
    _assert_sensitive_attempt,
    _direct_plan,
    _ObservedSSHCarrier,
)
from tests.execution.files._file_upload_support import BytesSource, new_metadata

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "linux", reason="the file helpers require Linux"),
]

_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
_DESTINATION = "file-operation-destination-canary"


def _upload(
    operation: FileOperation,
    carrier: _ObservedSSHCarrier,
    root: Path,
    content: bytes,
    condition: Create | Match,
) -> FileUploadOutcome:
    return operation.upload(
        carrier,
        trusted_root_path=str(root),
        relative_path=_DESTINATION,
        source=BytesSource(content),
        size=len(content),
        condition=condition,
        create_metadata=new_metadata(),
        plan=_direct_plan(),
        deadline=Deadline.after(30),
        runtime_selection=_RUNTIME,
    )


def _assert_released(operation: FileOperation, root: Path, outcome: FileUploadOutcome) -> None:
    scratch = root / scratch_name(outcome.token)
    if outcome.requires_owner_retention:
        pytest.fail(f"file upload retained custody; exact scratch path: {scratch}")
    assert outcome.scratch_cleanup_debt is None
    assert outcome.publication_cleanup_debt is None
    assert not outcome.pending_remote_effects
    assert not outcome.coordination_uncertain
    assert operation.active_uploads == ()
    assert operation.unfinished_uploads == ()
    assert not scratch.exists()


def _stat_fingerprint(path: Path) -> tuple[int, ...]:
    observed = path.stat()
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_nlink,
        observed.st_uid,
        observed.st_gid,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def test_file_operation_upload_conditions_and_cleanup_over_real_ssh(
    tmp_path: Path,
    local_sshd: SSHConnection,
) -> None:
    root = tmp_path.resolve() / "upload-root"
    root.mkdir()
    target = root / _DESTINATION
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "ssh-file-operation-vm"),
        "ssh-file-upload",
    )
    operation = FileOperation(owner)
    carrier = _ObservedSSHCarrier(local_sshd)
    created_content = bytes(range(256)) + b"\x00\xffcreated-over-ssh\r\n"
    updated_content = bytes(reversed(range(256))) + b"\xff\x00updated-over-ssh\n"
    refused_content = b"must-not-replace-destination\x00\xff"

    try:
        created = _upload(operation, carrier, root, created_content, Create())
        _assert_released(operation, root, created)
        assert created.status is FileUploadStatus.COMPLETE
        assert created.content_digest == hashlib.sha256(created_content).digest()
        assert created.runtime_prerequisite is not None
        assert created.runtime_prerequisite.state is RuntimePrerequisiteState.READY
        assert created.revision is not None
        created_result = reduce_file_upload(created, entity_kind="workspace file", entity_name="settings")
        assert created_result.change is Change.CHANGED and created_result.revision is not None
        assert target.read_bytes() == created_content
        created_stat = target.stat()
        assert stat.S_IMODE(created_stat.st_mode) == 0o640
        assert (created_stat.st_uid, created_stat.st_gid) == (os.geteuid(), os.getegid())

        matched = _upload(operation, carrier, root, updated_content, Match(created.revision))
        _assert_released(operation, root, matched)
        assert matched.status is FileUploadStatus.COMPLETE
        assert matched.content_digest == hashlib.sha256(updated_content).digest()
        assert matched.revision is not None
        matched_result = reduce_file_upload(matched, entity_kind="workspace file", entity_name="settings")
        assert matched_result.change is Change.CHANGED and matched_result.revision is not None
        assert matched_result.revision != created_result.revision
        assert target.read_bytes() == updated_content
        matched_stat = target.stat()
        assert stat.S_IMODE(matched_stat.st_mode) == 0o640
        assert (matched_stat.st_uid, matched_stat.st_gid) == (os.geteuid(), os.getegid())
        preserved = _stat_fingerprint(target)

        duplicate = _upload(operation, carrier, root, refused_content, Create())
        _assert_released(operation, root, duplicate)
        assert duplicate.status is FileUploadStatus.FAILED
        assert duplicate.failure is FileUploadFailure.PUBLICATION
        with pytest.raises(ConflictError) as duplicate_error:
            reduce_file_upload(duplicate, entity_kind="workspace file", entity_name="settings")
        assert duplicate_error.value.details is not None
        assert duplicate_error.value.details.reason is FileFailureReason.CONFLICT
        assert target.read_bytes() == updated_content
        assert _stat_fingerprint(target) == preserved

        stale = _upload(operation, carrier, root, refused_content, Match(created.revision))
        _assert_released(operation, root, stale)
        assert stale.status is FileUploadStatus.FAILED
        assert stale.failure is FileUploadFailure.PUBLICATION
        with pytest.raises(ConflictError) as stale_error:
            reduce_file_upload(stale, entity_kind="workspace file", entity_name="settings")
        assert stale_error.value.details is not None
        assert stale_error.value.details.reason is FileFailureReason.CONFLICT
        assert target.read_bytes() == updated_content
        assert _stat_fingerprint(target) == preserved
        assert tuple(root.iterdir()) == (target,)
        assert _all_attempts_succeeded(carrier)
        for attempt in range(len(carrier.attempts)):
            _assert_sensitive_attempt(
                carrier,
                attempt,
                str(root),
                _DESTINATION,
                "created-over-ssh",
                "updated-over-ssh",
                "must-not-replace-destination",
            )
        outcomes = (created, matched, duplicate, stale)
        for canary in ("created-over-ssh", "updated-over-ssh", "must-not-replace-destination"):
            assert canary not in repr(outcomes)

        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
        assert database.operations.inspect(owner.ownership.scope) is None
    finally:
        database.close()
