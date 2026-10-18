"""Host-only Lima WSL selection and authorized first-import transitions."""

from __future__ import annotations

import json
import os
import platform
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import LifecycleObligationState, VMStatus
from agentworks.errors import StateError
from agentworks.execution import _lima_activation, _lima_configuration, _lima_selection, _lima_wsl_hold
from agentworks.execution._lima_configuration import LimaConfiguration, decode_version, read_configuration
from agentworks.execution._lima_selection import LimaDirectoryCustody, LimaLocalSelection
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution._wsl2_platform_hold import decode_hold_payload, encode_hold_payload
from agentworks.execution._wsl2_registration import WSLRegistration, decode_registration, execution_power_from_listing
from agentworks.execution.carrier import Deadline, SinkOutput
from agentworks.execution.target import ExecutionRoute
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.vms._native_operation import _vm_operation
from tests.execution.carriers.test_lima import process_result
from tests.execution.test_wsl2_platform_hold import BOOT, OPEN, SETTLED, FakeNative, FakeObserver
from tests.vms.test_lima_native_access import local as local
from tests.vms.test_target_preparation import _MARKER

pytestmark = pytest.mark.windows


def _wsl_host_path(path: Path) -> Path:
    value = str(path)
    if os.name == "nt" or not value.startswith("/") or value.startswith("//"):
        return path
    return Path(f"/{value}")


@pytest.fixture
def wsl(local, monkeypatch):
    fake, access = local
    fake.home = _wsl_host_path(fake.home)
    monkeypatch.setenv("LIMA_HOME", str(fake.home))
    fake.vm_type = "wsl2"
    (fake.directory / "lima.yaml").write_text("vmType: wsl2\n")
    route = LimaLocalSelection("local", "selected", "box", str(fake.home), "/literal/limactl")
    route.directory = LimaDirectoryCustody.open(str(fake.directory))
    access.route = route
    monkeypatch.setattr(_lima_selection, "sys", SimpleNamespace(platform="win32"))
    state = SimpleNamespace(
        registration=WSLRegistration(
            "2f1b1ed8-a629-4fc5-93ef-504e9a780a37",
            "S-1-5-21-1001",
            "613ea898-8445-4e6e-82c7-f6e9ae8d7235",
            "lima-selected",
            str(fake.directory),
        ),
        listing=b"NAME STATE VERSION\n* lima-selected Running 2\n",
        calls=[],
        owned=True,
        native=FakeNative([]),
        observer=FakeObserver([]),
    )

    def registration(name, vm_name, deadline, custody):
        assert name == "lima-selected" and vm_name == "box"
        assert not state.owned or custody is access.custody
        state.calls.append(("registry", deadline))
        return state.registration

    original = fake.run

    def run(argv, *, io, deadline, custody, env):
        if argv[0] != "wsl":
            return original(argv, io=io, deadline=deadline, custody=custody, env=env)
        assert argv == ["wsl", "--list", "--verbose"]
        assert not state.owned or custody is access.custody
        state.calls.append(("power", deadline))
        assert isinstance(io.output, SinkOutput)
        io.output.stdout.try_write(memoryview(state.listing))
        return process_result()

    monkeypatch.setattr(_lima_selection, "observe_registration", registration)
    monkeypatch.setattr(_lima_selection, "run_process", run)
    monkeypatch.setattr(_lima_wsl_hold, "WindowsWSL2HostClient", lambda: state.native)
    monkeypatch.setattr(_lima_wsl_hold, "WSL2GuestObserver", lambda connection: state.observer)
    state.run = run
    state.observe_registration = registration
    return fake, access, state


@pytest.mark.parametrize("status", ["Running", "Stopped"])
def test_wsl_power_and_locator_never_call_lima_inspection(wsl, status) -> None:
    fake, access, state = wsl
    state.listing = f"NAME STATE VERSION\nlima-selected {status} 2\n".encode("utf-16-le")
    deadline = Deadline.after(5)
    assert access.observe_power(deadline) is VMStatus(status.lower())
    assert access.observe_locator(deadline) == access.selected.locator
    assert fake.calls == [] and all(item[1] is deadline for item in state.calls)
    assert access.independent_availability is None and access.binding is None


@pytest.mark.parametrize(
    "power", [b"lima-selected Starting 2", b"lima-selected Running 1", b"lima-selected Aktiv 2", b"\xff"]
)
def test_unconfirmed_wsl_power_never_enters_lima_or_guest(wsl, power) -> None:
    fake, access, state = wsl
    state.listing = power
    assert access.observe_power(Deadline.after(5)) is VMStatus.UNKNOWN
    assert fake.calls == [] and access.activation is None


def test_wrong_lima_home_registration_refuses_before_power_or_lima(wsl) -> None:
    fake, access, state = wsl
    state.registration = replace(state.registration, base_path="C:\\another-home\\selected")
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert [item[0] for item in state.calls] == ["registry"] and fake.calls == []


@pytest.mark.parametrize("wrong_base_path", [False, True])
def test_implicit_driver_rechecks_version_before_each_provider_observation(wsl, monkeypatch, wrong_base_path) -> None:
    fake, access, state = wsl
    fake.vm_type = "qemu"
    (fake.directory / "lima.yaml").write_text("{}\n")
    monkeypatch.setattr(_lima_configuration, "sys", SimpleNamespace(platform="win32"))
    versions = iter([b"limactl version 2.2.1\n", b"limactl version 2.1.0\n"])

    def run(argv, *, io, deadline, custody, env):
        if argv[0] != "wsl":
            state.calls.append((argv[1], deadline))
        if argv[1] == "--version":
            assert argv == ["/literal/limactl", "--version"]
            io.output.stdout.try_write(memoryview(next(versions)))
            return process_result()
        return state.run(argv, io=io, deadline=deadline, custody=custody, env=env)

    monkeypatch.setattr(_lima_selection, "run_process", run)
    assert access.observe_power(Deadline.after(5)) is VMStatus.RUNNING
    assert [item[0] for item in state.calls] == ["--version", "list"]
    state.calls.clear()
    if wrong_base_path:
        state.registration = replace(state.registration, base_path="C:\\another-home\\selected")
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert [item[0] for item in state.calls] == ["--version", "registry", *([] if wrong_base_path else ["power"])]
    assert [argv[1] for argv, _, _ in fake.calls] == ["list"]
    assert access.activation is None and access.binding is None


def test_unrelated_registration_appearing_before_start_is_never_adopted(wsl) -> None:
    fake, access, state = wsl
    exact = state.registration
    state.registration = replace(exact, guid=None, base_path=None)
    power = access.observe_power(Deadline.after(5))
    state.registration = exact
    state.listing = b"lima-selected Stopped 2"
    with pytest.raises(StateError):
        access.ensure_available(power, Deadline.after(5))
    assert fake.calls == [] and access.activation is None


@pytest.mark.parametrize("absent", [False, True])
def test_core_operator_stopped_gate_precedes_lima_list_and_start(wsl, absent, monkeypatch) -> None:
    fake, access, state = wsl
    if absent:
        state.registration = replace(state.registration, guid=None, base_path=None)
    state.listing = b"lima-selected Stopped 2"
    database = access.owner._repository._database
    # Release the fixture owner so the real core acquires its own exact claim.
    access.owner.close()
    database.set_operator_stopped("box", True)

    def build(vm, ctx, *, owner, custody):
        access.vm, access.ctx, access.owner, access.custody = vm, ctx, owner, custody
        return access

    monkeypatch.setattr(access.platform, "build_native_execution_access", build)
    with (
        pytest.raises(StateError),
        _vm_operation(
            database,
            "box",
            access.platform,
            RunContext(),
            route=ExecutionRoute.NATIVE,
            deadline=Deadline.after(5),
            trusted_root=PurePosixPath("/proof"),
        ),
    ):
        pytest.fail("operator-stopped selection admitted a body")
    assert fake.calls == [] and access.activation is None and access.binding is None
    assert access.wsl_hold is None and state.native.events == []


@pytest.mark.parametrize("absent", [False, True])
def test_authorized_wsl_start_acquires_exact_registration_then_resolves_user(wsl, absent, monkeypatch) -> None:
    fake, access, state = wsl
    exact = state.registration
    if absent:
        state.registration = replace(exact, guid=None, base_path=None)
    state.listing = b"lima-selected Stopped 2"
    deadline = Deadline.after(5)
    power = access.observe_power(deadline)
    original = access.selected
    start = fake.run

    def activate(*args, **kwargs):
        result = start(*args, **kwargs)
        state.registration = exact
        state.listing = b"lima-selected Running 2"
        return result

    monkeypatch.setattr(_lima_activation, "run_process", activate)
    spawn = state.native.spawn_owned

    def hold_spawn(argv, hold_deadline):
        assert hold_deadline is deadline and access.wsl_hold is not None
        assert access.wsl_hold.selected.registration == exact
        assert access.activation.running_observed
        assert access.activation.obligation.state is LifecycleObligationState.RESOLVED
        spawn(argv, hold_deadline)

    monkeypatch.setattr(state.native, "spawn_owned", hold_spawn)
    locator = access.ensure_available(power, deadline)
    assert access.selected is original and access.activation.running_observed
    assert access.activation.obligation.state is LifecycleObligationState.RESOLVED
    assert access.wsl_hold is not None
    access.wsl_hold.require_ready()
    assert state.native.argv[:5] == ("wsl", "--distribution", exact.name, "--user", "root")
    assert [argv[1] for argv, _, _ in fake.calls] == ["start"]
    assert (locator != original.locator) is absent
    assert access.observe_locator(deadline) == locator
    assert access.observe_power(deadline) is VMStatus.RUNNING
    binding = access.prepare_native_route(deadline)
    assert binding.delivery_account == fake.user and binding._independent_availability is None
    assert sum(argv[1] == "start" for argv, _, _ in fake.calls) == 1
    assert sum(argv[1] == "list" for argv, _, _ in fake.calls) == 1
    count = len(fake.calls)
    access.route_check(deadline)
    assert len(fake.calls) == count
    state.registration = replace(exact, guid="b2979319-35a4-4dd6-9bbb-e4a060d0b908")
    with pytest.raises(StateError):
        access.route_check(deadline)
    assert len(fake.calls) == count


@pytest.mark.parametrize("change", ["sid", "machine", "base_path", "lost_ack"])
def test_first_import_refuses_wrong_registration_or_lost_ack_and_retains_debt(wsl, change, monkeypatch) -> None:
    fake, access, state = wsl
    exact = state.registration
    state.registration = replace(exact, guid=None, base_path=None)
    power = access.observe_power(Deadline.after(5))
    start = fake.run

    def activate(*args, **kwargs):
        result = start(*args, **kwargs)
        state.registration = replace(exact, **{change: "wrong"}) if change != "lost_ack" else exact
        state.listing = b"lima-selected Running 2"
        if change == "lost_ack":
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(_lima_activation, "run_process", activate)
    with pytest.raises(KeyboardInterrupt if change == "lost_ack" else StateError):
        access.ensure_available(power, Deadline.after(5))
    assert access.binding is None and not access.settle(Deadline.after(5))
    assert access.owner.list_pending_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        access.ensure_available(power, Deadline.after(5))
    assert [argv[1] for argv, _, _ in fake.calls] == ["start"]


@pytest.mark.parametrize(
    "relative", ["_config/default.yaml", "_config/override.yaml", "selected/lima.yaml", "selected/lima-version"]
)
def test_native_route_pins_all_lima_user_input_files(wsl, relative) -> None:
    fake, access, state = wsl
    power = access.observe_power(Deadline.after(5))
    access.ensure_available(power, Deadline.after(5))
    access.prepare_native_route(Deadline.after(5))
    path = fake.home / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text("vmType: wsl2\nuser:\n  name: changed\n")
    count = len(fake.calls)
    with pytest.raises(StateError):
        access.route_check(Deadline.after(5))
    assert len(fake.calls) == count


def test_hold_ready_precedes_native_user_lookup(wsl, monkeypatch) -> None:
    fake, access, state = wsl
    original = fake.run

    def run(argv, **kwargs):
        if argv[0] == "wsl":
            return state.run(argv, **kwargs)
        assert access.wsl_hold is not None
        access.wsl_hold.require_ready()
        assert state.native.events.count("dispatch") == 1
        return original(argv, **kwargs)

    monkeypatch.setattr(_lima_selection, "run_process", run)
    deadline = Deadline.after(5)
    power = access.observe_power(deadline)
    assert access.wsl_hold is None and state.native.events == []
    access.ensure_available(power, deadline)
    assert fake.calls == [] and access.independent_availability is None
    access.prepare_native_route(deadline)
    assert len(fake.calls) == 1
    assert access.settle(Deadline.after(5))


def test_missing_durable_ready_refuses_availability_and_native_lookup(wsl, monkeypatch) -> None:
    fake, access, state = wsl
    power = access.observe_power(Deadline.after(5))
    with monkeypatch.context() as patch:
        patch.setattr(LifecycleObligation, "publish_payload", lambda self, **kwargs: self._persisted_obligation)
        with pytest.raises(StateError):
            access.ensure_available(power, Deadline.after(5))
    assert access.wsl_hold is not None and access.binding is None and fake.calls == []
    with pytest.raises(StateError):
        access.prepare_native_route(Deadline.after(5))
    assert state.native.events.count("dispatch") == 1
    assert access.settle(Deadline.after(5))


def test_changed_durable_ready_revision_blocks_user_lookup(wsl) -> None:
    fake, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    obligation = access.wsl_hold.hold.obligation
    row = obligation._persisted_obligation
    access.owner._repository.publish_lifecycle_obligation_payload(
        access.owner.ownership,
        row.obligation_id,
        expected_revision=row.payload_revision,
        payload_version=row.payload_version,
        payload=encode_hold_payload(replace(decode_hold_payload(row.payload), nonce="f" * 32)),
    )
    with pytest.raises(StateError):
        access.prepare_native_route(Deadline.after(5))
    assert fake.calls == [] and not access.settle(Deadline.after(5))
    assert access.route.directory.descriptor is not None
    assert state.native.events.count("dispatch") == 1 and "eof" not in state.native.events


@pytest.mark.parametrize("control", [OSError, KeyboardInterrupt])
def test_lost_ready_ack_retains_same_hold_for_cleanup_without_relaunch(wsl, monkeypatch, control) -> None:
    fake, access, state = wsl
    power = access.observe_power(Deadline.after(5))
    original = LifecycleObligation.publish_payload

    def publish(self, **kwargs):
        original(self, **kwargs)
        raise control("lost READY acknowledgment")

    with monkeypatch.context() as patch:
        patch.setattr(LifecycleObligation, "publish_payload", publish)
        with pytest.raises(control):
            access.ensure_available(power, Deadline.after(5))
    retained = access.wsl_hold
    assert retained is not None and fake.calls == [] and access.binding is None
    assert retained.hold.evidence.identity is not None
    with pytest.raises(StateError):
        access.ensure_available(power, Deadline.after(5))
    assert access.settle(Deadline.after(5)) and access.wsl_hold is retained
    assert state.native.events.count("dispatch") == 1
    assert not access.owner.list_pending_lifecycle_obligations()


def test_interrupted_hold_launch_retains_unresolved_owner_and_directory(wsl) -> None:
    fake, access, state = wsl
    state.native.fail_spawn = KeyboardInterrupt()
    power = access.observe_power(Deadline.after(5))
    with pytest.raises(KeyboardInterrupt):
        access.ensure_available(power, Deadline.after(5))
    retained = access.wsl_hold
    assert retained is not None and fake.calls == []
    assert not access.settle(Deadline.after(5))
    assert access.wsl_hold is retained and access.route.directory.descriptor is not None
    assert access.owner.list_pending_lifecycle_obligations()
    assert state.native.events.count("dispatch") == 1


def test_uncertain_registration_prevents_release_success(wsl, monkeypatch) -> None:
    fake, access, state = wsl
    power = access.observe_power(Deadline.after(5))
    original = OperationOwner.register_lifecycle_obligation

    def register(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("lost registration acknowledgment")

    with monkeypatch.context() as patch:
        patch.setattr(OperationOwner, "register_lifecycle_obligation", register)
        with pytest.raises(OSError):
            access.ensure_available(power, Deadline.after(5))
    assert access.wsl_hold.hold.registration_uncertain
    assert not access.settle(Deadline.after(5))
    assert access.route.directory.descriptor is not None and fake.calls == []
    assert "dispatch" not in state.native.events and access.owner.list_pending_lifecycle_obligations()


@pytest.mark.parametrize("field", ["boot", "init"])
def test_common_guest_must_match_held_boot_and_distribution_epoch(wsl, field) -> None:
    _, access, _ = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    guest = VMGuestIdentity(_MARKER, BOOT, 4096)
    access.guest_check(guest)
    changed = replace(
        guest,
        **({"boot_id": "00000000-0000-4000-8000-000000000001"} if field == "boot" else {"init_start_ticks": 4097}),
    )
    with pytest.raises(StateError):
        access.guest_check(changed)
    assert access.settle(Deadline.after(5))


def test_unrelated_lifecycle_debt_blocks_release_until_resolved(wsl) -> None:
    _, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    debt = access.owner.register_lifecycle_obligation("unrelated-work", payload_version=1, payload=b"work")
    debt.mark_possible_effect()
    assert not access.settle(Deadline.after(5))
    assert "eof" not in state.native.events and access.route.directory.descriptor is not None
    debt.resolve()
    assert access.settle(Deadline.after(5))
    assert access.route.directory.settled and state.native.events.count("dispatch") == 1


def test_unknown_absence_keeps_directory_and_retries_only_release(wsl) -> None:
    _, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    retained = access.wsl_hold
    state.observer.presence = GuestAnchorPresence.UNKNOWN
    assert not access.settle(Deadline.after(5))
    assert state.native.local == SETTLED and access.route.directory.descriptor is not None
    assert access.owner.list_pending_lifecycle_obligations()
    state.observer.presence = GuestAnchorPresence.ABSENT_CONFIRMED
    assert access.settle(Deadline.after(5)) and access.wsl_hold is retained
    assert state.native.events.count("dispatch") == state.native.events.count("eof") == 1
    assert state.observer.events == ["observe", "observe"]


def test_unsettled_native_handles_keep_directory_for_explicit_retry(wsl, monkeypatch) -> None:
    _, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    with monkeypatch.context() as patch:
        patch.setattr(state.native, "wait", lambda deadline: None)
        patch.setattr(state.native, "settle", lambda deadline: OPEN)
        assert not access.settle(Deadline.after(5))
    assert access.route.directory.descriptor is not None and state.observer.events == []
    assert access.settle(Deadline.after(5))
    assert state.native.events.count("dispatch") == 1


def test_registration_change_blocks_release_without_closing_directory(wsl) -> None:
    _, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    selected = state.registration
    state.registration = replace(selected, guid="b2979319-35a4-4dd6-9bbb-e4a060d0b908")
    assert not access.settle(Deadline.after(5))
    assert "eof" not in state.native.events and access.route.directory.descriptor is not None
    state.registration = selected
    assert access.settle(Deadline.after(5))


def test_local_delivery_custody_blocks_hold_release(wsl, monkeypatch) -> None:
    _, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    with monkeypatch.context() as patch:
        patch.setattr(access.custody, "close", lambda deadline: False)
        assert not access.settle(Deadline.after(5))
    assert "eof" not in state.native.events and access.route.directory.descriptor is not None
    assert access.settle(Deadline.after(5))


def test_release_interruption_retains_same_hold_and_directory(wsl, monkeypatch) -> None:
    _, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    retained = access.wsl_hold

    def interrupt(deadline):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(state.native, "wait", interrupt)
        with pytest.raises(KeyboardInterrupt):
            access.settle(Deadline.after(5))
    assert access.wsl_hold is retained and access.route.directory.descriptor is not None
    assert access.settle(Deadline.after(5))
    assert state.native.events.count("dispatch") == 1


def test_lost_release_ack_keeps_directory_until_retry(wsl, monkeypatch) -> None:
    _, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    original = LifecycleObligation.resolve

    def resolve(self):
        original(self)
        raise OSError("lost release acknowledgment")

    with monkeypatch.context() as patch:
        patch.setattr(LifecycleObligation, "resolve", resolve)
        assert not access.settle(Deadline.after(5))
    assert access.route.directory.descriptor is not None
    assert access.settle(Deadline.after(5))
    assert state.native.events.count("dispatch") == state.native.events.count("eof") == 1


def test_dead_native_hold_blocks_native_lookup(wsl) -> None:
    fake, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    state.native.local = SETTLED
    with pytest.raises(StateError):
        access.prepare_native_route(Deadline.after(5))
    assert fake.calls == [] and access.settle(Deadline.after(5))


def test_hold_must_still_be_live_after_registration_confirmation(wsl, monkeypatch) -> None:
    fake, access, state = wsl
    power = access.observe_power(Deadline.after(5))
    observe = state.observe_registration

    def registration(*args, **kwargs):
        if "dispatch" in state.native.events:
            state.native.local = SETTLED
        return observe(*args, **kwargs)

    monkeypatch.setattr(_lima_selection, "observe_registration", registration)
    with pytest.raises(StateError):
        access.ensure_available(power, Deadline.after(5))
    assert fake.calls == [] and access.binding is None
    assert access.settle(Deadline.after(5))


def test_successful_release_and_directory_closure_are_idempotent(wsl) -> None:
    _, access, state = wsl
    access.ensure_available(access.observe_power(Deadline.after(5)), Deadline.after(5))
    assert access.settle(Deadline.after(5))
    before = list(state.calls), list(state.native.events), list(state.observer.events)
    assert access.settle(Deadline.after(5))
    assert before == (state.calls, state.native.events, state.observer.events)


def test_direct_stopped_wsl_resolver_does_not_inspect_or_start(wsl, monkeypatch) -> None:
    fake, access, state = wsl
    state.listing = b"lima-selected Stopped 2"
    state.owned = False
    # Use the already-held synthetic directory while exercising the real resolver.
    monkeypatch.setattr(LimaLocalSelection, "resolve", classmethod(lambda cls, *args: access.route))
    with pytest.raises(StateError):
        access.platform.resolve_native_execution_binding(access.vm, access.ctx, deadline=Deadline.after(5))
    assert fake.calls == []


@pytest.mark.parametrize("driver", ["external-driver", "default", ""])
def test_unproved_driver_refuses_before_lima_inspect(local, driver, monkeypatch) -> None:
    fake, access = local
    (fake.directory / "lima.yaml").write_text(f"vmType: {json.dumps(driver)}\n")

    def run(argv, *, io, **kwargs):
        assert argv == ["/literal/limactl", "--version"]
        io.output.stdout.try_write(memoryview(b"limactl version 2.2.1\n"))
        return process_result()

    monkeypatch.setattr(_lima_selection, "run_process", run)
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert fake.calls == []


@pytest.mark.parametrize(
    "version,drivers,arch,expected",
    [
        ((0, 18), ("default", "vz", "wsl2"), None, {"qemu"}),
        ((0, 23), (None, "wsl2", None), None, {"wsl2"}),
        ((1, 2), ("default", "wsl2", "qemu"), None, {"wsl2"}),
        ((2, 0), ("", "qemu", None), "aarch64", {"wsl2"}),
        ((2, 1), ("", "qemu", None), "aarch64", {"qemu"}),
        ((2, 1), (None, None, None), "x86_64", {"wsl2"}),
        ((2, 2), (None, None, None), None, {"qemu"}),
    ],
)
def test_audited_windows_driver_default_history(version, drivers, arch, expected, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_lima_configuration, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    executable = tmp_path / "limactl.exe"
    executable.write_bytes(b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0\x64\x86")
    assert LimaConfiguration((), drivers, arch).classify(version, executable=str(executable)) == expected


@pytest.mark.parametrize(
    "machine,arch,expected",
    [
        (0x8664, "x86_64", {"wsl2"}),
        (0x8664, "aarch64", {"qemu"}),
        (0xAA64, "x86_64", {"qemu"}),
        (0xAA64, "aarch64", {"wsl2"}),
    ],
)
def test_lima_go_arch_comes_from_binary_even_when_host_differs(machine, arch, expected, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_lima_configuration, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "machine", lambda: "ARM64" if machine == 0x8664 else "AMD64")
    executable = tmp_path / "limactl.exe"
    executable.write_bytes(b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0" + machine.to_bytes(2, "little"))
    assert LimaConfiguration((), (None, None, None), arch).classify((2, 1), executable=str(executable)) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"#!wrapper",
        b"MZ" + b"\0" * 62,
        b"MZ" + b"\0" * 58 + (65_531).to_bytes(4, "little"),
        b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0\0\0",
        b"MZ" + b"\0" * 58 + (64).to_bytes(4, "little") + b"PE\0\0\x64",
    ],
)
def test_unknown_windows_binary_arch_never_inspects_guest(local, data, tmp_path, monkeypatch) -> None:
    fake, access = local
    (fake.directory / "lima.yaml").write_text("arch: x86_64\n")
    executable = tmp_path / "limactl.exe"
    executable.write_bytes(data)
    route = LimaLocalSelection("local", "selected", "box", str(fake.home), str(executable))
    route.directory = LimaDirectoryCustody.open(str(fake.directory))
    access.route = route
    monkeypatch.setattr(_lima_configuration, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(_lima_selection, "sys", SimpleNamespace(platform="win32"))
    calls = []

    def run(argv, *, io, **kwargs):
        calls.append(argv)
        assert argv == [str(executable), "--version"]
        io.output.stdout.try_write(memoryview(b"limactl version 2.1.0\n"))
        return process_result()

    monkeypatch.setattr(_lima_selection, "run_process", run)
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert calls == [[str(executable), "--version"]] and fake.calls == []


@pytest.mark.parametrize("version", [(2, 3), (3, 0)])
def test_unaudited_implicit_defaults_refuse(version, monkeypatch) -> None:
    monkeypatch.setattr(_lima_configuration, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(StateError):
        LimaConfiguration((), (None, None, None), None).classify(version)


@pytest.mark.parametrize(
    "raw",
    [b"", b"2.2.1", b"limactl version unknown", b"limactl version 2.2", b"limactl version " + b"9" * 5000 + b".2.1"],
)
def test_unknown_version_response_is_not_driver_evidence(raw) -> None:
    with pytest.raises(StateError):
        decode_version(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"lima-selected Running 1",
        b"lima-selected Running 2\nlima-selected Stopped 2",
        b"lima-selected Running x",
        b"lima-selected running 2",
    ],
)
def test_shared_power_decoder_requires_exact_unambiguous_wsl2_state(raw) -> None:
    assert execution_power_from_listing(raw, "lima-selected") is VMStatus.UNKNOWN


def test_absent_registry_is_distinct_from_corrupt_or_ambiguous_registration() -> None:
    payload = {
        "machine_guid": "2f1b1ed8-a629-4fc5-93ef-504e9a780a37",
        "user_sid": "S-1-5-21-1001",
        "registration": None,
    }
    assert decode_registration(json.dumps(payload).encode(), "lima-selected").guid is None
    registrations: tuple[object, ...] = (
        [],
        {},
        {"guid": "bad", "name": "lima-selected", "base_path": "C:\\lima"},
        {"guid": "613ea898-8445-4e6e-82c7-f6e9ae8d7235", "name": "Lima-selected", "base_path": "C:\\lima"},
    )
    for registration in registrations:
        with pytest.raises(StateError):
            decode_registration(json.dumps({**payload, "registration": registration}).encode(), "lima-selected")


def test_config_read_retains_only_classification_and_fingerprints(tmp_path: Path) -> None:
    (tmp_path / "selected").mkdir()
    (tmp_path / "selected/lima.yaml").write_text("vmType: qemu\nuser:\n  name: private-name\n")
    configuration = read_configuration(str(tmp_path), "selected")
    assert configuration.drivers == (None, "qemu", None)
    assert configuration.fingerprints[1] is not None and len(configuration.fingerprints[1]) == 64
    assert not hasattr(configuration, "user")
