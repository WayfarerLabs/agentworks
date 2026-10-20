"""Real SQLite closing-owner rollback with scripted provider observations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from agentworks.db.operations import LifecycleObligation, LifecycleObligationState, OperationOwnership
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers._proxmox_http import _Endpoint
from agentworks.execution.carriers.proxmox import _ProxmoxWire, _WireFailure
from agentworks.plugins.proxmox._configuration import ProxmoxConfiguration
from agentworks.plugins.proxmox._rollback import RollbackPhase
from tests.plugins.test_proxmox_creation_access import TOKEN, Harness
from tests.plugins.test_proxmox_creation_access import database as database
from tests.plugins.test_proxmox_creation_access import owned as owned

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody

_GENERATION = "11111111-2222-3333-4444-555555555555"
_MUTATIONS = (_Endpoint.VM_STOP, _Endpoint.VM_REMOVE_DISKS, _Endpoint.VM_DESTROY)
_NEW_READS = (
    _Endpoint.SNAPSHOTS,
    _Endpoint.PENDING,
    _Endpoint.VM_PERMISSIONS,
    _Endpoint.STORAGE_PERMISSIONS,
    _Endpoint.STORAGE_IMAGES,
    _Endpoint.VMID_FREE,
)


@dataclass
class RollbackHarness:
    creation: Harness
    alive: bool = True
    images: list[dict[str, object]] = field(
        default_factory=lambda: [
            {"volid": "local-lvm:vm-123-disk-0", "vmid": 123, "format": "raw"},
        ]
    )
    snapshots: object = field(default_factory=lambda: [{"name": "current"}])
    pending: object = field(default_factory=lambda: [{"key": "scsi0", "value": "unchanged", "delete": 0}])
    vm_grants: dict[str, object] = field(
        default_factory=lambda: {
            "VM.Config.Disk": 0,
            "VM.Config.CDROM": 0,
            "VM.Config.Cloudinit": 0,
        }
    )
    storage_grants: dict[str, object] = field(
        default_factory=lambda: {
            "Datastore.Audit": 0,
            "Datastore.AllocateSpace": 0,
        }
    )
    lose_reply: _Endpoint | None = None
    primary: BaseException = field(default_factory=lambda: KeyboardInterrupt("rollback response interrupted"))
    task_change: dict[str, object] = field(default_factory=dict)
    receipt_override: str | None = None
    retain_image: bool = False
    storage_error: bool = False
    hide_images: bool = False
    revoke_after_remove: bool = False
    power_after_stop: str = "stopped"
    free_error: bool = False

    def exchange(
        self,
        wire: _ProxmoxWire,
        endpoint: _Endpoint,
        method: str,
        suffix: str | None,
        *,
        custody: LocalDeliveryCustody,
        body: bytes | None,
        timeout: float | None,
    ) -> object:
        h = self.creation
        assert wire._connection.token_secret == TOKEN
        assert custody is h.custody and custody.settled and timeout is not None and 0 < timeout <= 5
        if not self.alive:
            assert endpoint in (_Endpoint.TASK_STATUS, _Endpoint.VMID_FREE)
        if endpoint is _Endpoint.TASK_STATUS:
            h.calls.append((endpoint, method, wire._connection.vmid))
            data = h.reply(endpoint, method, suffix, body, wire._connection.vmid)
            assert isinstance(data, dict)
            return {**data, **self.task_change}
        if endpoint not in (*_MUTATIONS, *_NEW_READS):
            return h.exchange(wire, endpoint, method, suffix, custody=custody, body=body, timeout=timeout)
        h.calls.append((endpoint, method, wire._connection.vmid))
        assert wire._connection.vmid == 123
        row = h.row()
        assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
        state = json.loads(row.payload)["rollback"]
        if endpoint in _MUTATIONS:
            phase, kind = {
                _Endpoint.VM_STOP: ("stop-requested", "qmstop"),
                _Endpoint.VM_REMOVE_DISKS: ("remove-requested", "qmconfig"),
                _Endpoint.VM_DESTROY: ("destroy-requested", "qmdestroy"),
            }[endpoint]
            assert state["phase"] == phase
            assert h.owner._close_requested.is_set() and not h.owner._obligations_sealed
            if endpoint is _Endpoint.VM_STOP:
                h.power = self.power_after_stop
            elif endpoint is _Endpoint.VM_REMOVE_DISKS:
                assert body is not None
                request = json.loads(body)
                assert request == {
                    "delete": ",".join(disk["key"] for disk in state["disks"]),
                    "force": 1,
                    "digest": h.current["digest"],
                }
                h.bodies.append((endpoint, body))
                for key in request["delete"].split(","):
                    del h.current[key]
                h.current["digest"] = "d" * 40
                if not self.retain_image:
                    self.images.clear()
                if self.revoke_after_remove:
                    self.vm_grants.clear()
            else:
                assert state["phase"] == "destroy-requested" and not self.images
                self.alive = False
            if self.lose_reply is endpoint:
                raise self.primary
            return self.receipt_override or f"UPID:node1:000000AB:100000001:000000CD:{kind}:123:worker@pve!creation:"
        if endpoint is _Endpoint.SNAPSHOTS:
            return self.snapshots
        if endpoint is _Endpoint.PENDING:
            return self.pending
        if endpoint is _Endpoint.VM_PERMISSIONS:
            return {"/vms/123": self.vm_grants.copy()}
        if endpoint is _Endpoint.STORAGE_PERMISSIONS:
            assert suffix == "local-lvm"
            return {"/storage/local-lvm": self.storage_grants.copy()}
        if endpoint is _Endpoint.STORAGE_IMAGES:
            assert suffix == "local-lvm"
            if self.storage_error:
                raise _WireFailure("storage unavailable")
            return [] if self.hide_images else self.images.copy()
        assert endpoint is _Endpoint.VMID_FREE
        if self.free_error:
            raise _WireFailure("VMID availability unproved")
        assert not self.alive
        return 123

    def mutations(self) -> list[_Endpoint]:
        return [endpoint for endpoint, _, _ in self.creation.calls if endpoint in _MUTATIONS]

    def close_for_rollback(self) -> None:
        self.creation.access.request_rollback()
        self.creation.owner.stop_admission()

    def settle(self) -> bool:
        return self.creation.access.settle(Deadline.after(5))


def _install(h: Harness, monkeypatch: pytest.MonkeyPatch) -> RollbackHarness:
    value = RollbackHarness(h)

    def exchange(
        wire: _ProxmoxWire,
        endpoint: _Endpoint,
        method: str,
        suffix: str | None,
        *,
        custody: LocalDeliveryCustody,
        body: bytes | None,
        timeout: float | None,
    ) -> object:
        return value.exchange(wire, endpoint, method, suffix, custody=custody, body=body, timeout=timeout)

    monkeypatch.setattr(_ProxmoxWire, "_exchange", exchange)
    return value


@pytest.fixture
def rollback_owned(owned: Harness, monkeypatch: pytest.MonkeyPatch) -> RollbackHarness:
    value = _install(owned, monkeypatch)
    owned.access.provision(Deadline.after(5))
    return value


def test_normal_rollback_resolves_exact_resource_under_closing_original_owner(rollback_owned: RollbackHarness) -> None:
    h = rollback_owned.creation
    owner, controllers = h.owner, (h.access.clone, h.access.configuration, h.access.activation, h.access.bootstrap)
    rollback_owned.close_for_rollback()
    assert rollback_owned.settle()
    assert rollback_owned.mutations() == list(_MUTATIONS)
    assert (row := h.row()) is not None and row.state is LifecycleObligationState.RESOLVED
    assert h.access.rollback is not None and h.access.rollback.state.phase is RollbackPhase.COMPLETE
    assert not rollback_owned.alive and not rollback_owned.images and h.owner is owner
    assert controllers == (h.access.clone, h.access.configuration, h.access.activation, h.access.bootstrap)
    before = h.calls.copy()
    assert rollback_owned.settle() and h.calls == before
    assert h.database.operations.inspect(owner.ownership.scope) is not None
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert h.database.operations.inspect(owner.ownership.scope) is None


@pytest.mark.parametrize("endpoint", _MUTATIONS)
def test_lost_mutation_reply_keeps_original_control_and_never_replays(
    rollback_owned: RollbackHarness,
    endpoint: _Endpoint,
) -> None:
    value = rollback_owned
    value.lose_reply = endpoint
    value.primary.__cause__ = RuntimeError("original cause")
    value.close_for_rollback()
    with pytest.raises(KeyboardInterrupt) as caught:
        value.settle()
    assert caught.value is value.primary and caught.value.__cause__ is value.primary.__cause__
    before = value.mutations()
    value.lose_reply = None
    with pytest.raises(StateError):
        value.settle()
    assert value.mutations() == before and before.count(endpoint) == 1
    assert (row := value.creation.row()) is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize(
    "phase,receipt",
    [
        ("stop-requested", None),
        ("stop-requested", "stop_upid"),
        ("remove-requested", None),
        ("remove-requested", "remove_upid"),
        ("destroy-requested", None),
        ("destroy-requested", "destroy_upid"),
        ("complete", None),
    ],
)
def test_publication_reply_loss_reconciles_exact_retained_state_without_replay(
    rollback_owned: RollbackHarness,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
    phase: str,
    receipt: str | None,
) -> None:
    value, h = rollback_owned, rollback_owned.creation
    repository = h.owner._repository
    original = repository.publish_lifecycle_obligation_payload
    primary = KeyboardInterrupt("resource publication interrupted")
    fired = False

    def publish(
        ownership: OperationOwnership,
        obligation_id: str,
        *,
        expected_revision: int,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        nonlocal fired
        state = json.loads(payload).get("rollback", {})
        upid_field = {
            "stop-requested": "stop_upid",
            "remove-requested": "remove_upid",
            "destroy-requested": "destroy_upid",
        }.get(phase)
        matches = state.get("phase") == phase and (
            receipt is not None
            and state.get(receipt) is not None
            or receipt is None
            and (upid_field is None or state.get(upid_field) is None)
        )
        if obligation_id == h.access._resource_id and matches and not fired:
            fired = True
            if committed:
                original(
                    ownership,
                    obligation_id,
                    expected_revision=expected_revision,
                    payload_version=payload_version,
                    payload=payload,
                )
            raise primary
        return original(
            ownership,
            obligation_id,
            expected_revision=expected_revision,
            payload_version=payload_version,
            payload=payload,
        )

    monkeypatch.setattr(repository, "publish_lifecycle_obligation_payload", publish)
    value.close_for_rollback()
    with pytest.raises(KeyboardInterrupt) as caught:
        value.settle()
    assert caught.value is primary and fired
    monkeypatch.setattr(repository, "publish_lifecycle_obligation_payload", original)
    assert value.settle()
    assert value.mutations() == list(_MUTATIONS)
    assert (row := h.row()) is not None and row.state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize(
    "change",
    [
        {"node": "other"},
        {"id": "124"},
        {"type": "qmstart"},
        {"tokenid": "other"},
        {"pid": True},
        {"exitstatus": "ERROR"},
        {"upid": "wrong"},
        {"user": "other@pve"},
    ],
)
def test_task_identity_and_completion_cannot_authorize_next_effect(
    rollback_owned: RollbackHarness,
    change: dict[str, object],
) -> None:
    value = rollback_owned
    value.task_change = change
    value.close_for_rollback()
    with pytest.raises(StateError):
        value.settle()
    assert value.mutations() == [_Endpoint.VM_STOP]
    value.task_change.clear()
    assert value.settle() and value.mutations() == list(_MUTATIONS)


@pytest.mark.parametrize(
    "upid",
    [
        "UPID:node1:000000AB:100000001:000000CD:qmstart:123:worker@pve!creation:",
        "UPID:node2:000000AB:100000001:000000CD:qmstop:123:worker@pve!creation:",
        "UPID:node1:000000AB:100000001:000000CD:qmstop:124:worker@pve!creation:",
        "UPID:node1:000000AB:100000001:000000CD:qmstop:123:other@pve!creation:",
    ],
)
def test_wrong_receipt_retains_possible_request_without_replay(rollback_owned: RollbackHarness, upid: str) -> None:
    value = rollback_owned
    value.receipt_override = upid
    value.close_for_rollback()
    with pytest.raises(ValidationError):
        value.settle()
    with pytest.raises(StateError):
        value.settle()
    assert value.mutations() == [_Endpoint.VM_STOP]


@pytest.mark.parametrize(
    "field,value",
    [
        ("snapshots", [{"name": "current"}, {"name": "snap"}]),
        ("snapshots", {"name": "current"}),
        ("snapshots", []),
        ("pending", [{"key": "scsi0", "pending": None}]),
        ("pending", [{"key": "scsi0", "delete": 1}]),
        ("pending", [{"key": "scsi0", "delete": False}]),
    ],
)
def test_unsupported_snapshot_pending_shapes_retain_disks(
    rollback_owned: RollbackHarness,
    field: str,
    value: object,
) -> None:
    h = rollback_owned
    setattr(h, field, value)
    h.close_for_rollback()
    with pytest.raises(StateError):
        h.settle()
    assert h.mutations() == [_Endpoint.VM_STOP] and h.images


@pytest.mark.parametrize(
    "privilege",
    ["VM.Config.Disk", "VM.Config.CDROM", "VM.Config.Cloudinit", "Datastore.Audit", "Datastore.AllocateSpace"],
)
@pytest.mark.parametrize("flag", [None, False, True, "0", 2])
def test_missing_effective_permission_refuses_removal(
    rollback_owned: RollbackHarness,
    privilege: str,
    flag: object,
) -> None:
    h = rollback_owned
    grants = h.vm_grants if privilege.startswith("VM.") else h.storage_grants
    if flag is None:
        grants.pop(privilege)
    else:
        grants[privilege] = flag
    h.close_for_rollback()
    with pytest.raises(StateError):
        h.settle()
    assert h.mutations() == [_Endpoint.VM_STOP]


@pytest.mark.parametrize("failure", ["storage", "hidden", "extra", "retained", "revoked", "foreign", "parent"])
def test_storage_failure_or_incomplete_visibility_never_proves_absence(
    rollback_owned: RollbackHarness,
    failure: str,
) -> None:
    h = rollback_owned
    h.storage_error = failure == "storage"
    h.hide_images = failure == "hidden"
    h.retain_image = failure == "retained"
    h.revoke_after_remove = failure == "revoked"
    if failure == "extra":
        h.images.append({"volid": "local-lvm:vm-123-disk-9", "vmid": 123, "format": "raw"})
    if failure == "foreign":
        h.images[0]["vmid"] = 124
    if failure == "parent":
        h.images[0]["parent"] = "local-lvm:base-9000-disk-0"
    h.close_for_rollback()
    with pytest.raises((StateError, _WireFailure)):
        h.settle()
    assert _Endpoint.VM_DESTROY not in h.mutations() and h.alive


@pytest.mark.parametrize("key", ["scsi1", "virtio0", "sata0", "ide2", "efidisk0", "tpmstate0", "unused0"])
def test_exact_config_drive_keys_include_cloudinit_cdrom_images(rollback_owned: RollbackHarness, key: str) -> None:
    h = rollback_owned
    volume = "local-lvm:vm-123-cloudinit" if key == "ide2" else "local-lvm:vm-123-disk-1"
    h.creation.current[key] = volume + (",media=cdrom" if key == "ide2" else ",size=4M")
    h.creation.current["ide3"] = "none,media=cdrom"
    h.images.append({"volid": volume, "vmid": 123, "format": "raw"})
    h.close_for_rollback()
    assert h.settle()
    body = next(body for endpoint, body in h.creation.bodies if endpoint is _Endpoint.VM_REMOVE_DISKS)
    assert set(json.loads(body)["delete"].split(",")) == {"scsi0", key}


@pytest.mark.parametrize(
    "raw",
    [
        "other:vm-123-disk-1",
        "/dev/sda",
        "none",
        "local-lvm:vm-123-disk-0,shared=1",
        "local-lvm:vm-123-disk-0,file=duplicate",
        "local-lvm:iso/debian.iso,media=cdrom",
    ],
)
def test_unsupported_drive_shapes_do_not_expand_delete_targets(rollback_owned: RollbackHarness, raw: str) -> None:
    h = rollback_owned
    h.creation.current["ide2"] = raw
    h.close_for_rollback()
    with pytest.raises(StateError):
        h.settle()
    assert h.mutations() == [_Endpoint.VM_STOP]


def test_preconfiguration_failure_rolls_back_clone_generation_without_planned_generation(
    owned: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _install(owned, monkeypatch)
    owned.current["vmgenid"] = _GENERATION
    primary = KeyboardInterrupt("before configuration")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise primary

    monkeypatch.setattr(ProxmoxConfiguration, "configure", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.access.provision(Deadline.after(5))
    assert caught.value is primary and owned.current["vmgenid"] != owned.access._generation
    value.close_for_rollback()
    assert value.settle()
    assert value.mutations() == [_Endpoint.VM_REMOVE_DISKS, _Endpoint.VM_DESTROY]
    assert owned.access.rollback is not None and owned.access.rollback.state.generation == _GENERATION


def test_accepted_resource_refuses_rollback_and_stays_present(rollback_owned: RollbackHarness) -> None:
    h = rollback_owned
    h.creation.access.complete_bootstrap(Deadline.after(5))
    with pytest.raises(StateError):
        h.creation.access.request_rollback()
    h.creation.owner.stop_admission()
    assert h.settle() and h.alive and not h.mutations()


def test_destroy_task_success_requires_independent_exact_free_vmid(rollback_owned: RollbackHarness) -> None:
    h = rollback_owned
    h.free_error = True
    h.close_for_rollback()
    with pytest.raises(_WireFailure):
        h.settle()
    assert (row := h.creation.row()) is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
    h.free_error = False
    assert h.settle() and h.mutations() == list(_MUTATIONS)
