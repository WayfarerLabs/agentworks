"""Clone request custody and incarnation association, without live provider claims."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agentworks.db import Database
from agentworks.db.operations import (
    LifecycleObligation,
    LifecycleObligationState,
    OperationOwnership,
    OperationRepository,
    OperationResourceKind,
    OperationScope,
)
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import ProxmoxCloneRequest, ProxmoxConnection, _ProxmoxWire
from agentworks.operations import OperationOwner
from agentworks.plugins.proxmox._creation import (
    CloneAssociation,
    CloneIntent,
    ProxmoxClone,
    decode_clone_receipt,
    encode_clone_intent,
)

MARKER = "a" * 32
SOURCE_UUID = "12345678-1234-1234-1234-123456789abc"
DESTINATION_UUID = "87654321-4321-4321-4321-cba987654321"
CONNECTION = ProxmoxConnection("https://pve.example:8006", "node1", 9000, "usér@pve!worker", "secret-sentinel")
REQUEST = ProxmoxCloneRequest(123, "test-vm", f"agentworks-instance:{MARKER}", "local-lvm", "agentworks")
UPID = "UPID:node1:000000AB:100000001:000000CD:qmclone:9000:usér@pve!worker:"
OwnedClone = tuple[OperationRepository, OperationOwner, ProxmoxClone, list[tuple[str, int | str]]]


def task(**changes: object) -> dict[str, object]:
    return {
        "upid": UPID,
        "node": "node1",
        "pid": 171,
        "pstart": 0x100000001,
        "starttime": 205,
        "type": "qmclone",
        "id": "9000",
        "user": "usér@pve",
        "tokenid": "worker",
        "status": "stopped",
        "exitstatus": "OK",
        **changes,
    }


@pytest.fixture
def owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedClone]:
    # These wires perform no timed I/O. Dedicated deadline tests replace this clock.
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    custody = LocalDeliveryCustody()
    with closing(Database(tmp_path / "clone.db")) as database:
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
        clone = ProxmoxClone(owner, "vm-one", CONNECTION, REQUEST, instance_marker=MARKER, custody=custody)
        calls: list[tuple[str, int | str]] = []

        def config(wire: _ProxmoxWire, *, timeout: float, custody: LocalDeliveryCustody) -> dict[str, object]:
            assert 0 < timeout <= 5 and custody is clone._custody
            calls.append(("config", wire._connection.vmid))
            if wire._connection.vmid == 9000:
                return {"template": 1, "smbios1": f"uuid={SOURCE_UUID}"}
            return {"smbios1": f"uuid={DESTINATION_UUID}", "description": REQUEST.description}

        def post(
            wire: _ProxmoxWire, request: ProxmoxCloneRequest, *, timeout: float, custody: LocalDeliveryCustody
        ) -> str:
            assert request is REQUEST and custody is clone._custody and 0 < timeout <= 5
            row = owner.inspect_lifecycle_obligation(clone.obligation_id)
            assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
            intent = json.loads(row.payload)
            assert intent["request"]["newid"] == 123 and intent["source_vmid"] == 9000 and intent["upid"] is None
            assert intent["source_smbios_uuid"] == clone.intent.source_smbios_uuid
            assert b"secret-sentinel" not in row.payload
            calls.append(("clone", request.newid))
            return UPID

        def status(
            wire: _ProxmoxWire, upid: str, *, timeout: float, custody: LocalDeliveryCustody
        ) -> dict[str, object]:
            assert upid == UPID and custody is clone._custody and 0 < timeout <= 5
            calls.append(("task", upid))
            return task()

        monkeypatch.setattr(_ProxmoxWire, "request_current_config", config)
        monkeypatch.setattr(_ProxmoxWire, "request_vm_clone", post)
        monkeypatch.setattr(_ProxmoxWire, "request_task_status", status)
        yield repository, owner, clone, calls
        assert custody.close(Deadline.after(1))


def _row(owner: OperationOwner, clone: ProxmoxClone) -> LifecycleObligation:
    row = owner.inspect_lifecycle_obligation(clone.obligation_id)
    assert row is not None
    return row


@pytest.mark.parametrize("retained", [False, True])
def test_interrupted_destination_retention_never_resolves_without_durable_uuid(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch, retained: bool
) -> None:
    repository, owner, clone, calls = owned
    clone.start(Deadline.after(5))
    primary = KeyboardInterrupt("destination retention interrupted")

    class InterruptedHistory(list[CloneIntent]):
        armed = True

        def append(self, value: CloneIntent) -> None:
            if value.destination_smbios_uuid is None or not self.armed:
                super().append(value)
                return
            self.armed = False
            if retained:
                super().append(value)
            raise primary

    clone._history = InterruptedHistory(clone._history)
    original = repository.resolve_lifecycle_obligation
    resolution_evidence: list[str | None] = []

    def resolve(ownership: OperationOwnership, obligation_id: str) -> LifecycleObligation:
        row = repository.inspect_lifecycle_obligation(ownership, obligation_id)
        assert row is not None
        uuid: object = json.loads(row.payload)["destination_smbios_uuid"]
        assert uuid is None or isinstance(uuid, str)
        resolution_evidence.append(uuid)
        return original(ownership, obligation_id)

    monkeypatch.setattr(repository, "resolve_lifecycle_obligation", resolve)
    with pytest.raises(KeyboardInterrupt) as caught:
        clone.wait(Deadline.after(5))
    assert caught.value is primary
    clone.reconcile(Deadline.after(5))
    row = _row(owner, clone)
    if retained:
        assert row.state is LifecycleObligationState.RESOLVED
        assert json.loads(row.payload)["destination_smbios_uuid"] == DESTINATION_UUID
    else:
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert json.loads(row.payload)["destination_smbios_uuid"] is None
        assert clone.association is None
    assert clone.wait(Deadline.after(5)) == CloneAssociation("node1", 123, DESTINATION_UUID)
    assert _row(owner, clone).state is LifecycleObligationState.RESOLVED
    assert json.loads(_row(owner, clone).payload)["destination_smbios_uuid"] == DESTINATION_UUID
    assert resolution_evidence == [DESTINATION_UUID]
    assert calls.count(("clone", 123)) == 1


@pytest.mark.parametrize("corruption", [None, {"version": 2}, {"source_vmid": True}, {"unknown": "field"}])
def test_corrupt_persisted_clone_payload_refuses_reconciliation(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch, corruption: dict[str, object] | None
) -> None:
    repository, owner, clone, calls = owned
    original = repository.publish_lifecycle_obligation_payload

    def corrupt(*args: Any, **kwargs: Any) -> LifecycleObligation:
        value = json.loads(kwargs["payload"])
        kwargs["payload"] = json.dumps(
            {"foreign": True} if corruption is None else {**value, **corruption}, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "publish_lifecycle_obligation_payload", corrupt)
    clone.start(Deadline.after(5))
    with pytest.raises(StateError):
        clone.reconcile(Deadline.after(5))
    persisted = json.loads(_row(owner, clone).payload)
    if corruption is None:
        assert persisted == {"foreign": True}
    else:
        assert all(persisted[key] == value for key, value in corruption.items())
    assert _row(owner, clone).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert clone.association is None
    assert calls == [("config", 9000), ("clone", 123)]


def test_exact_clone_source_receipt_then_fresh_destination_association(owned: OwnedClone) -> None:
    repository, owner, clone, calls = owned
    assert calls == []
    receipt = clone.start(Deadline.after(5))
    assert receipt.source_vmid == "9000"
    assert clone.intent.source_smbios_uuid == SOURCE_UUID
    assert clone.association is None
    owner.stop_admission()
    association = clone.wait(Deadline.after(5))
    assert association == CloneAssociation("node1", 123, DESTINATION_UUID)
    row = _row(owner, clone)
    assert row.state is LifecycleObligationState.RESOLVED and row.payload_revision == 2
    assert json.loads(row.payload)["destination_smbios_uuid"] == DESTINATION_UUID
    assert repository.inspect(owner.ownership.scope) is not None
    assert calls == [("config", 9000), ("clone", 123), ("task", UPID), ("config", 123)]
    clone.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        clone.start(Deadline.after(5))


@pytest.mark.parametrize(
    "change",
    [
        {"id": "123"},
        {"node": "other"},
        {"pid": True},
        {"tokenid": "different"},
        {"upid": UPID + "x"},
        {"status": "stopped", "exitstatus": "collision"},
        {"status": "stopped", "exitstatus": "WARNINGS: 1"},
    ],
)
def test_task_failure_or_foreign_task_never_reads_or_adopts_destination(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    _, owner, clone, calls = owned
    clone.start(Deadline.after(5))
    monkeypatch.setattr(_ProxmoxWire, "request_task_status", lambda *args, **kwargs: task(**change))
    with pytest.raises(StateError):
        clone.wait(Deadline.after(5))
    assert ("config", 123) not in calls and clone.association is None
    assert _row(owner, clone).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize(
    "change",
    [
        {"lock": "clone"},
        {"lock": None},
        {"description": "wrong-marker"},
        {"smbios1": f"uuid={SOURCE_UUID}"},
        {"smbios1": "uuid=00000000-0000-0000-0000-000000000000"},
        {"smbios1": "serial=public"},
        {"smbios1": f"uuid={DESTINATION_UUID},uuid={DESTINATION_UUID}"},
    ],
)
def test_terminal_clone_still_requires_unlocked_exact_marked_fresh_config(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    _, owner, clone, _ = owned
    clone.start(Deadline.after(5))
    config = {"description": REQUEST.description, "smbios1": f"uuid={DESTINATION_UUID}", **change}
    monkeypatch.setattr(_ProxmoxWire, "request_current_config", lambda *args, **kwargs: config)
    with pytest.raises((StateError, ValidationError)):
        clone.wait(Deadline.after(5))
    assert clone.association is None
    assert _row(owner, clone).state is LifecycleObligationState.POSSIBLE_EFFECT


def test_lost_clone_reply_retains_unknown_without_destination_observation_or_replay(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner, clone, calls = owned

    def lost(*args: Any, **kwargs: Any) -> NoReturn:
        calls.append(("lost-post", 123))
        raise TimeoutError("lost clone response")

    monkeypatch.setattr(_ProxmoxWire, "request_vm_clone", lost)
    with pytest.raises(TimeoutError):
        clone.start(Deadline.after(5))
    clone.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        clone.wait(Deadline.after(5))
    with pytest.raises(StateError):
        clone.start(Deadline.after(5))
    assert calls == [("config", 9000), ("lost-post", 123)]
    assert _row(owner, clone).state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("after_commit", [False, True])
def test_lost_acknowledgment_publication_keeps_receipt_and_reconciles_without_post(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    repository, _, clone, calls = owned
    original = repository.publish_lifecycle_obligation_payload

    def lost(*args: Any, **kwargs: Any) -> NoReturn:
        if after_commit:
            original(*args, **kwargs)
        raise RuntimeError("publication reply lost")

    monkeypatch.setattr(repository, "publish_lifecycle_obligation_payload", lost)
    with pytest.raises(RuntimeError):
        clone.start(Deadline.after(5))
    assert clone.intent.upid == UPID
    monkeypatch.setattr(repository, "publish_lifecycle_obligation_payload", original)
    clone.reconcile(Deadline.after(5))
    assert clone.wait(Deadline.after(5)).vmid == 123
    assert calls.count(("clone", 123)) == 1


def test_expired_and_unbounded_deadlines_refuse_before_provider(owned: OwnedClone) -> None:
    _, _, clone, calls = owned
    with pytest.raises(ValidationError):
        clone.start(Deadline(None))
    with pytest.raises(TimeoutError):
        clone.start(Deadline.after(0))
    assert calls == []


@pytest.mark.parametrize("stage", ["register", "mark", "resolve"])
@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_ledger_transitions_keep_original_control_and_no_replay(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch, stage: str, committed: bool
) -> None:
    repository, owner, clone, calls = owned
    name = {
        "register": "register_lifecycle_obligation",
        "mark": "mark_lifecycle_obligation_possible_effect",
        "resolve": "resolve_lifecycle_obligation",
    }[stage]
    original: Callable[..., LifecycleObligation] = getattr(repository, name)
    primary = KeyboardInterrupt("control preserved")

    def interrupted(*args: Any, **kwargs: Any) -> NoReturn:
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(repository, name, interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        clone.start(Deadline.after(5))
        clone.wait(Deadline.after(5))
    assert caught.value is primary
    monkeypatch.setattr(repository, name, original)
    clone.reconcile(Deadline.after(5))
    if stage == "register" and not committed:
        assert owner.inspect_lifecycle_obligation(clone.obligation_id) is None
    else:
        row = _row(owner, clone)
        assert row.state is LifecycleObligationState.RESOLVED
    assert calls.count(("clone", 123)) == int(stage == "resolve")
    with pytest.raises(StateError):
        clone.start(Deadline.after(5))


@pytest.mark.parametrize("stage", ["register", "mark"])
def test_deadline_after_arming_or_registration_settles_never_entered_clone(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    repository, owner, clone, calls = owned
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    name = "register_lifecycle_obligation" if stage == "register" else "mark_lifecycle_obligation_possible_effect"
    original: Callable[..., LifecycleObligation] = getattr(repository, name)

    def late(*args: Any, **kwargs: Any) -> LifecycleObligation:
        result = original(*args, **kwargs)
        clock[0] += 6
        return result

    monkeypatch.setattr(repository, name, late)
    with pytest.raises(TimeoutError):
        clone.start(Deadline.after(5))
    monkeypatch.setattr(repository, name, original)
    clone.reconcile(Deadline.after(5))
    assert _row(owner, clone).state is LifecycleObligationState.RESOLVED
    assert calls == [("config", 9000)] and clone.association is None


def test_never_entered_post_keeps_registration_until_local_custody_settles(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, owner, clone, calls = owned
    local_settled = [False]

    def interrupted(*args: Any, **kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt("before arm")

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", interrupted)
    with pytest.raises(KeyboardInterrupt):
        clone.start(Deadline.after(5))
    monkeypatch.setattr(LocalDeliveryCustody, "settled", property(lambda _: local_settled[0]))
    clone.reconcile(Deadline.after(5))
    assert _row(owner, clone).state is LifecycleObligationState.REGISTERED
    local_settled[0] = True
    clone.reconcile(Deadline.after(5))
    assert _row(owner, clone).state is LifecycleObligationState.RESOLVED
    assert calls == [("config", 9000)]


@pytest.mark.parametrize("committed", [False, True])
def test_never_entered_post_resolution_reply_is_retryable(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    repository, owner, clone, calls = owned

    def before_arm(*args: Any, **kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt("before arm")

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", before_arm)
    with pytest.raises(KeyboardInterrupt):
        clone.start(Deadline.after(5))
    original = repository.resolve_lifecycle_obligation
    primary = KeyboardInterrupt("resolve reply interrupted")

    def interrupted(*args: Any, **kwargs: Any) -> NoReturn:
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(repository, "resolve_lifecycle_obligation", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        clone.reconcile(Deadline.after(5))
    assert caught.value is primary
    monkeypatch.setattr(repository, "resolve_lifecycle_obligation", original)
    clone.reconcile(Deadline.after(5))
    assert _row(owner, clone).state is LifecycleObligationState.RESOLVED
    assert calls == [("config", 9000)]


def test_never_entered_post_cannot_settle_after_owner_takeover(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, owner, clone, calls = owned

    def before_arm(*args: Any, **kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt("before arm")

    monkeypatch.setattr(repository, "mark_lifecycle_obligation_possible_effect", before_arm)
    with pytest.raises(KeyboardInterrupt):
        clone.start(Deadline.after(5))
    OperationOwner.recover(repository, owner.ownership, "b" * 32)
    with pytest.raises(StateError):
        clone.reconcile(Deadline.after(5))
    assert calls == [("config", 9000)]


def test_stale_owner_cannot_observe_task_or_destination(owned: OwnedClone) -> None:
    repository, owner, clone, calls = owned
    clone.start(Deadline.after(5))
    OperationOwner.recover(repository, owner.ownership, "b" * 32)
    with pytest.raises(StateError):
        clone.wait(Deadline.after(5))
    assert calls == [("config", 9000), ("clone", 123)]


def test_late_acknowledgment_is_retained_without_replay(owned: OwnedClone, monkeypatch: pytest.MonkeyPatch) -> None:
    _, owner, clone, calls = owned
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def late(*args: Any, **kwargs: Any) -> str:
        calls.append(("clone", 123))
        clock[0] += 6
        return UPID

    monkeypatch.setattr(_ProxmoxWire, "request_vm_clone", late)
    with pytest.raises(TimeoutError):
        clone.start(Deadline.after(5))
    assert clone.intent.upid == UPID
    clone.reconcile(Deadline.after(5))
    assert json.loads(_row(owner, clone).payload)["upid"] == UPID
    assert calls.count(("clone", 123)) == 1


def test_template_without_smbios_uuid_still_requires_new_destination_uuid(
    owned: OwnedClone, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, clone, _ = owned
    original = _ProxmoxWire.request_current_config

    def config(wire: _ProxmoxWire, *, timeout: float, custody: LocalDeliveryCustody) -> dict[str, object]:
        if wire._connection.vmid == 9000:
            return {"template": 1}
        return original(wire, timeout=timeout, custody=custody)

    monkeypatch.setattr(_ProxmoxWire, "request_current_config", config)
    clone.start(Deadline.after(5))
    assert clone.intent.source_smbios_uuid is None
    assert clone.wait(Deadline.after(5)).smbios_uuid == DESTINATION_UUID


def test_payload_encoder_and_receipt_reject_inconsistent_identity() -> None:
    intent = CloneIntent(CONNECTION.api_url, "node1", 9000, CONNECTION.token_id, REQUEST)
    assert json.loads(encode_clone_intent(intent))["request"]["newid"] == REQUEST.newid
    for upid in (UPID.replace(":9000:", ":123:"), UPID.replace("qmclone", "qmstart"), UPID + "x"):
        with pytest.raises(ValidationError):
            decode_clone_receipt(upid, intent)
    with pytest.raises(ValidationError):
        encode_clone_intent(replace(intent, source_vmid=True))
    with pytest.raises(ValidationError):
        encode_clone_intent(replace(intent, destination_smbios_uuid=DESTINATION_UUID))
