"""Core creation's retained consumer of independent SSH trust maintenance."""

from __future__ import annotations

import json
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.db import LifecycleObligationState, OperationResourceKind
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHTrustFiles
from agentworks.execution.carriers.ssh.enrollment import (
    SSHCreationProvenance,
    SSHEnrollmentCustody,
    enroll_new_target,
)
from agentworks.execution.carriers.ssh.trust import refresh_trust, resolve_trust, trust_status
from agentworks.operations import LifecycleObligation
from agentworks.vms._ssh_native_binding import vm_ssh_connection
from agentworks.vms.identity import validate_vm_instance_marker

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.ssh import SSHSettings
    from agentworks.operations import OperationOwner


_KIND = "vm-creation-ssh-trust"


class CreationSSHTrust:
    """Enroll once, publish complete policy, and retain exact local writer debt.

    Construction is passive. The outer workflow retains this object before
    prepare and closes it before route cleanup. Successful preparation settles
    its own obligation before the canonical route borrows the owner. Uncertain
    global publication stays unresolved after writer cleanup; close never replays it.
    """

    def __init__(
        self, owner: OperationOwner, vm: VMRow, endpoint: str, settings: SSHSettings, *, deadline: Deadline
    ) -> None:
        if (
            owner.ownership.scope.resource_kind is not OperationResourceKind.VM
            or owner.ownership.scope.resource_name != vm.name
        ):
            raise ValidationError("SSH creation trust requires exact VM ownership")
        if deadline.expires_at is None:
            raise ValidationError("SSH creation trust requires a finite deadline")
        marker = validate_vm_instance_marker(vm.instance_marker)
        self._connection = vm_ssh_connection(vm, endpoint, settings)
        self._bundle = ManagedSSHTrust(settings.trust_store)
        self._provenance = SSHCreationProvenance("vm:" + marker, endpoint)
        self._owner = owner
        self._deadline = deadline
        self._delivery = LocalDeliveryCustody()
        self._custody = SSHEnrollmentCustody(self._delivery)
        self._lock = Lock()
        self._obligation_id = uuid4().hex
        self._history: list[bytes] = []
        self._intent: dict[str, object] = {}
        self._registration_entered = False
        self._used = False
        self._closed = False
        self._settled = False
        self._publication_entered = False
        self._published_generation: str | None = None

    @staticmethod
    def _remaining(deadline: Deadline) -> float:
        if deadline.expires_at is None:
            raise ValidationError("SSH creation trust requires a finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0:
            raise TimeoutError("SSH creation trust deadline expired")
        return remaining

    def _acquire(self, deadline: Deadline) -> None:
        if not self._lock.acquire(timeout=min(self._remaining(deadline), TIMEOUT_MAX)):
            raise TimeoutError("SSH creation trust transition deadline expired")

    def prepare(self) -> None:
        """Admit current ordinary ownership before policy reads or first contact."""
        self._acquire(self._deadline)
        try:
            if self._used or self._closed:
                raise StateError("SSH creation trust cannot be prepared again")
            self._used = True
            # Match creation's existing owner fence before any local policy I/O.
            with self._owner._guard:  # noqa: SLF001
                self._owner._reconcile_transition_locked()  # noqa: SLF001
                self._owner._require_dispatch_admission_locked()  # noqa: SLF001
                self._owner._require_no_active_work_locked()  # noqa: SLF001
                if self._owner._obligations_sealed or self._owner._recovery_owner:  # noqa: SLF001
                    raise StateError("SSH creation trust requires an unsealed ordinary owner")
            self._remaining(self._deadline)
            before = trust_status(self._bundle)
            snapshot = resolve_trust(self._bundle)
            after = trust_status(self._bundle)
            self._remaining(self._deadline)
            base = before.generation
            if before.blocked or after.blocked or base is None or base != after.generation:
                raise StateError("SSH creation trust base policy changed during admission")
            intent = {
                "resource_id": self._provenance.resource_id,
                "host": self._connection.host,
                "port": self._connection.port,
                "user": self._connection.user,
                "host_key_alias": self._connection.host_key_alias,
                "trust_store": str(self._bundle.directory),
                "base_generation": base,
                "known_hosts": [str(path) for path in snapshot.known_hosts],
                "revoked_host_keys": str(snapshot.revoked_host_keys) if snapshot.revoked_host_keys else None,
            }
            self._history.append(self._encode(intent, "enrollment"))
            self._intent = intent
            self._registration_entered = True
            obligation = self._owner.register_lifecycle_obligation(
                _KIND, payload_version=1, payload=self._history[0], obligation_id=self._obligation_id
            )
            self._remaining(self._deadline)
            obligation.mark_possible_effect()
            self._remaining(self._deadline)
            candidate = enroll_new_target(
                self._connection, provenance=self._provenance, deadline=self._deadline, custody=self._custody
            )
            self._remaining(self._deadline)
            if candidate.base_generation != base:
                raise StateError("SSH creation candidate does not match the retained policy generation")
            if not self._custody.close(self._deadline):
                raise StateError("SSH creation candidate writer cleanup remains unsettled")
            self._remaining(self._deadline)
            self._history.append(self._encode(intent, "publication-pending"))
            self._publish_history()
            self._remaining(self._deadline)
            self._publication_entered = True
            published = refresh_trust(
                self._bundle,
                sources=SSHTrustFiles((candidate.known_hosts_file, *snapshot.known_hosts), snapshot.revoked_host_keys),
                authority="core-creation:" + self._provenance.resource_id,
                expected_generation=base,
            )
            if published.blocked or published.generation is None or published.generation == base:
                raise StateError("SSH creation publication did not return a complete new generation")
            self._published_generation = published.generation
            self._complete_history()
            self._publish_history()
            self._remaining(self._deadline)
            self._resolve_known_obligation()
            self._settled = True
            self._remaining(self._deadline)
        finally:
            self._lock.release()

    @staticmethod
    def _encode(intent: dict[str, object], phase: str, *, generation: str | None = None) -> bytes:
        payload = json.dumps(
            {"version": 1, **intent, "phase": phase, "published_generation": generation},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            raise ValidationError("SSH creation trust intent exceeds the lifecycle bound")
        return payload

    def _read_obligation(self) -> LifecycleObligation | None:
        row = self._owner.inspect_lifecycle_obligation(self._obligation_id)
        if row is None:
            if self._publication_entered:
                raise StateError("SSH creation trust publication has no retained lifecycle obligation")
            return None
        revision = row.payload_revision
        if (
            row.obligation_kind != _KIND
            or row.payload_version != 1
            or not 0 <= revision < len(self._history)
            or row.payload != self._history[revision]
        ):
            raise StateError("SSH creation trust lifecycle custody conflicts")
        return LifecycleObligation(self._owner, row)

    def _publish_history(self) -> None:
        obligation = self._read_obligation()
        if obligation is None or obligation.state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("SSH creation trust lifecycle publication is not armed")
        for revision in range(obligation.payload_revision + 1, len(self._history)):
            obligation.publish_payload(
                expected_revision=revision - 1, payload_version=1, payload=self._history[revision]
            )

    def _complete_history(self) -> None:
        if self._published_generation is not None and len(self._history) == 2:
            self._history.append(self._encode(self._intent, "published", generation=self._published_generation))

    def _resolve_known_obligation(self) -> None:
        self._complete_history()
        obligation = self._read_obligation()
        if obligation is None:
            return
        if (
            self._published_generation is not None
            and obligation.state is LifecycleObligationState.RESOLVED
            and obligation.payload_revision != len(self._history) - 1
        ):
            raise StateError("SSH creation trust lifecycle obligation was prematurely resolved")
        if self._published_generation is not None and obligation.state is not LifecycleObligationState.RESOLVED:
            self._publish_history()
            obligation = self._read_obligation()
            assert obligation is not None
        if obligation.state is not LifecycleObligationState.RESOLVED:
            obligation.resolve()

    def close(self, deadline: Deadline) -> bool:
        """Close the actual writer first, then settle only this known obligation.

        A failed first contact leaves its candidate as evidence but no global
        policy debt. Once refresh is entered, only a returned complete receipt
        permits resolution. Unknown publication requires separate maintenance.
        """
        self._acquire(deadline)
        try:
            self._closed = True
            if self._settled:
                return True
            if not self._custody.close(deadline):
                return False
            self._remaining(deadline)
            if not self._registration_entered:
                self._settled = True
                return True
            if self._publication_entered and self._published_generation is None:
                return False
            self._resolve_known_obligation()
            self._settled = True
            return True
        finally:
            self._lock.release()
