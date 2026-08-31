"""Core managed-checkpoint lifecycle and restore attestation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.vm_platform import CheckpointDescriptor
from agentworks.db import Database, InitStatus, VMCheckpointState, VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import AlreadyExistsError, StateError
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms.manager import checkpoints


class _Platform:
    name = "test-platform"

    def __init__(self) -> None:
        self.power = VMStatus.STOPPED
        self.inventory: tuple[CheckpointDescriptor, ...] = ()
        self.created: list[CheckpointDescriptor] = []
        self.create_attempts: list[tuple[str, bool]] = []
        self.restored: list[CheckpointDescriptor] = []
        self.restore_operation_ids: list[str] = []
        self.deleted: list[CheckpointDescriptor] = []

    def status(self, vm: object, ctx: object) -> VMStatus:
        del vm, ctx
        return self.power

    def start(self, vm: object, ctx: object) -> None:
        del vm, ctx
        self.power = VMStatus.RUNNING

    def stop(self, vm: object, ctx: object) -> None:
        del vm, ctx
        self.power = VMStatus.STOPPED

    def create_checkpoint(
        self,
        vm: object,
        name: str,
        ctx: object,
        *,
        operation_id: str,
        resume: bool,
    ) -> CheckpointDescriptor:
        del vm, ctx
        self.create_attempts.append((operation_id, resume))
        descriptor = CheckpointDescriptor(name, f"provider-{name}")
        self.created.append(descriptor)
        self.inventory = (descriptor,)
        return descriptor

    def list_checkpoints(self, vm: object, ctx: object) -> tuple[CheckpointDescriptor, ...]:
        del vm, ctx
        return self.inventory

    def restore_checkpoint(
        self,
        vm: object,
        checkpoint: CheckpointDescriptor,
        ctx: object,
        *,
        operation_id: str,
    ) -> None:
        del vm, ctx
        self.restored.append(checkpoint)
        self.restore_operation_ids.append(operation_id)
        self.power = VMStatus.STOPPED

    def delete_checkpoint(
        self,
        vm: object,
        checkpoint: CheckpointDescriptor,
        ctx: object,
    ) -> None:
        del vm, ctx
        self.deleted.append(checkpoint)
        self.inventory = tuple(item for item in self.inventory if item != checkpoint)


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
    platform: _Platform,
) -> object:
    db.insert_vm("box", site="lima-local", hostname="box")
    db.update_vm_debian_release("box", DebianRelease.BOOKWORM)
    registry = object()
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *args, **kwargs: registry)
    monkeypatch.setattr(
        checkpoints,
        "_live_vm_boundary",
        lambda *args, **kwargs: (SimpleNamespace(site=SimpleNamespace(platform=platform)), object()),
    )
    monkeypatch.setattr(
        checkpoints,
        "checkpoint_desired_state_fingerprint",
        lambda *args, **kwargs: "a" * 64,
    )
    return registry


def test_create_checkpoint_persists_capture_release_and_one_slot(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)

    row = checkpoints.create_checkpoint(
        db,
        object(),
        "box",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert row.state is VMCheckpointState.READY
    assert row.purpose == "operator"
    assert row.capture_release is DebianRelease.BOOKWORM
    assert row.provider_identifier == f"provider-{row.name}"
    assert platform.created == [CheckpointDescriptor(row.name, row.provider_identifier)]
    with pytest.raises(AlreadyExistsError):
        checkpoints.create_checkpoint(
            db,
            object(),
            "box",
            interaction=TtyInteractionPolicy.REFUSE,
        )


def test_create_refuses_unrelated_provider_inventory_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    platform.inventory = (CheckpointDescriptor("agw-orphan", "provider-orphan"),)

    with pytest.raises(StateError):
        checkpoints.create_checkpoint(
            db,
            object(),
            "box",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert platform.created == []
    row = db.get_vm_checkpoint("box")
    assert row is not None
    assert row.state is VMCheckpointState.CREATING


def test_checkpoint_listing_is_available_without_an_operation_guard(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    created = checkpoints.create_checkpoint(
        db,
        object(),
        "box",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    def _unexpected_guard(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("read-only checkpoint listing acquired an operation guard")

    monkeypatch.setattr(checkpoints, "exclusive_vm_operation_guard", _unexpected_guard)

    rows = checkpoints.list_checkpoints(
        db,
        object(),
        vm_names=None,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert rows == [created]


def test_upgrade_checkpoint_running_precondition_leaves_slot_empty(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)

    with pytest.raises(StateError):
        checkpoints.create_upgrade_checkpoint(
            db,
            object(),
            "box",
            source_release=DebianRelease.BOOKWORM,
            target_release=DebianRelease.TRIXIE,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert db.get_vm_checkpoint("box") is None


def test_upgrade_resume_never_replaces_a_missing_journaled_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    platform.power = VMStatus.RUNNING

    with pytest.raises(StateError):
        checkpoints.require_upgrade_checkpoint(
            db,
            object(),
            "box",
            expected_name="journal-checkpoint",
            source_release=DebianRelease.BOOKWORM,
            target_release=DebianRelease.TRIXIE,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert db.get_vm_checkpoint("box") is None
    assert platform.inventory == ()


def test_destructive_checkpoint_commands_validate_before_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    prompts: list[object] = []
    monkeypatch.setattr(
        "agentworks.vms.manager.checkpoints.output.confirm",
        lambda *args, **kwargs: prompts.append((args, kwargs)),
    )

    with pytest.raises(StateError):
        checkpoints.restore_checkpoint(
            db,
            object(),
            "box",
            yes=False,
            interaction=TtyInteractionPolicy.REFUSE,
        )
    with pytest.raises(StateError):
        checkpoints.delete_checkpoint(
            db,
            object(),
            "box",
            yes=False,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert prompts == []


def test_restore_uses_capture_release_after_target_was_observed(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    created = checkpoints.create_checkpoint(
        db,
        object(),
        "box",
        interaction=TtyInteractionPolicy.REFUSE,
    )
    db.update_vm_debian_release("box", DebianRelease.TRIXIE)
    seen_expected: list[DebianRelease] = []

    monkeypatch.setattr("agentworks.transports.native_transport", lambda *args, **kwargs: object())

    def _probe(target: object, *, expected: DebianRelease) -> DebianRelease:
        del target
        seen_expected.append(expected)
        return expected

    monkeypatch.setattr("agentworks.debian.probe_debian_release", _probe)

    restored = checkpoints.restore_checkpoint(
        db,
        object(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
    restored_again = checkpoints.restore_checkpoint(
        db,
        object(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    descriptor = CheckpointDescriptor(created.name, f"provider-{created.name}")
    assert seen_expected == [DebianRelease.BOOKWORM, DebianRelease.BOOKWORM]
    assert platform.restored == [descriptor, descriptor]
    assert len(set(platform.restore_operation_ids)) == 2
    assert platform.power is VMStatus.STOPPED
    assert restored.state is VMCheckpointState.READY
    assert restored_again.state is VMCheckpointState.READY
    vm = db.get_vm("box")
    assert vm is not None
    assert vm.debian_release is DebianRelease.BOOKWORM
    assert vm.init_status == InitStatus.PENDING.value


def test_restore_refuses_desired_state_drift_before_provider_mutation(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    checkpoints.create_checkpoint(
        db,
        object(),
        "box",
        interaction=TtyInteractionPolicy.REFUSE,
    )
    monkeypatch.setattr(
        checkpoints,
        "checkpoint_desired_state_fingerprint",
        lambda *args, **kwargs: "b" * 64,
    )

    with pytest.raises(StateError):
        checkpoints.restore_checkpoint(
            db,
            object(),
            "box",
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert platform.restored == []


def test_interrupted_restore_reuses_its_persisted_operation_identity(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    checkpoints.create_checkpoint(
        db,
        object(),
        "box",
        interaction=TtyInteractionPolicy.REFUSE,
    )
    operation_id = "interrupted-restore"
    assert db.claim_vm_checkpoint(
        "box",
        expected_state=VMCheckpointState.READY,
        expected_operation_id=None,
        target_state=VMCheckpointState.RESTORING,
        operation_id=operation_id,
    )
    monkeypatch.setattr("agentworks.transports.native_transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "agentworks.debian.probe_debian_release",
        lambda _target, *, expected: expected,
    )

    restored = checkpoints.restore_checkpoint(
        db,
        object(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert platform.restore_operation_ids == [operation_id]
    assert restored.state is VMCheckpointState.READY


def test_delete_checkpoint_proves_provider_absence_before_releasing_slot(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    created = checkpoints.create_checkpoint(
        db,
        object(),
        "box",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    checkpoints.delete_checkpoint(
        db,
        object(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert platform.deleted == [CheckpointDescriptor(created.name, f"provider-{created.name}")]
    assert db.get_vm_checkpoint("box") is None


def test_delete_checkpoint_replays_provider_cleanup_after_primary_disappears(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    checkpoints_row = checkpoints.create_checkpoint(
        db,
        object(),
        "box",
        interaction=TtyInteractionPolicy.REFUSE,
    )
    operation_id = "interrupted-delete"
    assert db.claim_vm_checkpoint(
        "box",
        expected_state=VMCheckpointState.READY,
        expected_operation_id=None,
        target_state=VMCheckpointState.DELETING,
        operation_id=operation_id,
    )
    platform.inventory = ()

    checkpoints.delete_checkpoint(
        db,
        object(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    row = db.get_vm_checkpoint("box")
    assert row is None
    assert platform.deleted == [CheckpointDescriptor(checkpoints_row.name, checkpoints_row.provider_identifier)]


def test_delete_checkpoint_removes_materialized_interrupted_create(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    row = db.insert_vm_checkpoint(
        vm_name="box",
        name="agw-interrupted",
        operation_id="create-operation",
        desired_state_fingerprint="a" * 64,
        capture_release=DebianRelease.BOOKWORM,
        source_release=None,
        target_release=None,
    )
    descriptor = CheckpointDescriptor(row.name, f"provider-{row.name}")
    platform.inventory = (descriptor,)

    checkpoints.delete_checkpoint(
        db,
        object(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert platform.deleted == [descriptor]
    assert platform.create_attempts == []
    assert db.get_vm_checkpoint("box") is None


def test_delete_materialized_interrupted_create_replays_after_provider_delete(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    row = db.insert_vm_checkpoint(
        vm_name="box",
        name="agw-interrupted",
        operation_id="create-operation",
        desired_state_fingerprint="a" * 64,
        capture_release=DebianRelease.BOOKWORM,
        source_release=None,
        target_release=None,
    )
    descriptor = CheckpointDescriptor(row.name, f"provider-{row.name}")
    platform.inventory = (descriptor,)
    provider_delete = platform.delete_checkpoint

    def _delete_then_interrupt(
        vm: object,
        checkpoint: CheckpointDescriptor,
        ctx: object,
    ) -> None:
        provider_delete(vm, checkpoint, ctx)
        raise StateError("simulated interruption after provider deletion")

    monkeypatch.setattr(platform, "delete_checkpoint", _delete_then_interrupt)
    with pytest.raises(StateError):
        checkpoints.delete_checkpoint(
            db,
            object(),
            "box",
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    interrupted = db.get_vm_checkpoint("box")
    assert interrupted is not None
    assert interrupted.state is VMCheckpointState.DELETING
    assert interrupted.provider_identifier == descriptor.identifier
    assert interrupted.operation_id != "create-operation"

    monkeypatch.setattr(platform, "delete_checkpoint", provider_delete)
    checkpoints.delete_checkpoint(
        db,
        object(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert platform.create_attempts == []
    assert db.get_vm_checkpoint("box") is None


def test_delete_checkpoint_finishes_an_unobserved_create_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    row = db.insert_vm_checkpoint(
        vm_name="box",
        name="agw-interrupted",
        operation_id="create-operation",
        desired_state_fingerprint="a" * 64,
        capture_release=DebianRelease.BOOKWORM,
        source_release=None,
        target_release=None,
    )

    checkpoints.delete_checkpoint(
        db,
        object(),
        "box",
        yes=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    descriptor = CheckpointDescriptor(row.name, f"provider-{row.name}")
    assert platform.create_attempts == [("create-operation", True)]
    assert platform.created == [descriptor]
    assert platform.deleted == [descriptor]
    assert db.get_vm_checkpoint("box") is None


def test_delete_interrupted_create_retains_row_when_provider_inventory_disagrees(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
) -> None:
    platform = _Platform()
    _prepare(monkeypatch, db, platform)
    db.insert_vm_checkpoint(
        vm_name="box",
        name="agw-interrupted",
        operation_id="create-operation",
        desired_state_fingerprint="a" * 64,
        capture_release=DebianRelease.BOOKWORM,
        source_release=None,
        target_release=None,
    )
    platform.inventory = (CheckpointDescriptor("agw-other", "provider-other"),)

    with pytest.raises(StateError):
        checkpoints.delete_checkpoint(
            db,
            object(),
            "box",
            yes=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert db.get_vm_checkpoint("box") is not None
    assert platform.deleted == []
