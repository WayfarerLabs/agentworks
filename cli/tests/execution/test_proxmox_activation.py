"""Exact SQLite custody and deterministic activation wire boundaries."""

from __future__ import annotations

import json
import threading
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._proxmox_activation import (
    ActivationPayload,
    ProxmoxActivation,
    TaskOutcome,
    TaskPhase,
    decode_activation_payload,
    decode_receipt,
    encode_activation_payload,
    observe_task,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import ProxmoxConnection, _ProxmoxWire
from agentworks.operations import OperationOwner

IDENTITY = "usér@pve!worker"
UPID = f"UPID:node-1:000000AB:100000001:000000CD:qmstart:123:{IDENTITY}:"
CONNECTION = ProxmoxConnection("https://pve.example:8006", "node-1", 123, IDENTITY, "secret-sentinel")


def status(upid: str = UPID, phase: str = "stopped", exitstatus: object = "OK") -> dict[str, object]:
    receipt = decode_receipt(upid, payload(upid=upid))
    return {
        "upid": upid,
        "node": receipt.node,
        "pid": receipt.pid,
        "pstart": receipt.pstart,
        "starttime": receipt.starttime,
        "type": receipt.task_type,
        "id": receipt.vmid,
        "user": "usér@pve",
        "tokenid": "worker",
        "status": phase,
        "exitstatus": exitstatus,
    }


def payload(**kwargs) -> ActivationPayload:
    return replace(ActivationPayload(CONNECTION.api_url, "node-1", 123, IDENTITY, "a" * 64), **kwargs)


@pytest.fixture
def owned(tmp_path: Path, monkeypatch):
    local_delivery = LocalDeliveryCustody()
    with closing(Database(tmp_path / "activation.db")) as database:
        repository = database.operations
        owner = OperationOwner.acquire(repository, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
        adapter = ProxmoxActivation(
            owner, "vm-one", CONNECTION, ProviderLocator("selected-provider-locator"), custody=local_delivery
        )
        calls = []

        def start(wire, *, timeout, custody):
            assert custody is local_delivery
            assert 0 < timeout <= 5
            row = next(row for row in owner.list_lifecycle_obligations() if row.obligation_id == adapter.obligation_id)
            assert row.obligation_id == adapter.obligation_id
            assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
            assert decode_activation_payload(row.payload).upid is None
            calls.append("start")
            return UPID

        def poll(wire, upid, *, timeout, custody):
            assert custody is local_delivery
            assert upid == UPID
            calls.append("poll")
            return status()

        monkeypatch.setattr(_ProxmoxWire, "request_vm_start", start)
        monkeypatch.setattr(_ProxmoxWire, "request_task_status", poll)
        yield repository, owner, adapter, calls


def test_exact_armed_single_post_and_terminal_request_only(owned):
    repository, owner, adapter, calls = owned
    receipt = adapter.start(Deadline.after(5))
    assert receipt.pstart == 0x100000001
    (row,) = owner.list_lifecycle_obligations()
    assert row.payload_revision == 1
    assert decode_activation_payload(row.payload).upid == UPID
    assert b"secret-sentinel" not in row.payload and b"selected-provider-locator" not in row.payload
    owner.stop_admission()
    observation = adapter.observe(Deadline.after(5))
    assert observation.phase is TaskPhase.STOPPED and observation.outcome is TaskOutcome.OK
    assert observation.request_settled
    assert owner.list_lifecycle_obligations()[0].state is LifecycleObligationState.RESOLVED
    adapter.reconcile(Deadline.after(5))
    assert adapter.observe(Deadline.after(5)) == observation
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))
    assert calls == ["start", "poll"]
    assert repository.inspect(owner.ownership.scope) is not None


@pytest.mark.parametrize(
    "upid",
    [
        None,
        "",
        "junk",
        UPID.replace("node-1", "node.1"),
        UPID.replace(":123:", ":124:"),
        UPID.replace("qmstart", "qmstop"),
        UPID.replace("!worker", "!foreign"),
        UPID + "x",
        UPID.replace("usér", "user/name"),
    ],
)
def test_foreign_or_malformed_receipt_never_polls_or_replays(owned, monkeypatch, upid):
    _, owner, adapter, calls = owned

    def start(wire, *, timeout, custody):
        calls.append("start")
        return upid

    monkeypatch.setattr(_ProxmoxWire, "request_vm_start", start)
    with pytest.raises(ValidationError):
        adapter.start(Deadline.after(5))
    with pytest.raises(StateError):
        adapter.observe(Deadline.after(5))
    adapter.reconcile(Deadline.after(5))
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))
    assert calls == ["start"]
    assert owner.list_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", True),
        ("pid", "171"),
        ("pstart", 1),
        ("starttime", 1),
        ("node", "other"),
        ("upid", UPID + "x"),
        ("type", "hastart"),
        ("id", 123),
        ("user", IDENTITY),
        ("tokenid", "foreign"),
        ("status", "unknown"),
    ],
)
def test_task_identity_conflict_is_unknown_and_retains(owned, monkeypatch, field, value):
    _, owner, adapter, _ = owned
    adapter.start(Deadline.after(5))
    response = status()
    response[field] = value
    monkeypatch.setattr(_ProxmoxWire, "request_task_status", lambda *args, **kwargs: response)
    observation = adapter.observe(Deadline.after(5))
    assert observation.phase is TaskPhase.UNKNOWN and not observation.request_settled
    assert owner.list_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize(
    "phase,exitstatus,outcome,count,settled",
    [
        ("running", "OK", TaskOutcome.UNKNOWN, None, False),
        ("stopped", "OK", TaskOutcome.OK, None, True),
        ("stopped", "WARNINGS: 12", TaskOutcome.WARNINGS, 12, True),
        ("stopped", "provider secret error text", TaskOutcome.UNKNOWN, None, False),
        ("stopped", None, TaskOutcome.UNKNOWN, None, False),
        ("stopped", "WARNINGS: x", TaskOutcome.UNKNOWN, None, False),
    ],
)
def test_worker_outcome_is_separate_from_exact_settlement(
    owned, monkeypatch, phase, exitstatus, outcome, count, settled
):
    _, owner, adapter, _ = owned
    adapter.start(Deadline.after(5))
    monkeypatch.setattr(
        _ProxmoxWire, "request_task_status", lambda *args, **kwargs: status(phase=phase, exitstatus=exitstatus)
    )
    observation = adapter.observe(Deadline.after(5))
    assert observation.phase.value == phase and observation.outcome is outcome
    assert observation.warning_count == count and observation.request_settled is settled
    assert "provider secret" not in repr(observation)
    assert b"provider secret" not in owner.list_lifecycle_obligations()[0].payload
    expected = LifecycleObligationState.RESOLVED if settled else LifecycleObligationState.POSSIBLE_EFFECT
    assert owner.list_lifecycle_obligations()[0].state is expected


def test_ha_terminal_is_only_handoff(owned, monkeypatch):
    _, owner, adapter, _ = owned
    upid = UPID.replace("qmstart", "hastart")
    monkeypatch.setattr(_ProxmoxWire, "request_vm_start", lambda *args, **kwargs: upid)
    monkeypatch.setattr(_ProxmoxWire, "request_task_status", lambda *args, **kwargs: status(upid))
    adapter.start(Deadline.after(5))
    observation = adapter.observe(Deadline.after(5))
    assert observation.ha_handoff and observation.phase is TaskPhase.STOPPED
    assert not observation.request_settled
    assert owner.list_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("stage", ["register", "mark", "publish", "resolve"])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit, OSError])
@pytest.mark.windows
def test_lost_ledger_reply_preserves_primary_and_exact_bookkeeping(
    owned, monkeypatch, stage, committed, exception_type
):
    repository, owner, adapter, calls = owned
    names = {
        "register": "register_lifecycle_obligation",
        "mark": "mark_lifecycle_obligation_possible_effect",
        "publish": "publish_lifecycle_obligation_payload",
        "resolve": "resolve_lifecycle_obligation",
    }
    original = getattr(repository, names[stage])
    primary = exception_type("injected interruption")

    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(repository, names[stage], fail)
    with pytest.raises(exception_type) as caught:
        adapter.start(Deadline.after(5))
        adapter.observe(Deadline.after(5))
    assert caught.value is primary
    monkeypatch.setattr(repository, names[stage], original)
    if stage == "register" and not committed:
        with pytest.raises(StateError):
            adapter.reconcile(Deadline.after(5))
    else:
        adapter.reconcile(Deadline.after(5))
        (row,) = owner.list_lifecycle_obligations()
        expected = (
            LifecycleObligationState.RESOLVED
            if stage in {"register", "resolve"}
            else LifecycleObligationState.POSSIBLE_EFFECT
        )
        if stage == "mark" and not committed:
            expected = LifecycleObligationState.REGISTERED
        assert row.state is expected
        if stage in {"publish", "resolve"}:
            assert decode_activation_payload(row.payload).upid == UPID
        if stage == "resolve":
            assert adapter.observe(Deadline.after(5)).request_settled
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))
    assert calls.count("start") == int(stage in {"publish", "resolve"})
    assert calls.count("poll") == int(stage == "resolve")


@pytest.mark.parametrize("stage", ["start", "poll"])
@pytest.mark.parametrize("exception_type", [OSError, TimeoutError, KeyboardInterrupt, SystemExit])
def test_wire_failure_never_claims_rejection_or_replays(owned, monkeypatch, stage, exception_type):
    _, owner, adapter, calls = owned
    primary = exception_type("untrusted provider text")

    def fail(*args, **kwargs):
        calls.append(stage)
        raise primary

    if stage == "poll":
        adapter.start(Deadline.after(5))
    monkeypatch.setattr(_ProxmoxWire, "request_vm_start" if stage == "start" else "request_task_status", fail)
    with pytest.raises(exception_type) as caught:
        (adapter.start if stage == "start" else adapter.observe)(Deadline.after(5))
    assert caught.value is primary
    adapter.reconcile(Deadline.after(5))
    assert owner.list_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT
    assert calls.count("start") == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("vmid", True),
        ("vmid", "123"),
        ("upid", 1),
        ("node", "node.1"),
        ("origin", "http://pve"),
        ("api_identity", "x" * 8193),
        ("locator_sha256", "X" * 64),
        ("extra", "field"),
    ],
)
def test_persisted_payload_boundary_rejects_corruption(field, value):
    encoded = encode_activation_payload(payload())
    data = json.loads(encoded)
    data[field] = value
    with pytest.raises(ValidationError):
        decode_activation_payload(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("ascii"))


def test_receipt_utf8_capacity_and_user_identity():
    assert decode_receipt(UPID, payload()).api_identity == IDENTITY
    with pytest.raises(ValidationError):
        decode_receipt(UPID.replace(IDENTITY, "é" * 128), payload(api_identity="é" * 128))
    user_upid = UPID.replace(IDENTITY, "usér@pve")
    user_receipt = decode_receipt(user_upid, payload(api_identity="usér@pve"))
    data = status()
    data.update(upid=user_upid, user="usér@pve")
    data.pop("tokenid")
    assert observe_task(data, user_receipt).phase is TaskPhase.STOPPED
    data["tokenid"] = "worker"
    assert observe_task(data, user_receipt).phase is TaskPhase.UNKNOWN


def test_stale_takeover_refuses_read_and_publication(owned):
    repository, owner, adapter, calls = owned
    adapter.start(Deadline.after(5))
    OperationOwner.recover(repository, owner.ownership, "b" * 32)
    with pytest.raises(StateError):
        adapter.observe(Deadline.after(5))
    with pytest.raises(StateError):
        adapter.reconcile(Deadline.after(5))
    assert calls == ["start"]


def test_conflicting_revision_refuses_poll(owned):
    _, owner, adapter, calls = owned
    adapter.start(Deadline.after(5))
    assert adapter.obligation is not None
    adapter.obligation.publish_payload(
        expected_revision=1,
        payload_version=1,
        payload=encode_activation_payload(replace(adapter.payload, upid=UPID.replace("000000AB", "000000AC"))),
    )
    with pytest.raises(StateError):
        adapter.observe(Deadline.after(5))
    assert calls == ["start"]


def test_deadline_before_start_and_late_receipt_retains(owned, monkeypatch):
    _, owner, adapter, calls = owned
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    with pytest.raises(TimeoutError):
        adapter.start(Deadline.after(0))
    assert owner.list_lifecycle_obligations() == ()

    def late(*args, **kwargs):
        calls.append("start")
        clock[0] += 0.03
        return UPID

    monkeypatch.setattr(_ProxmoxWire, "request_vm_start", late)
    with pytest.raises(TimeoutError):
        adapter.start(Deadline.after(0.02))
    assert adapter.payload.upid == UPID
    adapter.reconcile(Deadline.after(5))
    assert decode_activation_payload(owner.list_lifecycle_obligations()[0].payload).upid == UPID
    assert calls == ["start"]


@pytest.mark.windows
def test_concurrent_start_sends_once(owned, monkeypatch):
    _, _, adapter, calls = owned
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        calls.append("start")
        entered.set()
        assert release.wait(2)
        return UPID

    monkeypatch.setattr(_ProxmoxWire, "request_vm_start", blocked)
    errors = []

    def invoke():
        try:
            adapter.start(Deadline.after(5))
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=invoke)
    first.start()
    assert entered.wait(2)
    second = threading.Thread(target=invoke)
    second.start()
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], StateError)
    assert calls == ["start"]


@pytest.mark.parametrize("stage", ["register", "mark"])
def test_deadline_after_ledger_commit_never_posts(owned, monkeypatch, stage):
    repository, owner, adapter, calls = owned
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    name = "register_lifecycle_obligation" if stage == "register" else "mark_lifecycle_obligation_possible_effect"
    original = getattr(repository, name)

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 0.03
        return result

    monkeypatch.setattr(repository, name, late)
    with pytest.raises(TimeoutError):
        adapter.start(Deadline.after(0.02))
    monkeypatch.setattr(repository, name, original)
    adapter.reconcile(Deadline.after(5))
    expected = LifecycleObligationState.RESOLVED if stage == "register" else LifecycleObligationState.POSSIBLE_EFFECT
    assert owner.list_lifecycle_obligations()[0].state is expected
    assert calls == []


def test_late_task_status_retains_and_fresh_observation_can_settle(owned, monkeypatch):
    _, owner, adapter, calls = owned
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    adapter.start(Deadline.after(5))
    with pytest.raises(TimeoutError):
        adapter.observe(Deadline.after(0))
    assert calls == ["start"]

    def late(*args, **kwargs):
        calls.append("late-poll")
        clock[0] += 0.03
        return status()

    monkeypatch.setattr(_ProxmoxWire, "request_task_status", late)
    with pytest.raises(TimeoutError):
        adapter.observe(Deadline.after(0.02))
    adapter.reconcile(Deadline.after(5))
    assert owner.list_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT
    monkeypatch.setattr(_ProxmoxWire, "request_task_status", lambda *args, **kwargs: status())
    assert adapter.observe(Deadline.after(5)).request_settled
    assert calls == ["start", "late-poll"]


def test_takeover_during_status_read_cannot_settle_successor(owned, monkeypatch):
    repository, owner, adapter, calls = owned
    adapter.start(Deadline.after(5))

    def takeover(*args, **kwargs):
        calls.append("poll")
        OperationOwner.recover(repository, owner.ownership, "b" * 32)
        return status()

    monkeypatch.setattr(_ProxmoxWire, "request_task_status", takeover)
    with pytest.raises(StateError):
        adapter.observe(Deadline.after(5))
    successor = repository.inspect(owner.ownership.scope)
    assert successor is not None
    assert (
        repository.list_lifecycle_obligations(successor.ownership)[0].state is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert calls == ["start", "poll"]


def test_unrelated_obligation_cannot_be_resolved_or_used(owned):
    _, owner, adapter, calls = owned
    other = owner.register_lifecycle_obligation("other-effect", payload_version=1, payload=b"other")
    other.mark_possible_effect()
    adapter.start(Deadline.after(5))
    assert adapter.observe(Deadline.after(5)).request_settled
    rows = owner.list_lifecycle_obligations()
    assert (
        next(row for row in rows if row.obligation_id == other.obligation_id).state
        is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert calls == ["start", "poll"]


def test_closed_admission_refuses_start_with_retained_registration_id(owned):
    _, owner, adapter, calls = owned
    retained = adapter.obligation_id
    owner.stop_admission()
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))
    assert adapter.obligation_id == retained and calls == []
    assert owner.list_lifecycle_obligations() == ()
    with pytest.raises(StateError):
        adapter.start(Deadline.after(5))


@pytest.mark.parametrize("data", [b"", b"[]", b"\xff", b"{}", b"x" * 8193])
def test_bad_external_payload_bytes(data):
    with pytest.raises(ValidationError):
        decode_activation_payload(data)


def test_aggregate_payload_bound_includes_json_utf8_escaping():
    with pytest.raises(ValidationError):
        encode_activation_payload(payload(api_identity="é" * 2000))
