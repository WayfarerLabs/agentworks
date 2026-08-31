"""Checkpoint migration and repository lifecycle contracts."""

from __future__ import annotations

import pytest

from agentworks.db import (
    Database,
    VMCheckpointState,
)
from agentworks.debian import DebianRelease
from agentworks.errors import StateError


def _insert_vm(db: Database) -> None:
    db.insert_vm("box", site="lima-local", hostname="box")


def test_checkpoint_repository_fences_create_restore_and_delete(db: Database) -> None:
    _insert_vm(db)
    created = db.insert_vm_checkpoint(
        vm_name="box",
        name="agw-checkpoint",
        operation_id="create-op",
        desired_state_fingerprint="a" * 64,
        capture_release=DebianRelease.BOOKWORM,
    )
    assert created.state is VMCheckpointState.CREATING
    assert created.capture_release is DebianRelease.BOOKWORM

    assert db.complete_vm_checkpoint(
        "box",
        expected_state=VMCheckpointState.CREATING,
        operation_id="create-op",
        provider_identifier="provider-checkpoint",
    )
    ready = db.get_vm_checkpoint("box")
    assert ready is not None
    assert ready.state is VMCheckpointState.READY
    assert ready.provider_identifier == "provider-checkpoint"

    assert db.claim_vm_checkpoint(
        "box",
        expected_state=VMCheckpointState.READY,
        expected_operation_id=None,
        target_state=VMCheckpointState.RESTORING,
        operation_id="restore-op",
    )
    assert not db.complete_vm_checkpoint(
        "box",
        expected_state=VMCheckpointState.RESTORING,
        operation_id="stale-op",
    )
    assert db.complete_vm_checkpoint(
        "box",
        expected_state=VMCheckpointState.RESTORING,
        operation_id="restore-op",
    )

    assert db.claim_vm_checkpoint(
        "box",
        expected_state=VMCheckpointState.READY,
        expected_operation_id=None,
        target_state=VMCheckpointState.DELETING,
        operation_id="delete-op",
    )
    assert db.delete_claimed_vm_checkpoint("box", operation_id="delete-op")
    assert db.get_vm_checkpoint("box") is None


def test_checkpoint_repository_fences_materialized_interrupted_create_deletion(
    db: Database,
) -> None:
    _insert_vm(db)
    db.insert_vm_checkpoint(
        vm_name="box",
        name="agw-checkpoint",
        operation_id="create-op",
        desired_state_fingerprint="a" * 64,
        capture_release=DebianRelease.BOOKWORM,
    )

    assert db.claim_materialized_vm_checkpoint_deletion(
        "box",
        expected_operation_id="create-op",
        provider_identifier="provider-checkpoint",
        operation_id="delete-op",
    )
    assert not db.claim_materialized_vm_checkpoint_deletion(
        "box",
        expected_operation_id="create-op",
        provider_identifier="provider-checkpoint",
        operation_id="stale-delete-op",
    )
    claimed = db.get_vm_checkpoint("box")
    assert claimed is not None
    assert claimed.state is VMCheckpointState.DELETING
    assert claimed.operation_id == "delete-op"
    assert claimed.provider_identifier == "provider-checkpoint"


def test_checkpoint_converter_rejects_release_unknown_to_build(db: Database) -> None:
    _insert_vm(db)
    db.insert_vm_checkpoint(
        vm_name="box",
        name="agw-checkpoint",
        operation_id="create-op",
        desired_state_fingerprint="b" * 64,
        capture_release=DebianRelease.BOOKWORM,
    )
    db._conn.execute(
        "UPDATE vm_checkpoints SET capture_release = ? WHERE vm_name = ?",
        ("forky", "box"),
    )
    db._conn.commit()

    with pytest.raises(StateError):
        db.get_vm_checkpoint("box")


def test_vm_backup_projection_includes_checkpoint_row(db: Database) -> None:
    _insert_vm(db)
    db.insert_vm_checkpoint(
        vm_name="box",
        name="agw-checkpoint",
        operation_id="create-op",
        desired_state_fingerprint="c" * 64,
        capture_release=DebianRelease.BOOKWORM,
        source_release=DebianRelease.BOOKWORM,
        target_release=DebianRelease.TRIXIE,
    )

    vm, checkpoint, *_rest = db.snapshot_vm_backup_data("box")

    assert vm is not None
    assert checkpoint is not None
    assert checkpoint.capture_release is DebianRelease.BOOKWORM
    assert checkpoint.source_release is DebianRelease.BOOKWORM
    assert checkpoint.target_release is DebianRelease.TRIXIE
