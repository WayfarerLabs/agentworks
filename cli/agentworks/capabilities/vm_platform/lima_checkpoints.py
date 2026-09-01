"""Driver-aware managed checkpoints for Lima instances."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentworks import output
from agentworks.capabilities.vm_platform.base import CheckpointDescriptor
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

LimaRunner = Callable[..., str]


@dataclass(frozen=True, slots=True)
class _RecoveryNames:
    checkpoint: str
    emergency: str
    stage: str
    discard: str


class LimaCheckpointOperations:
    """Manage one Lima instance's checkpoints through ``limactl``.

    QEMU has Lima snapshot support. VZ does not, so a stopped clone is the
    durable recovery point and restore swaps stopped instance names. Every
    recovery artifact has a deterministic name scoped to the source instance.
    """

    def __init__(self, *, vm_name: str, instance_name: str, run: LimaRunner) -> None:
        self.vm_name = vm_name
        self.instance_name = instance_name
        self.run = run
        self.source_identity = hashlib.sha256(instance_name.encode()).hexdigest()[:8]

    def create(
        self,
        name: str,
        *,
        operation_id: str,
        resume: bool,
    ) -> CheckpointDescriptor:
        name = self._require_checkpoint_name(name)
        clone_inventory = self._clone_descriptors()
        matches = [item for item in clone_inventory if item.name == name]
        if matches:
            self._require_one_descriptor(name, clone_inventory)
            descriptor = matches[0]
            self._finish_clone_checkpoint(descriptor, resume=resume)
            return descriptor

        backend = self._source_backend()
        self._require_instance_stopped(self.instance_name, purpose="checkpoint creation")
        if backend == "qemu":
            return self._create_snapshot(name)
        if backend != "vz":
            self._raise_unsupported_backend(backend)

        clone_descriptor = self._clone_descriptor(name)
        self._require_no_additional_disks(self.instance_name)
        self._ensure_source_incarnation(operation_id)
        output.info(f"Creating stopped Lima VZ recovery clone '{clone_descriptor.identifier}'...")
        output.detail(
            "Lima will use copy-on-write storage when the host filesystem supports it. "
            "The clone can grow as the VM changes."
        )
        expression = (
            f".param.{_CHECKPOINT_NAME_PARAM} = {json.dumps(name)} | "
            f".param.{_CHECKPOINT_SOURCE_PARAM} = {json.dumps(self.instance_name)} | "
            f'.param.{_LAST_RESTORE_PARAM} = ""'
        )
        try:
            self.run(
                "limactl clone --tty=false --set "
                f"{shlex.quote(expression)} {shlex.quote(self.instance_name)} "
                f"{clone_descriptor.identifier}"
            )
        except KeyboardInterrupt:
            self._cleanup_interrupted_clone(clone_descriptor.identifier)
            raise
        except (OSError, SSHError) as error:
            self._cleanup_interrupted_clone(clone_descriptor.identifier)
            raise StateError(
                f"Lima could not create checkpoint '{name}' for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    "The VM remains stopped and no Debian package mutation has begun. "
                    "Correct the Lima clone failure, then retry."
                ),
            ) from error
        self._finish_clone_checkpoint(clone_descriptor, resume=False)
        output.warn(
            f"Lima recovery clone '{clone_descriptor.identifier}' is stopped and protected. "
            "Do not start it: it contains the same guest and Tailscale identity as the VM."
        )
        return clone_descriptor

    def list(self) -> tuple[CheckpointDescriptor, ...]:
        recovery_artifacts = self._recovery_artifact_names()
        descriptors = self._clone_descriptors(recovery_artifacts)
        if recovery_artifacts:
            for descriptor in descriptors:
                record = self._instance_record(descriptor.identifier, required=False)
                if record is not None:
                    self._validate_clone_checkpoint_record(descriptor, record)
            return descriptors
        record = self._instance_record(self.instance_name, required=False)
        if record is None:
            return ()
        backend = self._record_backend(record, self.instance_name)
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

        all_names = self._instance_names()
        related = self._related_recovery_artifacts(clone_descriptor, all_names)
        checkpoint_artifact = clone_descriptor.identifier
        ordered = sorted(item for item in related if item != checkpoint_artifact)
        if checkpoint_artifact in related:
            ordered.append(checkpoint_artifact)
        for artifact in ordered:
            output.info(f"Deleting Lima recovery artifact '{artifact}'...")
            self._delete_recovery_instance(artifact)
        if self._related_recovery_artifacts(clone_descriptor, self._instance_names()):
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
        self._require_instance_stopped(self.instance_name, purpose="checkpoint restore")
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
        self._require_instance_stopped(self.instance_name, purpose="checkpoint restore")

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
        names = self._restore_names(checkpoint, operation_id)
        checkpoint_record = self._require_clone_checkpoint(checkpoint)
        if self._last_restore(checkpoint_record) == operation_id:
            self._finish_completed_restore(names)
            return

        artifacts = set(self._instance_names())
        source_exists = self.instance_name in artifacts
        stage_exists = names.stage in artifacts
        discard_exists = names.discard in artifacts
        emergency_exists = names.emergency in artifacts
        self._require_matching_recovery_incarnations(
            checkpoint_record,
            tuple(artifact for artifact in (names.stage, names.discard, names.emergency) if artifact in artifacts),
        )

        if source_exists and discard_exists:
            # The swap completed before its durable marker was written.
            if stage_exists:
                self._delete_recovery_instance(names.stage)
            self._mark_restore_complete(checkpoint.identifier, operation_id)
            self._delete_recovery_instance(names.discard)
            self._prove_completed_restore(names)
            return

        if not stage_exists:
            output.info(f"Preparing stopped Lima restore instance '{names.stage}' from the recovery clone...")
            expression = (
                f"del(.param.{_CHECKPOINT_NAME_PARAM}) | "
                f"del(.param.{_CHECKPOINT_SOURCE_PARAM}) | "
                f"del(.param.{_LAST_RESTORE_PARAM})"
            )
            self._clone_for_restore(checkpoint.identifier, names.stage, expression)
            stage_exists = True

        if source_exists:
            if emergency_exists:
                output.info(f"Retaining current Lima instance as '{names.discard}' during restore...")
                self._rename_instance(self.instance_name, names.discard)
                discard_exists = True
            else:
                output.info(f"Retaining pre-restore Lima instance as emergency recovery '{names.emergency}'...")
                self._rename_instance(self.instance_name, names.emergency)
                self._protect_instance(names.emergency)
                emergency_exists = True
            source_exists = False

        if emergency_exists:
            self._ensure_instance_protected(names.emergency)
        if source_exists or not stage_exists or not emergency_exists:
            raise StateError(
                f"Lima restore artifacts for VM '{self.vm_name}' are inconsistent",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start or rename the recovery instances. Inspect them with 'limactl list'.",
            )

        output.info(f"Installing the recovered Lima instance as '{self.instance_name}'...")
        self._rename_instance(names.stage, self.instance_name)
        self._require_instance_stopped(self.instance_name, purpose="checkpoint restore")
        self._mark_restore_complete(checkpoint.identifier, operation_id)
        if discard_exists:
            self._delete_recovery_instance(names.discard)
        self._prove_completed_restore(names)

    def _finish_completed_restore(self, names: _RecoveryNames) -> None:
        artifacts = set(self._instance_names())
        if self.instance_name not in artifacts:
            if names.stage in artifacts:
                output.info(f"Finishing interrupted Lima restore for VM '{self.vm_name}'...")
                self._rename_instance(names.stage, self.instance_name)
            else:
                raise StateError(
                    f"Lima restore for VM '{self.vm_name}' is marked complete but its instance is missing",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                )
        if names.stage in self._instance_names():
            self._delete_recovery_instance(names.stage)
        if names.discard in self._instance_names():
            self._delete_recovery_instance(names.discard)
        self._ensure_instance_protected(names.emergency)
        self._prove_completed_restore(names)

    def _prove_completed_restore(self, names: _RecoveryNames) -> None:
        artifacts = set(self._instance_names())
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
        self._require_instance_stopped(self.instance_name, purpose="checkpoint restore")
        self._require_instance_stopped(names.emergency, purpose="emergency recovery retention")
        checkpoint_record = self._instance_record(names.checkpoint)
        assert checkpoint_record is not None
        self._require_matching_recovery_incarnations(
            checkpoint_record,
            (self.instance_name, names.emergency),
        )
        emergency_record = self._instance_record(names.emergency)
        assert emergency_record is not None
        if emergency_record.get("protected") is not True:
            raise StateError(
                f"Lima emergency recovery instance '{names.emergency}' is not protected",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _finish_clone_checkpoint(
        self,
        descriptor: CheckpointDescriptor,
        *,
        resume: bool,
    ) -> None:
        record = self._instance_record(descriptor.identifier, required=False)
        if record is None:
            qualifier = "interrupted " if resume else ""
            raise StateError(
                f"Lima {qualifier}checkpoint clone '{descriptor.identifier}' is incomplete",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    f"Confirm that no limactl clone is still running, remove the incomplete instance "
                    f"'{descriptor.identifier}', then retry."
                ),
            )
        self._validate_clone_checkpoint_record(descriptor, record)
        if record.get("protected") is not True:
            output.info(f"Protecting Lima recovery clone '{descriptor.identifier}' from deletion...")
            self._protect_instance(descriptor.identifier)
            record = self._instance_record(descriptor.identifier)
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
    ) -> dict[str, Any]:
        record = self._instance_record(descriptor.identifier)
        assert record is not None
        self._validate_clone_checkpoint_record(descriptor, record)
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
    ) -> None:
        self._require_instance_stopped(descriptor.identifier, purpose="checkpoint recovery")
        backend = self._record_backend(record, descriptor.identifier)
        if backend != "vz":
            raise StateError(
                f"Lima recovery clone '{descriptor.identifier}' uses unexpected driver '{backend}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        self._require_no_additional_disks(descriptor.identifier, record=record)
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
        source_record = self._instance_record(self.instance_name, required=False)
        if source_record is not None:
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

    def _require_matching_recovery_incarnations(
        self,
        checkpoint_record: dict[str, Any],
        instance_names: tuple[str, ...],
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
        for instance_name in instance_names:
            record = self._instance_record(instance_name)
            assert record is not None
            params = record.get("param")
            if not isinstance(params, dict) or params.get(_SOURCE_INCARNATION_PARAM) != checkpoint_incarnation:
                raise StateError(
                    f"Lima recovery instance '{instance_name}' belongs to another VM incarnation",
                    entity_kind="vm",
                    entity_name=self.vm_name,
                    hint="Do not start, restore, rename, or delete the recovery instances.",
                )

    def _clone_for_restore(self, source: str, target: str, expression: str) -> None:
        try:
            self.run(f"limactl clone --tty=false --set {shlex.quote(expression)} {source} {target}")
        except KeyboardInterrupt:
            self._cleanup_interrupted_clone(target)
            raise
        except (OSError, SSHError) as error:
            self._cleanup_interrupted_clone(target)
            raise StateError(
                f"Lima could not prepare restore instance '{target}' for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            ) from error
        self._require_instance_stopped(target, purpose="checkpoint restore")
        self._require_no_additional_disks(target)

    def _mark_restore_complete(self, checkpoint_artifact: str, operation_id: str) -> None:
        output.detail("Recording completed Lima restore operation on the recovery clone...")
        self._set_instance_param(checkpoint_artifact, _LAST_RESTORE_PARAM, operation_id)
        record = self._instance_record(checkpoint_artifact)
        assert record is not None
        if self._last_restore(record) != operation_id:
            raise StateError(
                f"Lima did not retain the restore marker for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _ensure_source_incarnation(self, operation_id: str) -> str:
        record = self._instance_record(self.instance_name)
        assert record is not None
        params = record.get("param")
        if isinstance(params, dict):
            existing = params.get(_SOURCE_INCARNATION_PARAM)
            if isinstance(existing, str) and existing:
                return existing
        output.detail("Recording the Lima VM incarnation for checkpoint ownership...")
        self._set_instance_param(self.instance_name, _SOURCE_INCARNATION_PARAM, operation_id)
        record = self._instance_record(self.instance_name)
        assert record is not None
        params = record.get("param")
        if not isinstance(params, dict) or params.get(_SOURCE_INCARNATION_PARAM) != operation_id:
            raise StateError(
                f"Lima did not retain the VM incarnation marker for '{self.instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        return operation_id

    def _set_instance_param(self, instance_name: str, key: str, value: str) -> None:
        expression = f".param.{key} = {json.dumps(value)}"
        try:
            self.run(f"limactl edit --tty=false --set {shlex.quote(expression)} {instance_name}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not record managed checkpoint state for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Correct the Lima instance metadata failure, then retry.",
            ) from error

    @staticmethod
    def _last_restore(record: dict[str, Any]) -> str | None:
        params = record.get("param")
        if not isinstance(params, dict):
            return None
        value = params.get(_LAST_RESTORE_PARAM)
        return value if isinstance(value, str) and value else None

    def _source_backend(self) -> str:
        record = self._instance_record(self.instance_name)
        assert record is not None
        return self._record_backend(record, self.instance_name)

    def _record_backend(self, record: dict[str, Any], instance_name: str) -> str:
        backend = record.get("vmType")
        if not isinstance(backend, str) or not backend:
            raise StateError(
                f"Lima did not report a VM driver for instance '{instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        return backend.lower()

    def _raise_unsupported_backend(self, backend: str) -> None:
        raise StateError(
            f"Lima VM '{self.vm_name}' uses unsupported checkpoint driver '{backend}'",
            entity_kind="vm",
            entity_name=self.vm_name,
            hint="Agentworks supports Lima QEMU snapshots and stopped VZ recovery clones.",
        )

    def _require_instance_stopped(self, instance_name: str, *, purpose: str) -> None:
        record = self._instance_record(instance_name)
        assert record is not None
        status = record.get("status")
        if isinstance(status, str) and status.lower() == "stopped":
            return
        raise StateError(
            f"Lima instance '{instance_name}' must be stopped for {purpose}",
            entity_kind="vm",
            entity_name=self.vm_name,
        )

    def _require_no_additional_disks(
        self,
        instance_name: str,
        *,
        record: dict[str, Any] | None = None,
    ) -> None:
        if record is None:
            record = self._instance_record(instance_name)
            assert record is not None
        additional_disks = record.get("additionalDisks", [])
        if isinstance(additional_disks, list) and not additional_disks:
            return
        raise StateError(
            f"Lima instance '{instance_name}' has additional disks that a recovery clone would not own",
            entity_kind="vm",
            entity_name=self.vm_name,
            hint="Move the VM data onto its primary disk before creating a managed checkpoint.",
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

    def _instance_names(self) -> tuple[str, ...]:
        try:
            listing = self.run("limactl list --quiet")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima instance inventory is unavailable for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Correct the Lima inventory error, then retry the checkpoint operation.",
            ) from error
        return tuple(name for line in listing.splitlines() if (name := line.strip()))

    def _instance_record(
        self,
        instance_name: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        try:
            listing = self.run(
                f"limactl list --json {shlex.quote(instance_name)}",
                check=False,
            )
        except (OSError, SSHError) as error:
            if not required:
                return None
            raise StateError(
                f"Lima instance '{instance_name}' could not be inspected",
                entity_kind="vm",
                entity_name=self.vm_name,
            ) from error
        for line in listing.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("name") == instance_name:
                return record
        if not required:
            return None
        raise StateError(
            f"Lima instance '{instance_name}' does not exist or is incomplete",
            entity_kind="vm",
            entity_name=self.vm_name,
            hint="Inspect the Lima instance inventory before retrying the checkpoint operation.",
        )

    def _recovery_artifact_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in self._instance_names()
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

    def _restore_names(
        self,
        checkpoint: CheckpointDescriptor,
        operation_id: str,
    ) -> _RecoveryNames:
        operation_identity = hashlib.sha256(operation_id.encode()).hexdigest()[:8]
        stem = checkpoint.identifier.removeprefix("agwcp-")
        return _RecoveryNames(
            checkpoint=checkpoint.identifier,
            emergency=f"agwem-{stem}",
            stage=f"agwrs-{stem}-{operation_identity}",
            discard=f"agwrd-{stem}-{operation_identity}",
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

    def _protect_instance(self, instance_name: str) -> None:
        try:
            self.run(f"limactl protect {instance_name}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not protect recovery instance '{instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            ) from error

    def _ensure_instance_protected(self, instance_name: str) -> None:
        record = self._instance_record(instance_name)
        assert record is not None
        if record.get("protected") is True:
            return
        output.info(f"Protecting Lima recovery instance '{instance_name}' from deletion...")
        self._protect_instance(instance_name)
        record = self._instance_record(instance_name)
        assert record is not None
        if record.get("protected") is not True:
            raise StateError(
                f"Lima recovery instance '{instance_name}' is not protected",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _rename_instance(self, source: str, target: str) -> None:
        try:
            self.run(f"limactl rename --tty=false {source} {target}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not rename recovery instance '{source}' to '{target}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start or rename the recovery instances. Rerun the restore.",
            ) from error

    def _delete_recovery_instance(self, instance_name: str) -> None:
        if instance_name not in self._instance_names():
            return
        self.run(f"limactl unprotect {instance_name}", check=False)
        try:
            self.run(f"limactl delete --force {instance_name}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not delete recovery instance '{instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            ) from error
        if instance_name in self._instance_names():
            raise StateError(
                f"Lima recovery instance '{instance_name}' still exists after deletion",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def _cleanup_interrupted_clone(self, instance_name: str) -> None:
        output.detail(f"Cleaning up interrupted Lima recovery clone '{instance_name}'...")
        with contextlib.suppress(OSError, SSHError):
            self.run(f"limactl unprotect {instance_name}", check=False)
        with contextlib.suppress(OSError, SSHError):
            self.run(f"limactl delete --force {instance_name}", check=False)

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
