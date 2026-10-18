"""Local Lima snapshot, effect fencing, and external payload boundaries."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.db.operations import OperationRepository
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _lima_activation, _lima_selection
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._lima_activation import (
    ActivationPayload,
    decode_activation_payload,
    encode_activation_payload,
)
from agentworks.execution._lima_selection import LimaDirectoryCustody
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, encode_vm_guest_identity_success
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, PreparedInvocation
from agentworks.execution.carriers import lima
from agentworks.execution.carriers.lima import LimaConnection
from tests.execution.carriers.test_lima import process_result
from tests.vms.test_lima_native_access import install_guest
from tests.vms.test_lima_native_access import local as local

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic local directory and guest proof")


@pytest.mark.parametrize(
    "data", [b"", b"{}", b"[]", b"\xff", b"x" * 257, b'{"version":true,"locator":"lima:v1:' + b"a" * 64 + b'"}']
)
def test_corrupt_activation_payload_refuses(data: bytes) -> None:
    with pytest.raises(ValidationError):
        decode_activation_payload(data)


def test_activation_payload_roundtrip_has_one_versioned_opaque_locator() -> None:
    payload = ActivationPayload("lima:v1:" + "a" * 64)
    data = encode_activation_payload(payload)
    assert decode_activation_payload(data) == payload
    foreign_version = json.loads(data)
    foreign_version["version"] = 2
    with pytest.raises(ValidationError):
        decode_activation_payload(json.dumps(foreign_version).encode())
    with pytest.raises(ValidationError):
        decode_activation_payload(data + b" ")


@pytest.mark.parametrize(
    "changed",
    [{"name": "other"}, {"user": None}, {"user": ""}, {"dir": "relative"}, {"config": {"provision": "not-retained"}}],
)
def test_projection_external_fields_are_checked_before_directory_or_guest(local, changed) -> None:
    fake, access = local
    fake.raw = json.dumps(
        {
            "name": "selected",
            "status": "Running",
            "dir": str(fake.directory),
            "vmType": "qemu",
            "user": "limauser",
            **changed,
        }
    ).encode()
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert access.route.directory is None


def test_missing_directory_is_not_a_selected_instance(local) -> None:
    fake, access = local
    fake.directory.rmdir()
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert access.selected is None


def test_directory_replaced_during_query_refuses_after_observation(local, monkeypatch) -> None:
    fake, access = local
    power = access.observe_power(Deadline.after(5))
    run = fake.run

    def replace_directory(*args, **kwargs):
        result = run(*args, **kwargs)
        fake.directory.rename(fake.home / "retained-original")
        fake.directory.mkdir()
        return result

    monkeypatch.setattr(_lima_selection, "run_process", replace_directory)
    with pytest.raises(StateError):
        access.prepare(power, Deadline.after(5))
    assert access.activation is None


def test_default_home_is_resolved_once_only_during_owned_selection(local, monkeypatch) -> None:
    fake, access = local
    calls = []
    monkeypatch.delenv("LIMA_HOME")
    monkeypatch.setattr(Path, "home", lambda: calls.append(1) or fake.home.parent)
    fake.home.rename(fake.home.parent / ".lima")
    fake.home = fake.home.parent / ".lima"
    assert calls == []
    power = access.observe_power(Deadline.after(5))
    install_guest(monkeypatch)
    monkeypatch.setenv("LIMA_HOME", "/changed-after-owned-selection")
    access.prepare(power, Deadline.after(5))
    assert calls == [1]


def test_direct_resolver_closes_transient_directory_descriptor(local, monkeypatch) -> None:
    _, access = local
    opened = []
    original = LimaDirectoryCustody.open

    def observe(cls, path):
        held = original(path)
        opened.append(held)
        return held

    monkeypatch.setattr(LimaDirectoryCustody, "open", classmethod(observe))
    binding = access.platform.resolve_native_execution_binding(access.vm, access.ctx, deadline=Deadline.after(5))
    assert len(opened) == 1 and opened[0].descriptor is None
    assert binding.carrier.connection.selected_directory == opened[0].identity


def test_guest_carrier_pins_home_and_checks_directory_after_delivery(local, monkeypatch) -> None:
    fake, access = local
    binding = access.platform.resolve_native_execution_binding(access.vm, access.ctx, deadline=Deadline.after(5))
    seen = []

    def deliver(argv, *, io, deadline, custody, env):
        seen.append((argv, env["LIMA_HOME"]))
        fake.directory.rename(fake.home / "retained-original")
        fake.directory.mkdir()
        return process_result()

    monkeypatch.setattr(lima, "run_process", deliver)
    monkeypatch.setenv("LIMA_HOME", "/changed-after-binding")
    with pytest.raises(StateError):
        binding.carrier.execute(
            PreparedInvocation(("/noop",)), io=CarrierIO(), deadline=Deadline.after(5), custody=LocalDeliveryCustody()
        )
    assert len(seen) == 1 and seen[0][1] == str(fake.home)


def test_malformed_optional_directory_connection_refuses_at_boundary() -> None:
    with pytest.raises(ValidationError):
        LimaConnection("selected", "/limactl", "/lima", object())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LimaConnection("selected", "/limactl", "relative")


def test_start_mark_lost_reply_never_replays_or_invents_completion(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(5))
    original = OperationRepository.mark_lifecycle_obligation_possible_effect

    def lost(self, ownership, obligation_id):
        original(self, ownership, obligation_id)
        raise KeyboardInterrupt

    monkeypatch.setattr(OperationRepository, "mark_lifecycle_obligation_possible_effect", lost)
    with pytest.raises(KeyboardInterrupt):
        access.prepare(power, Deadline.after(5))
    assert access.activation is not None and access.activation.mark_began
    assert not access.settle(Deadline.after(5))
    assert all(argv[1] == "list" for argv, _, _ in fake.calls)


def test_proved_local_dispatch_failure_resolves_without_claiming_running(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(5))
    monkeypatch.setattr(
        _lima_activation,
        "run_process",
        lambda *a, **kw: process_result(started=False, exit_status=None, local_status=None, failure=Failure.DISPATCH),
    )
    with pytest.raises(StateError):
        access.prepare(power, Deadline.after(5))
    assert access.activation.no_dispatch and not access.activation.running_observed
    assert access.settle(Deadline.after(5))
    assert not access.owner.list_pending_lifecycle_obligations()


def test_foreign_activation_revision_cannot_be_resolved(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(5))
    monkeypatch.setattr(_lima_activation, "run_process", lambda *a, **kw: process_result(exit_status=1))
    with pytest.raises(StateError):
        access.prepare(power, Deadline.after(5))
    row = access.owner.inspect_lifecycle_obligation(access.activation.obligation_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    inspect = access.owner.inspect_lifecycle_obligation
    monkeypatch.setattr(
        access.owner, "inspect_lifecycle_obligation", lambda key: replace(inspect(key), payload_revision=1)
    )
    with pytest.raises(StateError):
        access.settle(Deadline.after(5))
    assert access.route.directory.descriptor is None


@pytest.mark.parametrize("field", ["boot_id", "init_start_ticks"])
def test_prepared_target_retains_full_guest_boot_fence(local, monkeypatch, field: str) -> None:
    _, access = local
    guest = VMGuestIdentity(access.vm.instance_marker, "00000000-0000-4000-8000-000000000001", 1234)
    install_guest(monkeypatch, payload=encode_vm_guest_identity_success("0" * 32, guest))
    power = access.observe_power(Deadline.after(5))
    access.prepare(power, Deadline.after(5))
    original = access.preparation.target
    assert access.settle(Deadline.after(5))
    changed = replace(guest, **{field: "00000000-0000-4000-8000-000000000002" if field == "boot_id" else 1235})
    install_guest(monkeypatch, payload=encode_vm_guest_identity_success("0" * 32, changed))
    fresh = access.platform.build_native_execution_access(
        access.vm, access.ctx, owner=access.owner, custody=LocalDeliveryCustody()
    )
    try:
        power = fresh.observe_power(Deadline.after(5))
        fresh.prepare(power, Deadline.after(5))
        assert fresh.preparation.target.incarnation == original.incarnation
        assert fresh.preparation.target.boot_id != original.boot_id
    finally:
        assert fresh.settle(Deadline.after(5))
