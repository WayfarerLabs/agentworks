"""Driver-aware managed checkpoints for Lima instances."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentworks import output
from agentworks.capabilities.vm_platform.base import CheckpointDescriptor
from agentworks.capabilities.vm_platform.lima_checkpoint_host import LimaCheckpointHost, LimaDirectoryRename
from agentworks.errors import StateError
from agentworks.ssh import SSHError

_MANAGED_CHECKPOINT_NAME = re.compile(r"agw-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_CLONE_CHECKPOINT_NAME = re.compile(r"agw-(?P<identity>[a-f0-9]{32})")
_RECOVERY_ARTIFACT_NAME = re.compile(
    r"agw(?P<role>cp|em|rs|rd)-(?P<source>[a-f0-9]{8})-"
    r"(?P<checkpoint>[a-f0-9]{32})(?:-(?P<operation>[a-f0-9]{8}))?"
)
_CHECKPOINT_NAME_PARAM = "agentworksCheckpointName"
_CHECKPOINT_SOURCE_PARAM = "agentworksCheckpointSource"
_SOURCE_INCARNATION_PARAM = "agentworksVmIncarnation"
_LAST_RESTORE_PARAM = "agentworksCheckpointLastRestore"
_RECOVERY_ROLE_PARAM = "agentworksRecoveryRole"
_RECOVERY_CHECKPOINT_PARAM = "agentworksRecoveryCheckpoint"
_RECOVERY_SOURCE_PARAM = "agentworksRecoverySource"
_RECOVERY_OPERATION_PARAM = "agentworksRecoveryOperation"

LimaRunner = Callable[..., str]


@dataclass(frozen=True, slots=True)
class _RecoveryNames:
    checkpoint: str
    emergency: str
    stage: str
    discard: str


class LimaCheckpointOperations:
    """Use QEMU snapshots or VZ clones for one Lima instance's checkpoints."""

    def __init__(
        self,
        *,
        vm_name: str,
        instance_name: str,
        run: LimaRunner,
        rename: LimaDirectoryRename | None,
    ) -> None:
        self.vm_name = vm_name
        self.instance_name = instance_name
        self.run = run
        self.host = LimaCheckpointHost(vm_name=vm_name, run=run, rename=rename)
        self.source_identity = hashlib.sha256(instance_name.encode()).hexdigest()[:8]

    def create(
        self,
        name: str,
        *,
        operation_id: str,
    ) -> CheckpointDescriptor:
        name = self._require_checkpoint_name(name)
        clone_name_match = _CLONE_CHECKPOINT_NAME.fullmatch(name)
        recovery_target = (
            f"agwcp-{self.source_identity}-{clone_name_match.group('identity')}"
            if clone_name_match is not None
            else None
        )
        recovery_artifacts = self._recovery_artifact_names(recovery_target=recovery_target)
        clone_inventory = self._clone_descriptors(recovery_artifacts)
        matches = [item for item in clone_inventory if item.name == name]
        if matches:
            self._require_one_descriptor(name, clone_inventory)
            self.host.require_atomic_rename()
            descriptor = matches[0]
            self._finish_clone_checkpoint(descriptor)
            return descriptor
        if recovery_artifacts:
            raise StateError(
                f"Lima VM '{self.vm_name}' has unreconciled Agentworks recovery artifacts",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start or rename those instances. Inspect them with 'limactl list' before retrying.",
            )

        source_record = self.host.record(self.instance_name)
        assert source_record is not None
        backend = self.host.backend(source_record, self.instance_name)
        self.host.require_stopped(
            self.instance_name,
            purpose="checkpoint creation",
            record=source_record,
        )
        if backend == "qemu":
            return self._create_snapshot(name)
        if backend != "vz":
            self._raise_unsupported_backend(backend)

        self.host.require_atomic_rename()
        clone_descriptor = self._clone_descriptor(name)
        self.host.require_no_additional_disks(self.instance_name, record=source_record)
        self._ensure_source_incarnation(operation_id, source_record)
        output.info(f"Creating stopped Lima VZ recovery clone '{clone_descriptor.identifier}'...")
        output.detail(
            "Lima will use copy-on-write storage when the host filesystem supports it. "
            "The clone can grow as the VM changes."
        )
        expression = (
            f".param.{_CHECKPOINT_NAME_PARAM} = {json.dumps(name)} | "
            f".param.{_CHECKPOINT_SOURCE_PARAM} = {json.dumps(self.instance_name)}"
        )
        try:
            self.run(
                "limactl clone --tty=false --set "
                f"{shlex.quote(expression)} {shlex.quote(self.instance_name)} "
                f"{clone_descriptor.identifier}"
            )
        except KeyboardInterrupt:
            output.warn(
                f"Lima checkpoint clone '{clone_descriptor.identifier}' may still exist. "
                "Rerun the checkpoint command to reconcile it."
            )
            raise
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not create checkpoint '{name}' for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    "The VM remains stopped and no Debian package mutation has begun. "
                    "Correct the Lima clone failure, then retry."
                ),
            ) from error
        self._finish_clone_checkpoint(clone_descriptor)
        output.warn(
            f"Lima recovery clone '{clone_descriptor.identifier}' is stopped and protected. "
            "Do not start it: it contains the same guest and Tailscale identity as the VM."
        )
        return clone_descriptor

    def list(self) -> tuple[CheckpointDescriptor, ...]:
        recovery_artifacts = self._recovery_artifact_names()
        descriptors = self._clone_descriptors(recovery_artifacts)
        if recovery_artifacts:
            source_record = self.host.record(self.instance_name, required=False)
            for descriptor in descriptors:
                record = self.host.record(descriptor.identifier)
                assert record is not None
                self._validate_clone_checkpoint_record(descriptor, record, source_record)
            return descriptors
        record = self.host.record(self.instance_name, required=False)
        if record is None:
            return ()
        backend = self.host.backend(record, self.instance_name)
        if backend == "vz":
            return ()
        if backend != "qemu":
            self._raise_unsupported_backend(backend)
        return tuple(CheckpointDescriptor(name=name, identifier=name) for name in sorted(self._snapshot_names()))

    def restore(self, checkpoint: CheckpointDescriptor, *, operation_id: str) -> None:
        name = self._require_checkpoint_name(checkpoint.name)
        if checkpoint.identifier == name:
            self._restore_snapshot(checkpoint)
            return
        clone_descriptor = self._clone_descriptor(name)
        if checkpoint != clone_descriptor:
            raise StateError(
                f"Lima checkpoint '{name}' has a conflicting provider identifier",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        self._restore_clone(clone_descriptor, operation_id=operation_id)

    def delete(self, checkpoint: CheckpointDescriptor) -> None:
        name = self._require_checkpoint_name(checkpoint.name)
        if checkpoint.identifier == name:
            self._delete_snapshot(checkpoint)
            return
        clone_descriptor = self._clone_descriptor(name)
        if checkpoint != clone_descriptor:
            raise StateError(
                f"Lima checkpoint '{name}' has a conflicting provider identifier",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

        all_names = self.host.names()
        related = self._related_recovery_artifacts(clone_descriptor, all_names)
        checkpoint_artifact = clone_descriptor.identifier
        ordered = sorted(item for item in related if item != checkpoint_artifact)
        if checkpoint_artifact in related:
            ordered.append(checkpoint_artifact)
        for artifact in ordered:
            if artifact == checkpoint_artifact:
                record = self.host.record(checkpoint_artifact)
                assert record is not None
                source_record = self.host.record(self.instance_name, required=False)
                self._validate_clone_checkpoint_record(clone_descriptor, record, source_record)
            else:
                self._validate_owned_recovery_artifact(clone_descriptor, artifact)
        for artifact in ordered:
            output.info(f"Deleting Lima recovery artifact '{artifact}'...")
            self.host.delete_recovery_instance(artifact)
        if self._related_recovery_artifacts(clone_descriptor, self.host.names()):
            raise StateError(
                f"Lima checkpoint '{name}' still has recovery artifacts after deletion",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _create_snapshot(self, name: str) -> CheckpointDescriptor:
        existing = self._snapshot_names()
        if name in existing:
            self._require_one_snapshot(name, existing)
            return CheckpointDescriptor(name=name, identifier=name)
        try:
            self.run(f"limactl snapshot create {shlex.quote(self.instance_name)} --tag {name}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not create checkpoint '{name}' for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    "Confirm that this Lima version and QEMU driver support snapshots; "
                    "the VM remains stopped and no Debian package mutation has begun."
                ),
            ) from error
        self._require_one_snapshot(name, self._snapshot_names())
        return CheckpointDescriptor(name=name, identifier=name)

    def _restore_snapshot(self, checkpoint: CheckpointDescriptor) -> None:
        if checkpoint.identifier != checkpoint.name:
            raise StateError(
                f"Lima checkpoint '{checkpoint.name}' has a conflicting provider identifier",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        self.host.require_stopped(self.instance_name, purpose="checkpoint restore")
        self._require_one_snapshot(checkpoint.name, self._snapshot_names())
        try:
            self.run(f"limactl snapshot apply {shlex.quote(self.instance_name)} --tag {checkpoint.name}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not restore checkpoint '{checkpoint.name}' for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            ) from error
        self._require_one_snapshot(checkpoint.name, self._snapshot_names())
        self.host.require_stopped(self.instance_name, purpose="checkpoint restore")

    def _delete_snapshot(self, checkpoint: CheckpointDescriptor) -> None:
        if checkpoint.identifier != checkpoint.name:
            raise StateError(
                f"Lima checkpoint '{checkpoint.name}' has a conflicting provider identifier",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        existing = self._snapshot_names()
        if checkpoint.name not in existing:
            return
        self._require_one_snapshot(checkpoint.name, existing)
        try:
            self.run(f"limactl snapshot delete {shlex.quote(self.instance_name)} --tag {checkpoint.name}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not delete checkpoint '{checkpoint.name}' for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            ) from error
        if checkpoint.name in self._snapshot_names():
            raise StateError(
                f"Lima checkpoint '{checkpoint.name}' still exists after deletion",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _restore_clone(
        self,
        checkpoint: CheckpointDescriptor,
        *,
        operation_id: str,
    ) -> None:
        self.host.require_atomic_rename()
        names = self._restore_names(checkpoint)
        artifacts = set(self.host.names(recovery_target=names.stage))
        source_record = self.host.record(self.instance_name) if self.instance_name in artifacts else None
        checkpoint_record = self._require_clone_checkpoint(checkpoint, source_record)
        if self._last_restore(checkpoint_record) == operation_id:
            self._finish_completed_restore(
                checkpoint,
                checkpoint_record,
                names,
                artifacts,
                source_record,
                operation_id=operation_id,
            )
            return

        stage_exists = names.stage in artifacts
        discard_exists = names.discard in artifacts
        emergency_exists = names.emergency in artifacts
        self._validate_recovery_instances(
            checkpoint,
            checkpoint_record,
            tuple(artifact for artifact in (names.stage, names.discard, names.emergency) if artifact in artifacts),
            operation_id=operation_id,
        )

        if source_record is not None:
            self.host.require_stopped_vz_source(
                source_record,
                self.instance_name,
                checkpoint_record,
                checkpoint.identifier,
            )
            self._require_matching_recovery_incarnation(checkpoint_record, self.instance_name, source_record)

        if source_record is not None and self._is_installed_instance(
            source_record,
            checkpoint=checkpoint.identifier,
            operation_id=operation_id,
        ):
            if not emergency_exists:
                raise StateError(
                    f"Lima restored instance for VM '{self.vm_name}' has no emergency recovery instance",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                    hint="Do not start or rename the recovery instances. Inspect them with 'limactl list'.",
                )
            if stage_exists:
                raise StateError(
                    f"Lima restore for VM '{self.vm_name}' has both installed and staged recovery instances",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                    hint="Do not start or rename the recovery instances. Inspect them with 'limactl list'.",
                )
            self._mark_restore_complete(checkpoint.identifier, operation_id)
            if discard_exists:
                self.host.delete_recovery_instance(names.discard)
            self._prove_completed_restore(names, operation_id=operation_id)
            return

        if source_record is not None and discard_exists:
            raise StateError(
                f"Lima restore artifacts for VM '{self.vm_name}' conflict with the current instance",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start or rename the recovery instances. Inspect them with 'limactl list'.",
            )

        if not stage_exists:
            if source_record is None:
                raise StateError(
                    f"Lima restore for VM '{self.vm_name}' is missing both its instance and restore stage",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                    hint="Do not start or rename the recovery instances. Inspect them with 'limactl list'.",
                )
            output.info(f"Preparing stopped Lima restore instance '{names.stage}' from the recovery clone...")
            expression = (
                f"del(.param.{_CHECKPOINT_NAME_PARAM}) | "
                f"del(.param.{_CHECKPOINT_SOURCE_PARAM}) | "
                f"del(.param.{_LAST_RESTORE_PARAM}) | "
                f'.param.{_RECOVERY_ROLE_PARAM} = "stage" | '
                f".param.{_RECOVERY_CHECKPOINT_PARAM} = {json.dumps(checkpoint.identifier)} | "
                f".param.{_RECOVERY_SOURCE_PARAM} = {json.dumps(self.instance_name)} | "
                f".param.{_RECOVERY_OPERATION_PARAM} = {json.dumps(operation_id)}"
            )
            self._clone_for_restore(checkpoint.identifier, names.stage, expression)
            self._validate_recovery_instances(
                checkpoint,
                checkpoint_record,
                (names.stage,),
                operation_id=operation_id,
            )

        if source_record is not None:
            if emergency_exists:
                output.info(f"Retaining current Lima instance as '{names.discard}' during restore...")
                self._mark_recovery_role(
                    self.instance_name,
                    role="discard",
                    checkpoint=checkpoint.identifier,
                    operation_id=operation_id,
                )
                self.host.move_atomically(self.instance_name, names.discard)
                discard_exists = True
            else:
                output.info(f"Retaining pre-restore Lima instance as emergency recovery '{names.emergency}'...")
                self._mark_recovery_role(
                    self.instance_name,
                    role="emergency",
                    checkpoint=checkpoint.identifier,
                    operation_id=operation_id,
                )
                self.host.ensure_protected(self.instance_name)
                self.host.move_atomically(self.instance_name, names.emergency)
                emergency_exists = True

        if not emergency_exists:
            raise StateError(
                f"Lima restore artifacts for VM '{self.vm_name}' are inconsistent",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start or rename the recovery instances. Inspect them with 'limactl list'.",
            )
        self.host.ensure_protected(names.emergency)

        output.info(f"Installing the recovered Lima instance as '{self.instance_name}'...")
        restored_record = self.host.move_atomically(names.stage, self.instance_name)
        self._validate_installed_instance(
            restored_record,
            checkpoint=checkpoint.identifier,
            operation_id=operation_id,
        )
        self._mark_restore_complete(checkpoint.identifier, operation_id)
        if discard_exists:
            self.host.delete_recovery_instance(names.discard)
        self._prove_completed_restore(names, operation_id=operation_id)

    def _finish_completed_restore(
        self,
        checkpoint: CheckpointDescriptor,
        checkpoint_record: dict[str, Any],
        names: _RecoveryNames,
        artifacts: set[str],
        source_record: dict[str, Any] | None,
        *,
        operation_id: str,
    ) -> None:
        if self.instance_name not in artifacts or names.emergency not in artifacts:
            raise StateError(
                f"Lima restore for VM '{self.vm_name}' is marked complete but a required instance is missing",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        self._validate_recovery_instances(
            checkpoint,
            checkpoint_record,
            tuple(artifact for artifact in (names.stage, names.discard, names.emergency) if artifact in artifacts),
            operation_id=operation_id,
        )
        assert source_record is not None
        self.host.require_stopped_vz_source(
            source_record,
            self.instance_name,
            checkpoint_record,
            checkpoint.identifier,
        )
        self._require_matching_recovery_incarnation(checkpoint_record, self.instance_name, source_record)
        self._validate_installed_instance(
            source_record,
            checkpoint=names.checkpoint,
            operation_id=operation_id,
        )
        if names.discard in artifacts:
            self.host.delete_recovery_instance(names.discard)
        self.host.ensure_protected(names.emergency)
        self._prove_completed_restore(names, operation_id=operation_id)

    def _prove_completed_restore(self, names: _RecoveryNames, *, operation_id: str) -> None:
        artifacts = set(self.host.names(recovery_target=names.stage))
        required = {self.instance_name, names.checkpoint, names.emergency}
        missing = sorted(required - artifacts)
        leftovers = sorted({names.stage, names.discard} & artifacts)
        if missing or leftovers:
            raise StateError(
                f"Lima restore reconciliation failed for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    f"Missing artifacts: {', '.join(missing) or 'none'}. "
                    f"Unexpected temporary artifacts: {', '.join(leftovers) or 'none'}."
                ),
            )
        checkpoint_record = self.host.record(names.checkpoint)
        assert checkpoint_record is not None
        source_record = self.host.record(self.instance_name)
        assert source_record is not None
        emergency_record = self.host.record(names.emergency)
        assert emergency_record is not None
        self.host.require_stopped(self.instance_name, purpose="checkpoint restore", record=source_record)
        self.host.require_stopped(names.emergency, purpose="emergency recovery retention", record=emergency_record)
        self._require_matching_recovery_incarnation(checkpoint_record, self.instance_name, source_record)
        self._require_matching_recovery_incarnation(checkpoint_record, names.emergency, emergency_record)
        self._validate_installed_instance(
            source_record,
            checkpoint=names.checkpoint,
            operation_id=operation_id,
        )
        if emergency_record.get("protected") is not True:
            raise StateError(
                f"Lima emergency recovery instance '{names.emergency}' is not protected",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _finish_clone_checkpoint(self, descriptor: CheckpointDescriptor) -> None:
        record = self.host.record(descriptor.identifier, required=False)
        if record is None:
            raise StateError(
                f"Lima checkpoint clone '{descriptor.identifier}' is incomplete",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    f"Confirm that no limactl clone is still running, remove the incomplete instance "
                    f"'{descriptor.identifier}', then retry."
                ),
            )
        source_record = self.host.record(self.instance_name, required=False)
        self._validate_clone_checkpoint_record(descriptor, record, source_record)
        if record.get("protected") is not True:
            output.info(f"Protecting Lima recovery clone '{descriptor.identifier}' from deletion...")
            self.host.protect(descriptor.identifier)
            record = self.host.record(descriptor.identifier)
            assert record is not None
        if record.get("protected") is not True:
            raise StateError(
                f"Lima recovery clone '{descriptor.identifier}' is not protected",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _require_clone_checkpoint(
        self,
        descriptor: CheckpointDescriptor,
        source_record: dict[str, Any] | None,
    ) -> dict[str, Any]:
        record = self.host.record(descriptor.identifier)
        assert record is not None
        self._validate_clone_checkpoint_record(descriptor, record, source_record)
        if record.get("protected") is not True:
            raise StateError(
                f"Lima recovery clone '{descriptor.identifier}' is not protected",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        return record

    def _validate_clone_checkpoint_record(
        self,
        descriptor: CheckpointDescriptor,
        record: dict[str, Any],
        source_record: dict[str, Any] | None,
    ) -> None:
        self.host.require_stopped(descriptor.identifier, purpose="checkpoint recovery", record=record)
        backend = self.host.backend(record, descriptor.identifier)
        if backend != "vz":
            raise StateError(
                f"Lima recovery clone '{descriptor.identifier}' uses unexpected driver '{backend}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        self.host.require_no_additional_disks(descriptor.identifier, record=record)
        params = record.get("param")
        if not isinstance(params, dict) or (
            params.get(_CHECKPOINT_NAME_PARAM) != descriptor.name
            or params.get(_CHECKPOINT_SOURCE_PARAM) != self.instance_name
            or not isinstance(params.get(_SOURCE_INCARNATION_PARAM), str)
            or not params[_SOURCE_INCARNATION_PARAM]
        ):
            raise StateError(
                f"Lima recovery clone '{descriptor.identifier}' is not bound to VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not restore or delete the artifact until its ownership is repaired.",
            )
        if source_record is not None:
            self.host.require_same_parent(
                record,
                descriptor.identifier,
                source_record,
                self.instance_name,
            )
            source_params = source_record.get("param")
            if not isinstance(source_params, dict) or (
                source_params.get(_SOURCE_INCARNATION_PARAM) != params[_SOURCE_INCARNATION_PARAM]
            ):
                raise StateError(
                    f"Lima recovery clone '{descriptor.identifier}' belongs to another VM incarnation",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                    hint="Do not restore or delete the artifact until its ownership is repaired.",
                )

    def _require_matching_recovery_incarnation(
        self,
        checkpoint_record: dict[str, Any],
        instance_name: str,
        record: dict[str, Any],
    ) -> None:
        checkpoint_params = checkpoint_record.get("param")
        checkpoint_incarnation = (
            checkpoint_params.get(_SOURCE_INCARNATION_PARAM) if isinstance(checkpoint_params, dict) else None
        )
        if not isinstance(checkpoint_incarnation, str) or not checkpoint_incarnation:
            raise StateError(
                f"Lima checkpoint for VM '{self.vm_name}' has no VM incarnation marker",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        params = record.get("param")
        if not isinstance(params, dict) or params.get(_SOURCE_INCARNATION_PARAM) != checkpoint_incarnation:
            raise StateError(
                f"Lima recovery instance '{instance_name}' belongs to another VM incarnation",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start, restore, rename, or delete the recovery instances.",
            )

    def _validate_recovery_instances(
        self,
        checkpoint: CheckpointDescriptor,
        checkpoint_record: dict[str, Any],
        instance_names: tuple[str, ...],
        *,
        operation_id: str,
    ) -> None:
        for instance_name in instance_names:
            record = self.host.record(instance_name)
            assert record is not None
            self._require_matching_recovery_incarnation(checkpoint_record, instance_name, record)
            checkpoint_name = str(checkpoint_record.get("name", ""))
            self.host.require_same_parent(
                checkpoint_record,
                checkpoint_name,
                record,
                instance_name,
            )
            if self.host.backend(record, instance_name) != "vz":
                raise StateError(
                    f"Lima recovery instance '{instance_name}' does not use the VZ driver",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                )
            self.host.require_stopped(instance_name, purpose="checkpoint restore", record=record)
            self.host.require_no_additional_disks(instance_name, record=record)
            match = _RECOVERY_ARTIFACT_NAME.fullmatch(instance_name)
            assert match is not None
            expected_role = {
                "em": "emergency",
                "rs": "stage",
                "rd": "discard",
            }.get(match.group("role"))
            params = record.get("param")
            recorded_operation = params.get(_RECOVERY_OPERATION_PARAM) if isinstance(params, dict) else None
            if not isinstance(params, dict) or (
                params.get(_RECOVERY_ROLE_PARAM) != expected_role
                or params.get(_RECOVERY_CHECKPOINT_PARAM) != checkpoint.identifier
                or params.get(_RECOVERY_SOURCE_PARAM) != self.instance_name
                or not isinstance(recorded_operation, str)
                or not recorded_operation
                or (expected_role != "emergency" and recorded_operation != operation_id)
            ):
                raise StateError(
                    f"Lima recovery instance '{instance_name}' has conflicting ownership metadata",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                    hint="Do not start, restore, rename, or delete the recovery instances.",
                )

    def _validate_installed_instance(
        self,
        record: dict[str, Any],
        *,
        checkpoint: str,
        operation_id: str,
    ) -> None:
        if self._is_installed_instance(record, checkpoint=checkpoint, operation_id=operation_id):
            return
        raise StateError(
            f"Lima restored instance for VM '{self.vm_name}' has conflicting ownership metadata",
            entity_kind="vm",
            entity_name=self.vm_name,
        )

    def _is_installed_instance(
        self,
        record: dict[str, Any],
        *,
        checkpoint: str,
        operation_id: str,
    ) -> bool:
        params = record.get("param")
        return isinstance(params, dict) and (
            params.get(_RECOVERY_ROLE_PARAM) == "stage"
            and params.get(_RECOVERY_CHECKPOINT_PARAM) == checkpoint
            and params.get(_RECOVERY_SOURCE_PARAM) == self.instance_name
            and params.get(_RECOVERY_OPERATION_PARAM) == operation_id
        )

    def _clone_for_restore(self, source: str, target: str, expression: str) -> None:
        try:
            self.run(f"limactl clone --tty=false --set {shlex.quote(expression)} {source} {target}")
        except KeyboardInterrupt:
            output.warn(f"Lima restore clone '{target}' may still exist. Rerun the restore command to reconcile it.")
            raise
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not prepare restore instance '{target}' for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            ) from error

    def _mark_restore_complete(self, checkpoint_artifact: str, operation_id: str) -> None:
        output.detail("Recording completed Lima restore operation on the recovery clone...")
        self._set_instance_param(checkpoint_artifact, _LAST_RESTORE_PARAM, operation_id)
        record = self.host.record(checkpoint_artifact)
        assert record is not None
        if self._last_restore(record) != operation_id:
            raise StateError(
                f"Lima did not retain the restore marker for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _ensure_source_incarnation(self, operation_id: str, record: dict[str, Any]) -> None:
        params = record.get("param")
        if isinstance(params, dict):
            existing = params.get(_SOURCE_INCARNATION_PARAM)
            if isinstance(existing, str) and existing:
                return
        output.detail("Recording the Lima VM incarnation for checkpoint ownership...")
        self._set_instance_param(self.instance_name, _SOURCE_INCARNATION_PARAM, operation_id)
        refreshed = self.host.record(self.instance_name)
        assert refreshed is not None
        params = refreshed.get("param")
        if not isinstance(params, dict) or params.get(_SOURCE_INCARNATION_PARAM) != operation_id:
            raise StateError(
                f"Lima did not retain the VM incarnation marker for '{self.instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _set_instance_param(self, instance_name: str, key: str, value: str) -> None:
        self._set_instance_params(instance_name, {key: value})

    def _set_instance_params(self, instance_name: str, values: dict[str, str]) -> None:
        expression = " | ".join(f".param.{key} = {json.dumps(value)}" for key, value in values.items())
        try:
            self.run(f"limactl edit --tty=false --set {shlex.quote(expression)} {instance_name}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not record managed checkpoint state for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Correct the Lima instance metadata failure, then retry.",
            ) from error

    def _mark_recovery_role(
        self,
        instance_name: str,
        *,
        role: str,
        checkpoint: str,
        operation_id: str,
    ) -> None:
        self._set_instance_params(
            instance_name,
            {
                _RECOVERY_ROLE_PARAM: role,
                _RECOVERY_CHECKPOINT_PARAM: checkpoint,
                _RECOVERY_SOURCE_PARAM: self.instance_name,
                _RECOVERY_OPERATION_PARAM: operation_id,
            },
        )
        record = self.host.record(instance_name)
        assert record is not None
        params = record.get("param")
        if not isinstance(params, dict) or (
            params.get(_RECOVERY_ROLE_PARAM) != role
            or params.get(_RECOVERY_CHECKPOINT_PARAM) != checkpoint
            or params.get(_RECOVERY_SOURCE_PARAM) != self.instance_name
            or params.get(_RECOVERY_OPERATION_PARAM) != operation_id
        ):
            raise StateError(
                f"Lima did not retain recovery ownership metadata for '{instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    @staticmethod
    def _last_restore(record: dict[str, Any]) -> str | None:
        params = record.get("param")
        if not isinstance(params, dict):
            return None
        value = params.get(_LAST_RESTORE_PARAM)
        return value if isinstance(value, str) and value else None

    def _raise_unsupported_backend(self, backend: str) -> None:
        raise StateError(
            f"Lima VM '{self.vm_name}' uses unsupported checkpoint driver '{backend}'",
            entity_kind="vm",
            entity_name=self.vm_name,
            hint="Agentworks supports Lima QEMU snapshots and stopped VZ recovery clones.",
        )

    def _snapshot_names(self) -> tuple[str, ...]:
        try:
            listing = self.run(f"limactl snapshot list {shlex.quote(self.instance_name)} --quiet")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima checkpoint inventory is unavailable for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    "Lima snapshots are experimental and require a snapshot-capable QEMU driver. "
                    "Update Lima before retrying."
                ),
            ) from error
        return tuple(
            name
            for raw_name in listing.splitlines()
            if (name := raw_name.strip()) and _MANAGED_CHECKPOINT_NAME.fullmatch(name) is not None
        )

    def _recovery_artifact_names(self, *, recovery_target: str | None = None) -> tuple[str, ...]:
        return tuple(
            name
            for name in self.host.names(recovery_target=recovery_target)
            if (match := _RECOVERY_ARTIFACT_NAME.fullmatch(name)) is not None
            and match.group("source") == self.source_identity
        )

    def _clone_descriptors(
        self,
        artifacts: tuple[str, ...] | None = None,
    ) -> tuple[CheckpointDescriptor, ...]:
        if artifacts is None:
            artifacts = self._recovery_artifact_names()
        descriptors: list[CheckpointDescriptor] = []
        for artifact in artifacts:
            match = _RECOVERY_ARTIFACT_NAME.fullmatch(artifact)
            assert match is not None
            if match.group("role") != "cp" or match.group("operation") is not None:
                continue
            name = f"agw-{match.group('checkpoint')}"
            descriptors.append(CheckpointDescriptor(name=name, identifier=artifact))
        return tuple(sorted(descriptors, key=lambda item: item.name))

    def _clone_descriptor(self, name: str) -> CheckpointDescriptor:
        match = _CLONE_CHECKPOINT_NAME.fullmatch(name)
        if match is None:
            raise StateError(
                f"Lima VZ checkpoint name {name!r} is not a core-generated checkpoint name",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        identifier = f"agwcp-{self.source_identity}-{match.group('identity')}"
        return CheckpointDescriptor(name=name, identifier=identifier)

    def _restore_names(self, checkpoint: CheckpointDescriptor) -> _RecoveryNames:
        stem = checkpoint.identifier.removeprefix("agwcp-")
        return _RecoveryNames(
            checkpoint=checkpoint.identifier,
            emergency=f"agwem-{stem}",
            stage=f"agwrs-{stem}",
            discard=f"agwrd-{stem}",
        )

    def _related_recovery_artifacts(
        self,
        checkpoint: CheckpointDescriptor,
        artifacts: tuple[str, ...],
    ) -> tuple[str, ...]:
        match = _RECOVERY_ARTIFACT_NAME.fullmatch(checkpoint.identifier)
        assert match is not None
        checkpoint_identity = match.group("checkpoint")
        return tuple(
            artifact
            for artifact in artifacts
            if (candidate := _RECOVERY_ARTIFACT_NAME.fullmatch(artifact)) is not None
            and candidate.group("source") == self.source_identity
            and candidate.group("checkpoint") == checkpoint_identity
        )

    def _validate_owned_recovery_artifact(
        self,
        checkpoint: CheckpointDescriptor,
        artifact: str,
    ) -> None:
        match = _RECOVERY_ARTIFACT_NAME.fullmatch(artifact)
        assert match is not None
        expected_role = {
            "em": "emergency",
            "rs": "stage",
            "rd": "discard",
        }.get(match.group("role"))
        if expected_role is None:
            raise StateError(
                f"Lima recovery artifact '{artifact}' has an unexpected role",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        record = self.host.record(artifact)
        assert record is not None
        params = record.get("param")
        incarnation = params.get(_SOURCE_INCARNATION_PARAM) if isinstance(params, dict) else None
        operation = params.get(_RECOVERY_OPERATION_PARAM) if isinstance(params, dict) else None
        if not isinstance(params, dict) or (
            params.get(_RECOVERY_ROLE_PARAM) != expected_role
            or params.get(_RECOVERY_CHECKPOINT_PARAM) != checkpoint.identifier
            or params.get(_RECOVERY_SOURCE_PARAM) != self.instance_name
            or not isinstance(incarnation, str)
            or not incarnation
            or not isinstance(operation, str)
            or not operation
        ):
            raise StateError(
                f"Lima recovery artifact '{artifact}' has conflicting ownership metadata",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not delete the artifact until its ownership is repaired.",
            )
        if self.host.backend(record, artifact) != "vz":
            raise StateError(
                f"Lima recovery artifact '{artifact}' does not use the VZ driver",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        self.host.require_stopped(artifact, purpose="checkpoint deletion", record=record)
        self.host.require_no_additional_disks(artifact, record=record)

        source_record = self.host.record(self.instance_name, required=False)
        checkpoint_record = self.host.record(checkpoint.identifier, required=False)
        anchor = source_record or checkpoint_record
        if anchor is not None:
            anchor_name = self.instance_name if source_record is not None else checkpoint.identifier
            self.host.require_same_parent(anchor, anchor_name, record, artifact)
            anchor_params = anchor.get("param")
            if not isinstance(anchor_params, dict) or anchor_params.get(_SOURCE_INCARNATION_PARAM) != incarnation:
                raise StateError(
                    f"Lima recovery artifact '{artifact}' belongs to another VM incarnation",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                )

    def _require_checkpoint_name(self, name: str) -> str:
        if _MANAGED_CHECKPOINT_NAME.fullmatch(name) is None:
            raise StateError(
                f"Lima checkpoint name {name!r} is not an Agentworks-managed name",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        return name

    def _require_one_snapshot(self, name: str, names: tuple[str, ...]) -> None:
        count = names.count(name)
        if count == 1:
            return
        problem = "missing" if count == 0 else "duplicated"
        raise StateError(
            f"Lima checkpoint '{name}' is {problem} for VM '{self.vm_name}'",
            entity_kind="vm",
            entity_name=self.vm_name,
        )

    def _require_one_descriptor(
        self,
        name: str,
        descriptors: tuple[CheckpointDescriptor, ...],
    ) -> None:
        count = sum(item.name == name for item in descriptors)
        if count == 1:
            return
        problem = "missing" if count == 0 else "duplicated"
        raise StateError(
            f"Lima checkpoint '{name}' is {problem} for VM '{self.vm_name}'",
            entity_kind="vm",
            entity_name=self.vm_name,
        )
