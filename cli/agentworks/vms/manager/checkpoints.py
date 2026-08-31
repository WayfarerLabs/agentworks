"""Managed VM checkpoint lifecycle and provider reconciliation."""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform import CheckpointDescriptor
from agentworks.db import (
    PID_STOPPED,
    InitStatus,
    VMCheckpointState,
    VMStatus,
)
from agentworks.errors import AgentworksError, AlreadyExistsError, StateError, UserAbort

from ._helpers import _guard_failed_vm, _require_vm
from .boundary import _live_vm_boundary
from .checkpoint_fingerprint import checkpoint_desired_state_fingerprint
from .checkpoint_listing import CheckpointListing, CheckpointRestoreStatus
from .operation_guard import exclusive_vm_operation_guard

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMCheckpointRow, VMRow
    from agentworks.debian import DebianRelease
    from agentworks.resources import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy


def _new_checkpoint_name() -> str:
    return f"agw-{uuid.uuid4().hex}"


def _require_sessions_stopped(db: Database, vm_name: str) -> None:
    running = [row.name for row in db.list_sessions(vm_name=vm_name) if row.pid != PID_STOPPED]
    if not running:
        return
    raise StateError(
        f"VM '{vm_name}' has {output.count(len(running), 'Agentworks session')} that must be stopped: "
        + ", ".join(running),
        entity_kind="vm",
        entity_name=vm_name,
        hint=(
            f"Run 'agw session stop --all --vm {vm_name}', verify with "
            f"'agw session list --vm {vm_name}', then retry. A broken session may require --force."
        ),
    )


def _require_stopped_vm(platform: VMPlatform, vm: VMRow, ctx: RunContext) -> None:
    status = platform.status(vm, ctx)
    if status in {VMStatus.STOPPED, VMStatus.DEALLOCATED}:
        return
    raise StateError(
        f"VM '{vm.name}' must be stopped before this checkpoint operation (status: {status.value})",
        entity_kind="vm",
        entity_name=vm.name,
        hint=f"Run 'agw vm stop {vm.name}', then retry.",
    )


def _descriptor_from_row(row: VMCheckpointRow) -> CheckpointDescriptor:
    if row.provider_identifier is None:
        raise StateError(
            f"checkpoint '{row.name}' has no completed provider artifact",
            entity_kind="vm",
            entity_name=row.vm_name,
            hint="Rerun checkpoint creation to reconcile the interrupted provider operation.",
        )
    return CheckpointDescriptor(name=row.name, identifier=row.provider_identifier)


def _reconcile_ready_checkpoint(
    platform: VMPlatform,
    vm: VMRow,
    ctx: RunContext,
    row: VMCheckpointRow,
    *,
    inventory: Sequence[CheckpointDescriptor] | None = None,
) -> CheckpointDescriptor:
    if inventory is None:
        inventory = platform.list_checkpoints(vm, ctx)
    matching = [item for item in inventory if item.name == row.name]
    if len(inventory) != 1 or len(matching) != 1:
        raise StateError(
            f"VM '{vm.name}' checkpoint inventory disagrees with Agentworks state",
            entity_kind="vm",
            entity_name=vm.name,
            hint=(
                f"Expected exactly checkpoint '{row.name}', but the platform returned "
                f"{output.count(len(inventory), 'Agentworks-managed checkpoint')}. "
                "Repair the provider inventory before retrying."
            ),
        )
    descriptor = matching[0]
    if row.provider_identifier is not None and descriptor.identifier != row.provider_identifier:
        raise StateError(
            f"VM '{vm.name}' checkpoint identity disagrees with the platform",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Do not restore or delete the artifact until its ownership is repaired.",
        )
    return descriptor


def checkpoint_restore_status(
    db: Database,
    config: Config,
    registry: Registry,
    vm: VMRow,
    row: VMCheckpointRow,
) -> CheckpointRestoreStatus:
    """Derive whether the current declarations permit managed restore."""

    if row.state not in {VMCheckpointState.READY, VMCheckpointState.RESTORING}:
        return CheckpointRestoreStatus.UNAVAILABLE
    fingerprint = checkpoint_desired_state_fingerprint(
        db,
        config,
        registry,
        vm,
        capture_release=row.capture_release,
    )
    if fingerprint != row.desired_state_fingerprint:
        return CheckpointRestoreStatus.DECLARATIONS_CHANGED
    if row.state is VMCheckpointState.RESTORING:
        return CheckpointRestoreStatus.RESUME_REQUIRED
    return CheckpointRestoreStatus.AVAILABLE


def _validate_upgrade_checkpoint_slot(
    db: Database,
    config: Config,
    registry: Registry,
    platform: VMPlatform,
    vm: VMRow,
    ctx: RunContext,
    *,
    source_release: DebianRelease,
    target_release: DebianRelease,
) -> VMCheckpointRow | None:
    """Read and validate the one checkpoint slot without mutating it."""

    row = db.get_vm_checkpoint(vm.name)
    inventory = platform.list_checkpoints(vm, ctx)
    if row is None:
        if inventory:
            raise StateError(
                f"VM '{vm.name}' has an Agentworks-managed provider checkpoint with no database record",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Repair or remove the provider artifact before retrying the upgrade.",
            )
        return None
    expected_pair = (source_release, target_release)
    if (
        row.state not in {VMCheckpointState.CREATING, VMCheckpointState.READY}
        or row.capture_release is not source_release
        or (row.source_release, row.target_release) != expected_pair
    ):
        raise AlreadyExistsError(
            f"VM '{vm.name}' already has checkpoint '{row.name}' that cannot be reused for this upgrade",
            entity_kind="vm",
            entity_name=vm.name,
            hint=f"Restore or delete it with 'agw vm delete-checkpoint {vm.name}' before retrying.",
        )
    expected_fingerprint = checkpoint_desired_state_fingerprint(
        db,
        config,
        registry,
        vm,
        capture_release=row.capture_release,
    )
    if expected_fingerprint != row.desired_state_fingerprint:
        raise StateError(
            f"VM '{vm.name}' declarations changed after checkpoint '{row.name}' was claimed",
            entity_kind="vm",
            entity_name=vm.name,
            hint=f"Delete it with 'agw vm delete-checkpoint {vm.name}' before retrying the upgrade.",
        )
    if row.state is VMCheckpointState.READY:
        _reconcile_ready_checkpoint(platform, vm, ctx, row, inventory=inventory)
        return row
    matching = [item for item in inventory if item.name == row.name]
    if inventory and (len(inventory) != 1 or len(matching) != 1):
        raise StateError(
            f"VM '{vm.name}' checkpoint inventory disagrees with the interrupted create",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Repair the provider inventory before retrying the upgrade.",
        )
    return row


def _insert_or_resume_create(
    db: Database,
    config: Config,
    registry: Registry,
    vm: VMRow,
    *,
    capture_release: DebianRelease,
    source_release: DebianRelease | None,
    target_release: DebianRelease | None,
) -> tuple[VMCheckpointRow, bool]:
    fingerprint = checkpoint_desired_state_fingerprint(
        db,
        config,
        registry,
        vm,
        capture_release=capture_release,
    )
    existing = db.get_vm_checkpoint(vm.name)
    if existing is None:
        return (
            db.insert_vm_checkpoint(
                vm_name=vm.name,
                name=_new_checkpoint_name(),
                operation_id=uuid.uuid4().hex,
                desired_state_fingerprint=fingerprint,
                capture_release=capture_release,
                source_release=source_release,
                target_release=target_release,
            ),
            False,
        )
    if existing.state is not VMCheckpointState.CREATING:
        raise AlreadyExistsError(
            f"VM '{vm.name}' already has managed checkpoint '{existing.name}'",
            entity_kind="vm",
            entity_name=vm.name,
            hint=f"Delete it with 'agw vm delete-checkpoint {vm.name}' before creating another.",
        )
    expected_pair = (source_release, target_release)
    if (
        existing.capture_release is not capture_release
        or (existing.source_release, existing.target_release) != expected_pair
        or existing.desired_state_fingerprint != fingerprint
    ):
        raise StateError(
            f"VM '{vm.name}' has an interrupted checkpoint create for different desired state",
            entity_kind="vm",
            entity_name=vm.name,
            hint="Repair or delete the interrupted checkpoint before retrying.",
        )
    return existing, True


def _complete_create(
    db: Database,
    platform: VMPlatform,
    vm: VMRow,
    ctx: RunContext,
    row: VMCheckpointRow,
    *,
    resume: bool,
) -> VMCheckpointRow:
    inventory = platform.list_checkpoints(vm, ctx)
    if inventory:
        matching = [item for item in inventory if item.name == row.name]
        if len(inventory) != 1 or len(matching) != 1:
            raise StateError(
                f"VM '{vm.name}' already has an unrelated Agentworks-managed provider checkpoint",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Repair the provider inventory before retrying checkpoint creation.",
            )
    assert row.operation_id is not None
    descriptor = platform.create_checkpoint(
        vm,
        row.name,
        ctx,
        operation_id=row.operation_id,
        resume=resume,
    )
    inventory_descriptor = _reconcile_ready_checkpoint(platform, vm, ctx, row)
    if descriptor != inventory_descriptor:
        raise StateError(
            f"VM '{vm.name}' checkpoint create returned inconsistent provider identity",
            entity_kind="vm",
            entity_name=vm.name,
        )
    if not db.complete_vm_checkpoint(
        vm.name,
        expected_state=VMCheckpointState.CREATING,
        operation_id=row.operation_id,
        provider_identifier=descriptor.identifier,
    ):
        raise StateError(f"VM '{vm.name}' checkpoint state changed while creation was completing")
    completed = db.get_vm_checkpoint(vm.name)
    assert completed is not None
    db.insert_vm_event(
        vm.name,
        "checkpoint_created",
        json.dumps(
            {
                "capture_release": completed.capture_release.value,
                "checkpoint": completed.name,
                "provider_identifier": completed.provider_identifier,
                "purpose": completed.purpose,
            },
            sort_keys=True,
        ),
    )
    return completed


def create_checkpoint(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> VMCheckpointRow:
    """Create one offline operator checkpoint without changing power intent."""

    from agentworks.bootstrap import load_request_registry

    with exclusive_vm_operation_guard(db, name, operation="create checkpoint"):
        vm = _require_vm(db, name)
        _guard_failed_vm(vm)
        _require_sessions_stopped(db, name)
        if vm.debian_release is None:
            raise StateError(
                f"VM '{name}' has no recognized Debian release observation",
                entity_kind="vm",
                entity_name=name,
                hint=(
                    f"Start the VM, run 'agw vm reinit {name}' so Agentworks can observe Debian, "
                    f"then run 'agw vm stop {name}' and retry."
                ),
            )
        registry = load_request_registry(config, live_database=db)
        vm_node, ops_ctx = _live_vm_boundary(
            db,
            config,
            vm,
            registry=registry,
            interaction=interaction,
        )
        platform = vm_node.site.platform
        _require_stopped_vm(platform, vm, ops_ctx)
        if db.get_vm_checkpoint(name) is None:
            output.info("Checking provider checkpoint support and inventory...")
            inventory = platform.list_checkpoints(vm, ops_ctx)
            if inventory:
                raise StateError(
                    f"VM '{name}' has an Agentworks-managed provider checkpoint with no database record",
                    entity_kind="vm",
                    entity_name=name,
                    hint="Repair or remove the provider artifact before creating a checkpoint.",
                )
        row, resume = _insert_or_resume_create(
            db,
            config,
            registry,
            vm,
            capture_release=vm.debian_release,
            source_release=None,
            target_release=None,
        )
        output.info(f"Creating managed checkpoint for VM '{name}'...")
        completed = _complete_create(db, platform, vm, ops_ctx, row, resume=resume)
        output.result(f"Checkpoint '{completed.name}' created for VM '{name}'.")
        return completed


def create_upgrade_checkpoint(
    db: Database,
    config: Config,
    name: str,
    *,
    source_release: DebianRelease,
    target_release: DebianRelease,
    interaction: TtyInteractionPolicy,
) -> VMCheckpointRow:
    """Capture an upgrade checkpoint under exclusive VM ownership."""

    with exclusive_vm_operation_guard(db, name, operation="create upgrade checkpoint"):
        return _create_upgrade_checkpoint(
            db,
            config,
            name,
            source_release=source_release,
            target_release=target_release,
            interaction=interaction,
        )


def require_upgrade_checkpoint(
    db: Database,
    config: Config,
    name: str,
    *,
    expected_name: str,
    source_release: DebianRelease,
    target_release: DebianRelease,
    interaction: TtyInteractionPolicy,
) -> VMCheckpointRow:
    """Prove a journaled upgrade still owns its original recovery point."""

    from agentworks.bootstrap import load_request_registry

    with exclusive_vm_operation_guard(db, name, operation="resume Debian upgrade"):
        vm = _require_vm(db, name)
        _require_sessions_stopped(db, name)
        row = db.get_vm_checkpoint(name)
        if (
            row is None
            or row.name != expected_name
            or row.state is not VMCheckpointState.READY
            or row.capture_release is not source_release
            or row.source_release is not source_release
            or row.target_release is not target_release
        ):
            raise StateError(
                f"VM '{name}' no longer has the managed checkpoint recorded by its Debian upgrade journal",
                entity_kind="vm",
                entity_name=name,
                hint="Restore the matching Agentworks database and provider checkpoint before resuming.",
            )
        registry = load_request_registry(config, live_database=db)
        expected_fingerprint = checkpoint_desired_state_fingerprint(
            db,
            config,
            registry,
            vm,
            capture_release=row.capture_release,
        )
        if expected_fingerprint != row.desired_state_fingerprint:
            raise StateError(
                f"VM '{name}' desired state changed after its Debian upgrade checkpoint was created",
                entity_kind="vm",
                entity_name=name,
                hint="Restore the matching Agentworks database and declarations before resuming.",
            )
        vm_node, ops_ctx = _live_vm_boundary(
            db,
            config,
            vm,
            registry=registry,
            interaction=interaction,
        )
        _reconcile_ready_checkpoint(vm_node.site.platform, vm, ops_ctx, row)
        output.info(f"Confirmed managed upgrade checkpoint '{row.name}'.")
        return row


def preflight_upgrade_checkpoint(
    db: Database,
    config: Config,
    name: str,
    *,
    source_release: DebianRelease,
    target_release: DebianRelease,
    interaction: TtyInteractionPolicy,
) -> None:
    """Refuse an unusable checkpoint slot before creating backup artifacts."""

    from agentworks.bootstrap import load_request_registry

    vm = _require_vm(db, name)
    registry = load_request_registry(config, live_database=db)
    vm_node, ops_ctx = _live_vm_boundary(
        db,
        config,
        vm,
        registry=registry,
        interaction=interaction,
    )
    output.info("Checking managed checkpoint availability...")
    _validate_upgrade_checkpoint_slot(
        db,
        config,
        registry,
        vm_node.site.platform,
        vm,
        ops_ctx,
        source_release=source_release,
        target_release=target_release,
    )
    output.info("Managed checkpoint preflight complete.")


def _create_upgrade_checkpoint(
    db: Database,
    config: Config,
    name: str,
    *,
    source_release: DebianRelease,
    target_release: DebianRelease,
    interaction: TtyInteractionPolicy,
) -> VMCheckpointRow:
    """Capture the fresh upgrade source while preserving the active power state."""

    from agentworks.bootstrap import load_request_registry

    vm = _require_vm(db, name)
    _require_sessions_stopped(db, name)
    registry = load_request_registry(config, live_database=db)
    vm_node, ops_ctx = _live_vm_boundary(
        db,
        config,
        vm,
        registry=registry,
        interaction=interaction,
    )
    platform = vm_node.site.platform
    existing = _validate_upgrade_checkpoint_slot(
        db,
        config,
        registry,
        platform,
        vm,
        ops_ctx,
        source_release=source_release,
        target_release=target_release,
    )
    if existing is not None and existing.state is VMCheckpointState.READY:
        output.info(f"Reusing managed upgrade checkpoint '{existing.name}' created at {existing.created_at}.")
        return existing

    status = platform.status(vm, ops_ctx)
    if status is not VMStatus.RUNNING:
        raise StateError(
            f"VM '{name}' must be running when upgrade captures its managed checkpoint",
            entity_kind="vm",
            entity_name=name,
        )
    row, resume = _insert_or_resume_create(
        db,
        config,
        registry,
        vm,
        capture_release=source_release,
        source_release=source_release,
        target_release=target_release,
    )
    output.info(f"Stopping VM '{name}' for an offline managed checkpoint...")
    platform.stop(vm, ops_ctx)
    try:
        _require_stopped_vm(platform, vm, ops_ctx)
        output.info(f"Creating managed Debian {source_release.value} recovery checkpoint...")
        completed = _complete_create(db, platform, vm, ops_ctx, row, resume=resume)
    finally:
        output.info(f"Restarting VM '{name}' after checkpoint capture...")
        platform.start(vm, ops_ctx)
    output.result(f"Managed upgrade checkpoint '{completed.name}' is ready.")
    return completed


def list_checkpoints(
    db: Database,
    config: Config,
    *,
    vm_names: str | list[str] | None,
    names_only: bool = False,
    interaction: TtyInteractionPolicy,
) -> list[CheckpointListing]:
    """List persisted checkpoints after comparing each provider inventory."""

    from agentworks.bootstrap import load_request_registry
    from agentworks.name_filters import validate_name_filters

    validate_name_filters(db, vm_name=vm_names)
    rows = db.list_vm_checkpoints(vm_name=vm_names)
    if names_only:
        for row in rows:
            output.info(row.name)
        return [CheckpointListing(checkpoint=row, restore_status=None) for row in rows]
    registry = load_request_registry(config, live_database=db)
    listings: list[CheckpointListing] = []
    for row in rows:
        vm = _require_vm(db, row.vm_name)
        vm_node, ops_ctx = _live_vm_boundary(
            db,
            config,
            vm,
            registry=registry,
            interaction=interaction,
        )
        if row.state in {VMCheckpointState.READY, VMCheckpointState.RESTORING}:
            _reconcile_ready_checkpoint(vm_node.site.platform, vm, ops_ctx, row)
        listings.append(
            CheckpointListing(
                checkpoint=row,
                restore_status=checkpoint_restore_status(db, config, registry, vm, row),
            )
        )
    return listings


def restore_checkpoint(
    db: Database,
    config: Config,
    name: str,
    *,
    yes: bool,
    interaction: TtyInteractionPolicy,
) -> VMCheckpointRow:
    """Restore one managed checkpoint, attest Debian, and leave the VM stopped."""

    from agentworks.bootstrap import load_request_registry
    from agentworks.debian import probe_debian_release
    from agentworks.transports import (
        NativeTransportUnavailable,
        native_transport,
        transport,
        wait_for_reconnect,
    )

    with exclusive_vm_operation_guard(db, name, operation="restore checkpoint"):
        vm = _require_vm(db, name)
        _require_sessions_stopped(db, name)
        row = db.get_vm_checkpoint(name)
        if row is None:
            raise StateError(
                f"VM '{name}' has no managed checkpoint",
                entity_kind="vm",
                entity_name=name,
                hint=f"Create one with 'agw vm create-checkpoint {name}'.",
            )
        if row.state not in {VMCheckpointState.READY, VMCheckpointState.RESTORING}:
            raise StateError(
                f"checkpoint '{row.name}' is {row.state.value}; restore cannot begin",
                entity_kind="vm",
                entity_name=name,
            )
        registry = load_request_registry(config, live_database=db)
        fingerprint = checkpoint_desired_state_fingerprint(
            db,
            config,
            registry,
            vm,
            capture_release=row.capture_release,
        )
        if fingerprint != row.desired_state_fingerprint:
            raise StateError(
                f"VM '{name}' declarations no longer match checkpoint '{row.name}'",
                entity_kind="vm",
                entity_name=name,
                hint=(
                    "The VM, workspace, agent, session, site, or authorized-key configuration changed "
                    "after capture. Restore the matching Agentworks database and declarations, or delete "
                    f"this checkpoint with 'agw vm delete-checkpoint {name}' and create a new one. "
                    "Managed restore has no unsafe force bypass."
                ),
            )
        vm_node, ops_ctx = _live_vm_boundary(
            db,
            config,
            vm,
            registry=registry,
            interaction=interaction,
        )
        platform = vm_node.site.platform
        _require_stopped_vm(platform, vm, ops_ctx)
        descriptor = _reconcile_ready_checkpoint(platform, vm, ops_ctx, row)
        if not yes and not output.confirm(
            f"Restore VM '{name}' to its managed checkpoint? Current guest disk changes will be replaced.",
            default=False,
        ):
            raise UserAbort("checkpoint restore cancelled")
        if row.state is VMCheckpointState.READY:
            operation_id = uuid.uuid4().hex
            if not db.claim_vm_checkpoint(
                name,
                expected_state=VMCheckpointState.READY,
                expected_operation_id=None,
                target_state=VMCheckpointState.RESTORING,
                operation_id=operation_id,
            ):
                raise StateError(f"VM '{name}' checkpoint state changed before restore began")
        else:
            if row.operation_id is None:
                raise StateError(f"VM '{name}' interrupted checkpoint restore has no operation identity")
            operation_id = row.operation_id

        output.warn("Connected console clients, if any, will be disconnected during restore.")
        output.info(f"Restoring VM '{name}' from checkpoint '{row.name}'...")
        platform.restore_checkpoint(vm, descriptor, ops_ctx, operation_id=operation_id)
        _require_stopped_vm(platform, vm, ops_ctx)

        attested = False
        try:
            output.info("Starting the restored VM for Debian release verification...")
            platform.start(vm, ops_ctx)
            with contextlib.ExitStack() as stack:
                try:
                    target = native_transport(vm, platform, config, ctx=ops_ctx, stack=stack)
                except NativeTransportUnavailable:
                    target = transport(vm, config)
                    if not wait_for_reconnect(target):
                        raise StateError(
                            f"VM '{name}' did not reconnect after checkpoint restore",
                            entity_kind="vm",
                            entity_name=name,
                            hint="Use the platform console to repair connectivity, then rerun restore.",
                        ) from None
                output.info(f"Confirming restored Debian release {row.capture_release.value}...")
                observed = probe_debian_release(target, expected=row.capture_release)
            db.update_vm_debian_release(name, observed)
            db.update_vm_init_status(name, InitStatus.PENDING)
            db.insert_vm_event(
                name,
                "checkpoint_restored",
                json.dumps(
                    {
                        "checkpoint": row.name,
                        "provider_identifier": descriptor.identifier,
                        "release": observed.value,
                    },
                    sort_keys=True,
                ),
            )
            attested = True
        finally:
            output.info("Stopping the restored VM after verification...")
            platform.stop(vm, ops_ctx)
            _require_stopped_vm(platform, vm, ops_ctx)
        if not attested:
            raise StateError(f"VM '{name}' checkpoint restore could not be attested")
        if not db.complete_vm_checkpoint(
            name,
            expected_state=VMCheckpointState.RESTORING,
            operation_id=operation_id,
        ):
            raise StateError(f"VM '{name}' checkpoint state changed while restore was completing")
        completed = db.get_vm_checkpoint(name)
        assert completed is not None
        output.result(
            f"VM '{name}' restored to Debian {completed.capture_release.value} and stopped. "
            f"Run 'agw vm reinit {name}' before relying on guest convergence."
        )
        return completed


def delete_checkpoint(
    db: Database,
    config: Config,
    name: str,
    *,
    yes: bool,
    force: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Delete one provider checkpoint and release the VM's managed slot."""

    with exclusive_vm_operation_guard(db, name, operation="delete checkpoint"):
        vm = _require_vm(db, name)
        try:
            vm_node, ops_ctx = _live_vm_boundary(db, config, vm, interaction=interaction)
        except UserAbort:
            raise
        except AgentworksError as error:
            if not force:
                _raise_checkpoint_cleanup_failure(name, error)
            _abandon_checkpoint(db, name, error=error, yes=yes)
            return
        _delete_checkpoint_with_boundary(
            db,
            vm,
            vm_node.site.platform,
            ops_ctx,
            yes=yes,
            force=force,
        )


def _delete_checkpoint_with_boundary(
    db: Database,
    vm: VMRow,
    platform: VMPlatform,
    ops_ctx: RunContext,
    *,
    yes: bool,
    force: bool = False,
) -> None:
    """Delete a managed checkpoint through an already-resolved VM boundary."""

    try:
        _delete_checkpoint_reconciled(db, vm, platform, ops_ctx, yes=yes)
    except UserAbort:
        raise
    except AgentworksError as error:
        if not force:
            _raise_checkpoint_cleanup_failure(vm.name, error)
        _abandon_checkpoint(db, vm.name, error=error, yes=yes)


def _delete_checkpoint_reconciled(
    db: Database,
    vm: VMRow,
    platform: VMPlatform,
    ops_ctx: RunContext,
    *,
    yes: bool,
) -> None:
    """Reconcile and delete a managed checkpoint without abandoning ownership."""

    name = vm.name
    row = db.get_vm_checkpoint(name)
    if row is None:
        raise StateError(
            f"VM '{name}' has no managed checkpoint",
            entity_kind="vm",
            entity_name=name,
        )
    inventory = platform.list_checkpoints(vm, ops_ctx)
    confirmed = False
    claimed_operation_id: str | None = None
    if row.state is VMCheckpointState.CREATING:
        _require_stopped_vm(platform, vm, ops_ctx)
        matching = [item for item in inventory if item.name == row.name]
        if inventory and (len(inventory) != 1 or len(matching) != 1):
            raise StateError(
                f"VM '{name}' checkpoint inventory disagrees with the interrupted create",
                entity_kind="vm",
                entity_name=name,
                hint=(
                    f"Expected exactly checkpoint '{row.name}', but the platform returned "
                    f"{output.count(len(inventory), 'Agentworks-managed checkpoint')}. "
                    "Repair the provider inventory before retrying."
                ),
            )
        _confirm_checkpoint_deletion(name, yes=yes)
        confirmed = True
        if matching:
            descriptor = matching[0]
            assert row.operation_id is not None
            claimed_operation_id = uuid.uuid4().hex
            if not db.claim_materialized_vm_checkpoint_deletion(
                name,
                expected_operation_id=row.operation_id,
                provider_identifier=descriptor.identifier,
                operation_id=claimed_operation_id,
            ):
                raise StateError(f"VM '{name}' checkpoint state changed before deletion began")
            row = db.get_vm_checkpoint(name)
            assert row is not None
        else:
            output.info(f"Reconciling interrupted checkpoint creation for VM '{name}' before deletion...")
            row = _complete_create(db, platform, vm, ops_ctx, row, resume=True)
            inventory = platform.list_checkpoints(vm, ops_ctx)
    if row.state is VMCheckpointState.DELETING and not inventory:
        _confirm_checkpoint_deletion(name, yes=yes)
        descriptor = _descriptor_from_row(row)
        output.info(f"Finishing managed checkpoint cleanup for VM '{name}'...")
        platform.delete_checkpoint(vm, descriptor, ops_ctx)
        if platform.list_checkpoints(vm, ops_ctx):
            raise StateError("platform still reports Agentworks-managed checkpoints after deletion")
        assert row.operation_id is not None
        if not db.delete_claimed_vm_checkpoint(name, operation_id=row.operation_id):
            raise StateError(f"VM '{name}' checkpoint state changed while deletion was completing")
        db.insert_vm_event(
            name,
            "checkpoint_deleted",
            json.dumps(
                {"checkpoint": row.name, "provider_identifier": row.provider_identifier},
                sort_keys=True,
            ),
        )
        output.result(f"Checkpoint '{row.name}' deletion reconciled for VM '{name}'.")
        return
    if row.state not in {VMCheckpointState.READY, VMCheckpointState.DELETING}:
        raise StateError(
            f"checkpoint '{row.name}' is {row.state.value}; deletion cannot begin",
            entity_kind="vm",
            entity_name=name,
        )
    descriptor = _reconcile_ready_checkpoint(platform, vm, ops_ctx, row)
    if not confirmed:
        _confirm_checkpoint_deletion(name, yes=yes)
    operation_id = claimed_operation_id or uuid.uuid4().hex
    if claimed_operation_id is None and not db.claim_vm_checkpoint(
        name,
        expected_state=row.state,
        expected_operation_id=row.operation_id,
        target_state=VMCheckpointState.DELETING,
        operation_id=operation_id,
    ):
        raise StateError(f"VM '{name}' checkpoint state changed before deletion began")
    output.info(f"Deleting managed checkpoint '{row.name}' for VM '{name}'...")
    platform.delete_checkpoint(vm, descriptor, ops_ctx)
    remaining = platform.list_checkpoints(vm, ops_ctx)
    if remaining:
        raise StateError("platform still reports Agentworks-managed checkpoints after deletion")
    if not db.delete_claimed_vm_checkpoint(name, operation_id=operation_id):
        raise StateError(f"VM '{name}' checkpoint state changed while deletion was completing")
    db.insert_vm_event(
        name,
        "checkpoint_deleted",
        json.dumps(
            {"checkpoint": row.name, "provider_identifier": descriptor.identifier},
            sort_keys=True,
        ),
    )
    output.result(f"Checkpoint '{row.name}' deleted for VM '{name}'.")


def _raise_checkpoint_cleanup_failure(name: str, error: AgentworksError) -> None:
    provider_hint = f"{error.hint} " if error.hint else ""
    raise StateError(
        f"Managed checkpoint cleanup for VM '{name}' could not complete: {error}",
        entity_kind="vm",
        entity_name=name,
        hint=(
            f"{provider_hint}Correct the provider problem and retry. If provider cleanup cannot be recovered, "
            f"run 'agw vm delete-checkpoint {name} --force' to make Agentworks forget ownership; "
            "provider artifacts may remain and continue billing."
        ),
    ) from error


def _abandon_checkpoint(
    db: Database,
    name: str,
    *,
    error: BaseException,
    yes: bool,
) -> None:
    """Explicitly forget ownership after provider cleanup could not be proved."""

    row = db.get_vm_checkpoint(name)
    if row is None:
        raise StateError(f"VM '{name}' has no managed checkpoint to forget") from error
    output.warn(f"Provider checkpoint cleanup could not be confirmed: {error}")
    provider_identifier = row.provider_identifier or "not recorded"
    output.warn(
        f"Forgetting checkpoint '{row.name}' in Agentworks with provider identifier "
        f"'{provider_identifier}'. Provider artifacts, including late or additional artifacts, "
        "may remain and continue billing; inspect and clean them up with the provider's tools."
    )
    if not yes and not output.confirm(
        f"Forget Agentworks ownership of checkpoint '{row.name}' for VM '{name}'?",
        default=False,
    ):
        raise UserAbort("checkpoint abandonment cancelled")
    if not db.abandon_vm_checkpoint(
        name,
        expected_state=row.state,
        expected_operation_id=row.operation_id,
    ):
        raise StateError(f"VM '{name}' checkpoint state changed before ownership was forgotten")
    db.insert_vm_event(
        name,
        "checkpoint_abandoned",
        json.dumps(
            {
                "checkpoint": row.name,
                "provider_identifier": row.provider_identifier,
                "state": row.state.value,
            },
            sort_keys=True,
        ),
    )
    output.result(f"Agentworks forgot checkpoint '{row.name}' for VM '{name}'. Provider cleanup was not verified.")


def _confirm_checkpoint_deletion(name: str, *, yes: bool) -> None:
    if not yes and not output.confirm(f"Delete the managed checkpoint for VM '{name}'?", default=False):
        raise UserAbort("checkpoint deletion cancelled")
