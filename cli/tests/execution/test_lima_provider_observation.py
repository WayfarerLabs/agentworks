"""Host-only Lima WSL selection and authorized first-import transitions."""

from __future__ import annotations

import json
import platform
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import LifecycleObligationState, VMStatus
from agentworks.errors import StateError
from agentworks.execution import _lima_activation, _lima_configuration, _lima_selection
from agentworks.execution._lima_configuration import LimaConfiguration, decode_version, read_configuration
from agentworks.execution._lima_selection import LimaDirectoryCustody, LimaLocalSelection
from agentworks.execution._wsl2_registration import WSLRegistration, decode_registration, execution_power_from_listing
from agentworks.execution.carrier import Deadline, SinkOutput
from agentworks.execution.target import ExecutionRoute
from agentworks.vms._native_operation import _vm_operation
from tests.execution.carriers.test_lima import process_result
from tests.vms.test_lima_native_access import local as local

pytestmark = pytest.mark.windows


@pytest.fixture
def wsl(local, monkeypatch):
    fake, access = local
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
    state.run = run
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
    locator = access.ensure_available(power, deadline)
    assert access.selected is original and access.activation.running_observed
    assert access.activation.obligation.state is LifecycleObligationState.RESOLVED
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
