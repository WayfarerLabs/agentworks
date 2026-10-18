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
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, PreparedInvocation, Retention
from agentworks.execution.carriers import lima
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.carriers.lima import LimaConnection
from tests.execution.carriers.test_lima import process_result
from tests.vms.test_lima_native_access import install_guest, prepare_native
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
    [
        {"name": "other"},
        {"user": None},
        {"user": ""},
        {"dir": "relative"},
        {"config": {"provision": "not-retained"}},
        {"hasErrors": True},
        {"hasErrors": None},
        {"hasErrors": 0},
    ],
)
def test_projection_external_fields_are_checked_before_activation_or_native_route(local, changed) -> None:
    fake, access = local
    fake.raw = json.dumps(
        {
            "name": "selected",
            "status": "Running",
            "dir": str(fake.directory),
            "vmType": "qemu",
            "hasErrors": False,
            **changed,
        }
    ).encode()
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert access.binding is None and access.activation is None


def test_reported_inspection_errors_refuse_before_start_or_guest(local) -> None:
    fake, access = local
    fake.has_errors = True
    fake.status = "Stopped"
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert access.selected is None and access.activation is None
    assert all(argv[1] == "list" for argv, _, _ in fake.calls)


def test_selected_query_discards_real_process_diagnostics_and_retains_only_projection(local, monkeypatch) -> None:
    fake, access = local
    results = []
    projected = json.dumps(
        {
            "name": "selected",
            "status": "Running",
            "dir": str(fake.directory),
            "vmType": "qemu",
            "hasErrors": False,
        }
    )

    def deliver(argv, *, io, deadline, custody, env):
        result = run_process(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                "import sys;print(sys.argv[1]);print(sys.argv[2],file=sys.stderr)",
                projected,
                "synthetic-private-provider-warning",
            ],
            io=io,
            deadline=deadline,
            custody=custody,
            env=env,
        )
        results.append(result)
        return result

    monkeypatch.setattr(_lima_selection, "run_process", deliver)
    access.observe_power(Deadline.after(5))
    assert len(results) == 1
    assert results[0].stdout.retention is Retention.DELIVERED
    assert results[0].stdout.data == results[0].stderr.data == b""
    assert not hasattr(access.selected, "user")


def finalize_owner(access) -> None:
    repository = access.owner._repository
    access.owner.stop_admission()
    access.owner.seal_lifecycle_obligations()
    access.owner.record_effects_resolved()
    access.owner.close()
    assert repository.inspect(access.owner.ownership.scope) is None


@pytest.mark.parametrize("control_type", [RuntimeError, KeyboardInterrupt])
def test_registration_before_commit_has_exact_recoverable_no_effect_disposition(
    local, monkeypatch, control_type
) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(5))
    control = control_type("original-registration-stop")

    def refuse(*args, **kwargs):
        raise control

    monkeypatch.setattr(OperationRepository, "register_lifecycle_obligation", refuse)
    with pytest.raises(control_type) as caught:
        prepare_native(access, power, Deadline.after(5))
    assert caught.value is control
    assert access.activation.registration_absent is (control_type is RuntimeError)
    assert access.settle(Deadline.after(5))
    assert access.activation.registration_absent
    assert all(argv[1] == "list" for argv, _, _ in fake.calls)
    with pytest.raises(StateError):
        access.activation.start(Deadline.after(5))
    finalize_owner(access)


def test_unreadable_registration_absence_retains_custody_until_fenced_read_recovers(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(5))
    inspect = OperationRepository.inspect_lifecycle_obligation

    def refuse(*args, **kwargs):
        raise RuntimeError

    monkeypatch.setattr(OperationRepository, "register_lifecycle_obligation", refuse)
    monkeypatch.setattr(OperationRepository, "inspect_lifecycle_obligation", refuse)
    with pytest.raises(RuntimeError):
        prepare_native(access, power, Deadline.after(5))
    assert not access.activation.registration_absent
    with pytest.raises(RuntimeError):
        access.settle(Deadline.after(5))
    monkeypatch.setattr(OperationRepository, "inspect_lifecycle_obligation", inspect)
    assert access.settle(Deadline.after(5))
    assert access.activation.registration_absent
    finalize_owner(access)


def test_missing_directory_is_not_a_selected_instance(local) -> None:
    fake, access = local
    (fake.directory / "lima.yaml").unlink()
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
        prepare_native(access, power, Deadline.after(5))
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
    prepare_native(access, power, Deadline.after(5))
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


@pytest.mark.parametrize("driver", ["qemu", "vz"])
def test_availability_stage_never_binds_or_probes_native_guest(local, monkeypatch, driver: str) -> None:
    fake, access = local
    fake.vm_type = driver
    (fake.directory / "lima.yaml").write_text(f"vmType: {driver}\n")
    assert access.independent_availability is None
    power = access.observe_power(Deadline.after(5))
    assert access.independent_availability is None
    monkeypatch.setattr(
        "agentworks.vms._lima_native_access.lima_native_binding",
        lambda *a: pytest.fail("availability constructed a native binding"),
    )
    access.ensure_available(power, Deadline.after(5))
    assert access.binding is None and access.route_check is None and access.guest_check is None
    count = len(fake.calls)
    expected = _IndependentJobAvailability.NO_IDLE_STOP if driver in {"qemu", "vz"} else None
    assert access.independent_availability is expected
    assert len(fake.calls) == count
    with pytest.raises(AttributeError):
        access.independent_availability = None
    fake.user = "changed-native-user"
    fake.vm_type = "changed-native-driver"
    with pytest.raises(StateError):
        access.observe_locator(Deadline.after(5))
    assert access.independent_availability is None
    assert access.settle(Deadline.after(5))
    assert access.independent_availability is None
    assert all(argv[1] == "list" for argv, _, _ in fake.calls)


@pytest.mark.parametrize("fresh", ["external-driver", "wsl2", "vz"])
def test_availability_refuses_provider_driver_drift(local, fresh) -> None:
    fake, access = local
    power = access.observe_power(Deadline.after(5))
    fake.vm_type = fresh
    with pytest.raises(StateError):
        access.ensure_available(power, Deadline.after(5))
    assert access.independent_availability is None
    assert access.binding is None


@pytest.mark.parametrize("failure", ["power", "replacement", "projection"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_failed_or_lost_availability_never_exposes_original_driver_fact(local, failure: str, confirmed: bool) -> None:
    fake, access = local
    power = access.observe_power(Deadline.after(5))
    if confirmed:
        access.ensure_available(power, Deadline.after(5))
        assert access.independent_availability is _IndependentJobAvailability.NO_IDLE_STOP
    if failure == "power":
        fake.status = "Stopped"
    elif failure == "replacement":
        fake.directory.rename(fake.home / "original")
        fake.directory.mkdir()
    else:
        fake.raw = b"invalid projection"
    if confirmed:
        if failure == "power":
            access.observe_locator(Deadline.after(5))
        else:
            with pytest.raises(StateError):
                access.observe_locator(Deadline.after(5))
    else:
        with pytest.raises(StateError):
            access.ensure_available(power, Deadline.after(5))
    assert access.independent_availability is None
    assert access.binding is None


def test_stopped_activation_can_publish_availability_fact_without_native_route(local) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(5))
    assert access.independent_availability is None
    access.ensure_available(power, Deadline.after(5))
    assert access.independent_availability is _IndependentJobAvailability.NO_IDLE_STOP
    assert access.activation.running_observed and access.binding is None


def test_availability_fact_uses_final_running_observation(local, monkeypatch) -> None:
    fake, access = local
    power = access.observe_power(Deadline.after(5))
    run = fake.run

    def change_after_revalidation(*args, **kwargs):
        result = run(*args, **kwargs)
        fake.vm_type = "external-driver"
        return result

    monkeypatch.setattr(_lima_selection, "run_process", change_after_revalidation)
    with pytest.raises(StateError):
        access.ensure_available(power, Deadline.after(5))
    assert access.selected.vm_type == "qemu"
    assert access.independent_availability is None and access.binding is None


def test_stopped_start_does_not_resolve_native_user_during_availability(local) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(5))
    fake.user = "changed-native-user"
    access.ensure_available(power, Deadline.after(5))
    assert access.activation is not None and access.activation.running_observed
    assert not access.owner.list_pending_lifecycle_obligations()
    assert all(".config.user.name" not in argv for argv, _, _ in fake.calls)


@pytest.mark.parametrize("driver", ["qemu", "vz"])
def test_independent_availability_requires_a_source_proved_driver_lifetime(local, driver: str) -> None:
    fake, access = local
    fake.vm_type = driver
    (fake.directory / "lima.yaml").write_text(f"vmType: {driver}\n")
    binding = access.platform.resolve_native_execution_binding(access.vm, access.ctx, deadline=Deadline.after(5))
    expected = _IndependentJobAvailability.NO_IDLE_STOP if driver in {"qemu", "vz"} else None
    assert binding._independent_availability is expected


def test_native_static_configuration_refuses_before_provider_observation(local) -> None:
    fake, access = local
    access.vm.platform_metadata["instance_name"] = "invalid/name"
    with pytest.raises(StateError):
        access.native_configuration_check()
    assert not fake.calls


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
        prepare_native(access, power, Deadline.after(5))
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
        prepare_native(access, power, Deadline.after(5))
    assert access.activation.no_dispatch and not access.activation.running_observed
    assert access.settle(Deadline.after(5))
    assert not access.owner.list_pending_lifecycle_obligations()


def test_foreign_activation_revision_cannot_be_resolved(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(5))
    monkeypatch.setattr(_lima_activation, "run_process", lambda *a, **kw: process_result(exit_status=1))
    with pytest.raises(StateError):
        prepare_native(access, power, Deadline.after(5))
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
    workflow = prepare_native(access, power, Deadline.after(5))
    assert workflow.preparation is not None and workflow.preparation.target is not None
    original = workflow.preparation.target
    assert access.settle(Deadline.after(5))
    changed = replace(guest, **{field: "00000000-0000-4000-8000-000000000002" if field == "boot_id" else 1235})
    install_guest(monkeypatch, payload=encode_vm_guest_identity_success("0" * 32, changed))
    fresh = access.platform.build_native_execution_access(
        access.vm, access.ctx, owner=access.owner, custody=LocalDeliveryCustody()
    )
    try:
        power = fresh.observe_power(Deadline.after(5))
        fresh_workflow = prepare_native(fresh, power, Deadline.after(5))
        assert fresh_workflow.preparation is not None and fresh_workflow.preparation.target is not None
        assert fresh_workflow.preparation.target.incarnation == original.incarnation
        assert fresh_workflow.preparation.target.boot_id != original.boot_id
    finally:
        assert fresh.settle(Deadline.after(5))
