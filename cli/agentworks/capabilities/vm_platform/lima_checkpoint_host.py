"""Validated Lima host inventory and stopped-instance mutations."""

from __future__ import annotations

import json
import posixpath
import shlex
from collections.abc import Callable
from typing import Any

from agentworks import output
from agentworks.errors import StateError
from agentworks.ssh import SSHError

LimaRunner = Callable[..., str]
LimaDirectoryRename = Callable[[str, str], None]


class LimaCheckpointHost:
    """Treat Lima's reported host state as an external trust boundary."""

    def __init__(self, *, vm_name: str, run: LimaRunner, rename: LimaDirectoryRename | None) -> None:
        self.vm_name = vm_name
        self.run = run
        self.rename = rename

    @staticmethod
    def _is_recovery_instance(instance_name: str) -> bool:
        return instance_name.startswith(("agwcp-", "agwem-", "agwrs-", "agwrd-"))

    @staticmethod
    def _partial_recovery_hint(instance_name: str) -> str:
        return (
            f"Confirm that no limactl clone is still running for '{instance_name}', then inspect it with "
            f"'limactl list --json {instance_name}'. If Lima cannot read the instance, move that exact "
            "instance directory out of Lima's instance root before retrying."
        )

    def names(self, *, recovery_target: str | None = None) -> tuple[str, ...]:
        try:
            listing = self.run("limactl list --quiet")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima instance inventory is unavailable for VM '{self.vm_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    self._partial_recovery_hint(recovery_target)
                    if recovery_target is not None
                    else "Correct the Lima inventory error, then retry the checkpoint operation."
                ),
            ) from error
        return tuple(name for line in listing.splitlines() if (name := line.strip()))

    def record(
        self,
        instance_name: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        if not required and instance_name not in self.names():
            return None
        try:
            listing = self.run(f"limactl list --json {shlex.quote(instance_name)}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima instance '{instance_name}' could not be inspected",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint=(
                    self._partial_recovery_hint(instance_name)
                    if instance_name.startswith(("agwcp-", "agwem-", "agwrs-", "agwrd-"))
                    else None
                ),
            ) from error
        for line in listing.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("name") == instance_name:
                status = record.get("status")
                if self._is_recovery_instance(instance_name) and isinstance(status, str) and status.lower() == "broken":
                    raise StateError(
                        f"Lima recovery instance '{instance_name}' is incomplete",
                        entity_kind="vm",
                        entity_name=self.vm_name,
                        hint=self._partial_recovery_hint(instance_name),
                    )
                return record
        raise StateError(
            f"Lima instance '{instance_name}' does not exist or is incomplete",
            entity_kind="vm",
            entity_name=self.vm_name,
            hint=(
                self._partial_recovery_hint(instance_name)
                if self._is_recovery_instance(instance_name)
                else "Inspect the Lima instance inventory before retrying the checkpoint operation."
            ),
        )

    def backend(self, record: dict[str, Any], instance_name: str) -> str:
        backend = record.get("vmType")
        if not isinstance(backend, str) or not backend:
            raise StateError(
                f"Lima did not report a VM driver for instance '{instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        return backend.lower()

    def require_stopped(
        self,
        instance_name: str,
        *,
        purpose: str,
        record: dict[str, Any] | None = None,
    ) -> None:
        if record is None:
            record = self.record(instance_name)
            assert record is not None
        status = record.get("status")
        if isinstance(status, str) and status.lower() == "stopped":
            return
        raise StateError(
            f"Lima instance '{instance_name}' must be stopped for {purpose}",
            entity_kind="vm",
            entity_name=self.vm_name,
        )

    def require_no_additional_disks(
        self,
        instance_name: str,
        *,
        record: dict[str, Any] | None = None,
    ) -> None:
        if record is None:
            record = self.record(instance_name)
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

    def instance_dir(self, record: dict[str, Any], instance_name: str) -> str:
        value = record.get("dir")
        if not isinstance(value, str) or not value:
            raise StateError(
                f"Lima did not report an instance directory for '{instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        normalized = posixpath.normpath(value)
        parent = posixpath.dirname(value)
        if (
            value != normalized
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or not posixpath.isabs(value)
            or posixpath.basename(value) != instance_name
            or parent in {"", "/"}
        ):
            raise StateError(
                f"Lima reported an unsafe instance directory for '{instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Inspect the Lima instance directory before retrying the checkpoint operation.",
            )
        return value

    def require_atomic_rename(self) -> None:
        if self.rename is not None:
            return
        raise StateError(
            f"Lima VZ checkpoint operations for VM '{self.vm_name}' must run on the Lima placement host",
            entity_kind="vm",
            entity_name=self.vm_name,
            hint="Run Agentworks on that host with a local-placement Lima site, then retry.",
        )

    def require_same_parent(
        self,
        reference: dict[str, Any],
        reference_name: str,
        candidate: dict[str, Any],
        candidate_name: str,
    ) -> None:
        reference_dir = self.instance_dir(reference, reference_name)
        candidate_dir = self.instance_dir(candidate, candidate_name)
        if posixpath.dirname(reference_dir) != posixpath.dirname(candidate_dir):
            raise StateError(
                f"Lima instance '{candidate_name}' is outside the checkpoint's instance directory",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Inspect the Lima instance directories before retrying the checkpoint operation.",
            )

    def require_stopped_vz_source(
        self,
        record: dict[str, Any],
        instance_name: str,
        checkpoint_record: dict[str, Any],
        checkpoint_name: str,
    ) -> None:
        if self.backend(record, instance_name) != "vz":
            raise StateError(
                f"Lima VM '{self.vm_name}' no longer uses the VZ checkpoint driver",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        self.require_stopped(instance_name, purpose="checkpoint restore", record=record)
        self.require_no_additional_disks(instance_name, record=record)
        self.require_same_parent(checkpoint_record, checkpoint_name, record, instance_name)

    def protect(self, instance_name: str) -> None:
        try:
            self.run(f"limactl protect {instance_name}")
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not protect recovery instance '{instance_name}'",
                entity_kind="vm",
                entity_name=self.vm_name,
            ) from error

    def ensure_protected(self, instance_name: str) -> None:
        record = self.record(instance_name)
        assert record is not None
        if record.get("protected") is True:
            return
        output.info(f"Protecting Lima recovery instance '{instance_name}' from deletion...")
        self.protect(instance_name)
        record = self.record(instance_name)
        assert record is not None
        if record.get("protected") is not True:
            raise StateError(
                f"Lima recovery instance '{instance_name}' is not protected",
                entity_kind="vm",
                entity_name=self.vm_name,
            )

    def move_atomically(self, source: str, target: str) -> dict[str, Any]:
        self.require_atomic_rename()
        assert self.rename is not None
        source_record = self.record(source)
        assert source_record is not None
        self.require_stopped(source, purpose="checkpoint restore", record=source_record)
        if target in self.names():
            raise StateError(
                f"Lima recovery target '{target}' already exists",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start or rename the recovery instances. Rerun the restore.",
            )
        source_dir = self.instance_dir(source_record, source)
        target_dir = posixpath.join(posixpath.dirname(source_dir), target)
        try:
            self.rename(source_dir, target_dir)
        except (OSError, SSHError) as error:
            raise StateError(
                f"Lima could not atomically move recovery instance '{source}' to '{target}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start or rename the recovery instances. Rerun the restore.",
            ) from error
        artifacts = set(self.names())
        if source in artifacts or target not in artifacts:
            raise StateError(
                f"Lima did not complete the recovery move from '{source}' to '{target}'",
                entity_kind="vm",
                entity_name=self.vm_name,
                hint="Do not start or rename the recovery instances. Rerun the restore.",
            )
        target_record = self.record(target)
        assert target_record is not None
        if self.instance_dir(target_record, target) != target_dir:
            raise StateError(
                f"Lima recovery instance '{target}' moved to an unexpected directory",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
        self.require_stopped(target, purpose="checkpoint restore", record=target_record)
        return target_record

    def delete_recovery_instance(self, instance_name: str) -> None:
        if instance_name not in self.names():
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
        if instance_name in self.names():
            raise StateError(
                f"Lima recovery instance '{instance_name}' still exists after deletion",
                entity_kind="vm",
                entity_name=self.vm_name,
            )
