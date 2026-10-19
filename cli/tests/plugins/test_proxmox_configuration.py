"""Configuration request custody and exact resize evidence, without live PVE claims."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from typing import Any, NoReturn

import pytest

from agentworks.db import Database
from agentworks.db.operations import (
    LifecycleObligation,
    LifecycleObligationState,
    OperationRepository,
    OperationResourceKind,
    OperationScope,
)
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import ProxmoxConfigurationRequest, ProxmoxConnection, _ProxmoxWire
from agentworks.operations import OperationOwner
from agentworks.plugins.proxmox._configuration import ProxmoxConfiguration, ResizeIntent, _disk
from agentworks.plugins.proxmox._creation import CloneAssociation
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY

MARKER = "a" * 32
SMBIOS = "87654321-4321-4321-4321-cba987654321"
GENERATION = "12345678-1234-1234-1234-123456789abc"
CONNECTION = ProxmoxConnection("https://pve.example:8006", "node1", 123, "usér@pve!worker", "secret-sentinel")
REQUEST = ProxmoxConfigurationRequest(4, 8192, "admin", TEST_SSH_PUBLIC_KEY + "\n", GENERATION)
ASSOCIATION = CloneAssociation("node1", 123, SMBIOS)
UPID = "UPID:node1:000000AB:100000001:000000CD:resize:123:usér@pve!worker:"


def task(**changes: object) -> dict[str, object]:
    return {
        "upid": UPID,
        "node": "node1",
        "pid": 171,
        "pstart": 0x100000001,
        "starttime": 205,
        "type": "resize",
        "id": "123",
        "user": "usér@pve",
        "tokenid": "worker",
        "status": "stopped",
        "exitstatus": "OK",
        **changes,
    }


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Database]:
    # Every case uses its own operation scope. One database bounds retained fixture disk usage.
    with closing(Database(tmp_path_factory.mktemp("configuration-ledger") / "state.db")) as value:
        yield value


@dataclass
class Owned:
    repository: OperationRepository
    owner: OperationOwner
    stage: ProxmoxConfiguration
    calls: list[str]
    current: dict[str, object]
    custody: LocalDeliveryCustody


@pytest.fixture
def owned(database: Database, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[Owned]:
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    repository = database.operations
    owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, request.node.name), "proof")
    custody = LocalDeliveryCustody()
    stage = ProxmoxConfiguration(
        owner,
        request.node.name,
        CONNECTION,
        ASSOCIATION,
        REQUEST,
        instance_marker=MARKER,
        disk_gib=40,
        custody=custody,
    )
    value = Owned(
        repository,
        owner,
        stage,
        [],
        {
            "description": f"agentworks-instance:{MARKER}",
            "smbios1": f"uuid={SMBIOS}",
            "digest": "a" * 40,
            "scsi0": "local-lvm:vm-123-disk-0,size=20G",
        },
        custody,
    )

    def config(wire: _ProxmoxWire, *, timeout: float, custody: LocalDeliveryCustody) -> dict[str, object]:
        assert wire._connection == CONNECTION and custody is value.custody and 0 < timeout <= 5
        value.calls.append("config")
        return value.current.copy()

    def power(wire: _ProxmoxWire, **kwargs: Any) -> dict[str, object]:
        value.calls.append("power")
        return {"status": "stopped"}

    def configure(wire: _ProxmoxWire, supplied: ProxmoxConfigurationRequest, *, digest: str, **kwargs: Any) -> None:
        row = owner.inspect_lifecycle_obligation(stage.configuration_obligation_id)
        assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert json.loads(row.payload)["acknowledged"] is False
        assert b"secret-sentinel" not in row.payload
        assert digest == value.current["digest"] and supplied is REQUEST
        value.calls.append("configure")
        value.current.update(supplied.body("b" * 40))

    def resize(wire: _ProxmoxWire, *, size_gib: int, digest: str, **kwargs: Any) -> str:
        row = owner.inspect_lifecycle_obligation(stage.resize_obligation_id)
        assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
        payload = json.loads(row.payload)
        assert payload["disk"]["volume"] == "local-lvm:vm-123-disk-0" and payload["size_gib"] == size_gib == 40
        assert digest == value.current["digest"] == payload["digest"]
        value.calls.append("resize")
        value.current.update(scsi0="local-lvm:vm-123-disk-0,size=40G", digest="c" * 40)
        return UPID

    def status(wire: _ProxmoxWire, upid: str, **kwargs: Any) -> dict[str, object]:
        assert upid == UPID
        value.calls.append("task")
        return task()

    monkeypatch.setattr(_ProxmoxWire, "request_current_config", config)
    monkeypatch.setattr(_ProxmoxWire, "request_power", power)
    monkeypatch.setattr(_ProxmoxWire, "request_vm_configure", configure)
    monkeypatch.setattr(_ProxmoxWire, "request_vm_resize", resize)
    monkeypatch.setattr(_ProxmoxWire, "request_task_status", status)
    yield value
    assert custody.close(Deadline.after(1))


def test_two_requests_publish_exact_evidence_without_releasing_owner(owned: Owned) -> None:
    value = owned
    assert value.calls == []
    value.stage.configure(Deadline.after(5))
    row = value.owner.inspect_lifecycle_obligation(value.stage.configuration_obligation_id)
    assert row is not None and row.state is LifecycleObligationState.RESOLVED
    assert row.payload_revision == 1 and json.loads(row.payload)["acknowledged"] is True
    value.stage.resize(Deadline.after(5))
    value.owner.stop_admission()
    result = value.stage.wait(Deadline.after(5))
    assert (
        result.task_succeeded and result.verified_disk is not None and result.verified_disk.size_bytes == 40 * 1024**3
    )
    row = value.owner.inspect_lifecycle_obligation(value.stage.resize_obligation_id)
    assert row is not None and row.state is LifecycleObligationState.RESOLVED and row.payload_revision == 3
    assert json.loads(row.payload)["verified_digest"] == "c" * 40
    assert value.repository.inspect(value.owner.ownership.scope) is not None
    assert value.calls == ["config", "power", "configure", "config", "power", "resize", "task", "config", "power"]
    value.stage.reconcile(Deadline.after(5))
    for method in (value.stage.configure, value.stage.resize):
        with pytest.raises(StateError):
            method(Deadline.after(5))


@pytest.mark.parametrize("effect", ["configure", "resize"])
def test_lost_wire_reply_cannot_be_replaced_by_matching_desired_state(
    owned: Owned, monkeypatch: pytest.MonkeyPatch, effect: str
) -> None:
    if effect == "resize":
        owned.stage.configure(Deadline.after(5))
    original = getattr(_ProxmoxWire, f"request_vm_{effect}")
    primary = KeyboardInterrupt("lost wire reply")

    def lost(*args: Any, **kwargs: Any) -> NoReturn:
        original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(_ProxmoxWire, f"request_vm_{effect}", lost)
    with pytest.raises(KeyboardInterrupt) as caught:
        getattr(owned.stage, effect)(Deadline.after(5))
    assert caught.value is primary
    owned.stage.reconcile(Deadline.after(5))
    before = owned.calls.copy()
    with pytest.raises(StateError):
        owned.stage.wait(Deadline.after(5))
    assert owned.calls == before and owned.calls.count(effect) == 1
    identifier = owned.stage.configuration_obligation_id if effect == "configure" else owned.stage.resize_obligation_id
    row = owned.owner.inspect_lifecycle_obligation(identifier)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        {"lock": "backup"},
        {"description": "other"},
        {"smbios1": f"uuid={GENERATION}"},
        {"digest": True},
        {"scsi0": "local-lvm:vm-123-disk-0,size=41G"},
    ],
)
def test_fresh_association_and_no_shrink_precede_each_effect(
    owned: Owned, configured: bool, change: dict[str, object]
) -> None:
    if configured:
        owned.stage.configure(Deadline.after(5))
    owned.current.update(change)
    with pytest.raises((StateError, ValidationError)):
        (owned.stage.resize if configured else owned.stage.configure)(Deadline.after(5))
    assert owned.calls.count("configure") == int(configured) and "resize" not in owned.calls


@pytest.mark.parametrize(
    "change",
    [
        {"memory": True},
        {"memory": "08192"},
        {"cores": False},
        {"cpu": "kvm64"},
        {"agent": "0"},
        {"boot": "order=net0;scsi0"},
        {"ipconfig0": "ip=manual"},
        {"sshkeys": TEST_SSH_PUBLIC_KEY},
        {"vmgenid": "1"},
        {"ciuser": "other"},
        {"scsi0": "local-lvm:vm-123-disk-other,size=20G"},
    ],
)
def test_resize_requires_every_current_setting_and_original_disk(owned: Owned, change: dict[str, object]) -> None:
    owned.stage.configure(Deadline.after(5))
    owned.current.update(change)
    with pytest.raises(StateError):
        owned.stage.resize(Deadline.after(5))
    assert "resize" not in owned.calls


@pytest.mark.parametrize("power", ["running", "paused", None])
def test_stopped_power_is_required_before_put(owned: Owned, monkeypatch: pytest.MonkeyPatch, power: object) -> None:
    monkeypatch.setattr(_ProxmoxWire, "request_power", lambda *args, **kwargs: {"status": power})
    with pytest.raises(StateError):
        owned.stage.configure(Deadline.after(5))
    assert "configure" not in owned.calls


@pytest.mark.parametrize(
    "change",
    [
        {"type": "qmclone"},
        {"id": "9000"},
        {"upid": UPID + "x"},
        {"pid": True},
        {"pstart": 1},
        {"starttime": 1},
        {"node": "other"},
        {"user": "other@pve"},
        {"tokenid": "other"},
        {"exitstatus": "WARNINGS: 1"},
        {"exitstatus": "ERROR"},
        {"status": "unknown"},
    ],
)
def test_only_exact_positive_resize_task_allows_final_observation(
    owned: Owned, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    owned.stage.configure(Deadline.after(5))
    owned.stage.resize(Deadline.after(5))
    monkeypatch.setattr(_ProxmoxWire, "request_task_status", lambda *args, **kwargs: task(**change))
    before = owned.calls.copy()
    with pytest.raises(StateError):
        owned.stage.wait(Deadline.after(5))
    assert owned.calls == before


@pytest.mark.parametrize("size", ["41943040K", "40960M", "40G", "0.0390625T", "42949672960"])
def test_equal_size_still_dispatches_and_disk_size_comparison_uses_bytes(owned: Owned, size: str) -> None:
    owned.current["scsi0"] = f"local-lvm:vm-123-disk-0,size={size}"
    owned.stage.configure(Deadline.after(5))
    owned.current.update(memory=8192, cores="4")
    owned.stage.resize(Deadline.after(5))
    owned.current["scsi0"] = f"file=local-lvm:vm-123-disk-0,size={size}"
    assert owned.stage.wait(Deadline.after(5)).verified_disk is not None
    assert owned.calls.count("resize") == 1 and owned.calls.count("task") == 1


@pytest.mark.parametrize(
    "disk",
    [
        "other:volume,size=40G",
        "local-lvm:vm-123-disk-0,size=39G",
        "local-lvm:vm-123-disk-0,size=41G",
    ],
)
def test_successful_task_does_not_replace_fresh_disk_evidence(owned: Owned, disk: str) -> None:
    owned.stage.configure(Deadline.after(5))
    owned.stage.resize(Deadline.after(5))
    owned.current["scsi0"] = disk
    with pytest.raises(StateError):
        owned.stage.wait(Deadline.after(5))
    row = owned.owner.inspect_lifecycle_obligation(owned.stage.resize_obligation_id)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert json.loads(row.payload)["task_succeeded"] is True and json.loads(row.payload)["verified_disk"] is None


@pytest.mark.parametrize("effect", ["configure", "resize"])
@pytest.mark.parametrize("step", ["register", "arm", "publish", "resolve"])
@pytest.mark.parametrize("committed", [False, True])
def test_lost_ledger_replies_reconcile_without_replaying_effects(
    owned: Owned, monkeypatch: pytest.MonkeyPatch, effect: str, step: str, committed: bool
) -> None:
    if effect == "resize":
        owned.stage.configure(Deadline.after(5))
    name = {
        "register": "register_lifecycle_obligation",
        "arm": "mark_lifecycle_obligation_possible_effect",
        "publish": "publish_lifecycle_obligation_payload",
        "resolve": "resolve_lifecycle_obligation",
    }[step]
    original = getattr(owned.repository, name)
    primary = KeyboardInterrupt("lost ledger reply")

    def lost(*args: Any, **kwargs: Any) -> NoReturn:
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(owned.repository, name, lost)
    with pytest.raises(KeyboardInterrupt) as caught:
        getattr(owned.stage, effect)(Deadline.after(5))
        if effect == "resize" and step == "resolve":
            owned.stage.wait(Deadline.after(5))
    assert caught.value is primary
    monkeypatch.setattr(owned.repository, name, original)
    owned.stage.reconcile(Deadline.after(5))
    if effect == "resize" and step in {"publish", "resolve"}:
        owned.stage.wait(Deadline.after(5))
    assert owned.calls.count(effect) == int(step in {"publish", "resolve"})


@pytest.mark.parametrize("retained", [False, True])
def test_interrupted_final_retention_cannot_resolve_a_split_completion_cache(
    owned: Owned, monkeypatch: pytest.MonkeyPatch, retained: bool
) -> None:
    owned.stage.configure(Deadline.after(5))
    owned.stage.resize(Deadline.after(5))
    primary = KeyboardInterrupt("final retention")

    class InterruptedHistory(list[ResizeIntent]):
        armed = True

        def append(self, value: ResizeIntent) -> None:
            if value.verified_disk is None or not self.armed:
                super().append(value)
                return
            self.armed = False
            if retained:
                super().append(value)
            raise primary

    owned.stage._resize_history = InterruptedHistory(owned.stage._resize_history)
    original = owned.repository.resolve_lifecycle_obligation

    def resolve(*args: Any, **kwargs: Any) -> LifecycleObligation:
        row = owned.repository.inspect_lifecycle_obligation(owned.owner.ownership, owned.stage.resize_obligation_id)
        assert row is not None and json.loads(row.payload)["verified_disk"] is not None
        return original(*args, **kwargs)

    monkeypatch.setattr(owned.repository, "resolve_lifecycle_obligation", resolve)
    with pytest.raises(KeyboardInterrupt) as caught:
        owned.stage.wait(Deadline.after(5))
    assert caught.value is primary
    owned.stage.reconcile(Deadline.after(5))
    row = owned.owner.inspect_lifecycle_obligation(owned.stage.resize_obligation_id)
    assert row is not None
    assert (row.state is LifecycleObligationState.RESOLVED) is retained
    owned.stage.wait(Deadline.after(5))
    assert owned.calls.count("resize") == 1


def test_corrupt_publication_never_resolves(owned: Owned, monkeypatch: pytest.MonkeyPatch) -> None:
    original = owned.repository.publish_lifecycle_obligation_payload

    def corrupt(*args: Any, **kwargs: Any) -> LifecycleObligation:
        kwargs["payload"] = b'{"foreign":true}'
        return original(*args, **kwargs)

    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", corrupt)
    with pytest.raises(StateError):
        owned.stage.configure(Deadline.after(5))
    with pytest.raises(StateError):
        owned.stage.reconcile(Deadline.after(5))
    row = owned.owner.inspect_lifecycle_obligation(owned.stage.configuration_obligation_id)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("effect", ["configuration", "resize"])
def test_version_only_publication_corruption_retains_possible_effect_without_replay(
    owned: Owned, monkeypatch: pytest.MonkeyPatch, effect: str
) -> None:
    if effect == "resize":
        owned.stage.configure(Deadline.after(5))
        owned.stage.resize(Deadline.after(5))
    original = owned.repository.publish_lifecycle_obligation_payload

    def corrupt_version(*args: Any, **kwargs: Any) -> LifecycleObligation:
        value = json.loads(kwargs["payload"])
        if (
            effect == "configuration"
            and value.get("acknowledged") is True
            or (effect == "resize" and value.get("verified_disk") is not None)
        ):
            kwargs["payload_version"] = 2
        return original(*args, **kwargs)

    monkeypatch.setattr(owned.repository, "publish_lifecycle_obligation_payload", corrupt_version)
    with pytest.raises(StateError):
        (owned.stage.configure if effect == "configuration" else owned.stage.wait)(Deadline.after(5))
    identifier = (
        owned.stage.configuration_obligation_id if effect == "configuration" else owned.stage.resize_obligation_id
    )
    row = owned.owner.inspect_lifecycle_obligation(identifier)
    assert row is not None and row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert row.payload_version == 2 and json.loads(row.payload)["version"] == 1
    before = owned.calls.copy()
    with pytest.raises(StateError):
        owned.stage.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        (owned.stage.configure if effect == "configuration" else owned.stage.resize)(Deadline.after(5))
    assert owned.calls == before
    assert owned.calls.count("configure") == 1 and owned.calls.count("resize") == int(effect == "resize")


def test_takeover_fences_observation_and_bookkeeping(owned: Owned) -> None:
    owned.stage.configure(Deadline.after(5))
    owned.stage.resize(Deadline.after(5))
    OperationOwner.recover(owned.repository, owned.owner.ownership, "b" * 32)
    before = owned.calls.copy()
    for method in (owned.stage.reconcile, owned.stage.wait):
        with pytest.raises(StateError):
            method(Deadline.after(5))
    assert owned.calls == before


@pytest.mark.parametrize("deadline", [Deadline(None), Deadline(99)])
def test_finite_fresh_deadline_precedes_provider_reads(owned: Owned, deadline: Deadline) -> None:
    with pytest.raises((TimeoutError, ValidationError)):
        owned.stage.configure(deadline)
    assert owned.calls == []


@pytest.mark.parametrize(
    "raw", [None, "none,size=40G", "volume,size=1G,size=40G", "file=a,file=b,size=40G", "a,size=0.1"]
)
def test_ambiguous_or_unprovable_disk_is_not_size_evidence(raw: object) -> None:
    with pytest.raises(StateError):
        _disk({"scsi0": raw})


def test_fractional_byte_disk_size_cannot_round_into_exact_target_evidence() -> None:
    with pytest.raises(StateError):
        _disk({"scsi0": "volume,size=39.999999999999999999999999999G"})


def test_long_fraction_disk_size_preserves_exact_integer_bytes() -> None:
    observed = _disk({"scsi0": "volume,size=39.999999999068677425384521484375G"})
    assert observed.size_bytes == 40 * 1024**3 - 1
