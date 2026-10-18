"""Synthetic local Lima composition with real SQLite custody and packed helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope, VMStatus
from agentworks.db.operations import OperationRepository
from agentworks.errors import ConnectivityError, LimitExceededError, StateError, ValidationError
from agentworks.execution import _lima_activation, _lima_selection
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution._lima_activation import decode_activation_payload
from agentworks.execution._lima_selection import LimaLocalSelection, LimaObservationControlFact
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    EndOfInput,
    ExitStatus,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers import lima
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.lima import LimaCarrier
from agentworks.operations import OperationOwner
from agentworks.vms._lima_native_access import LimaOwnedNativePlatformAccess
from agentworks.vms._native_operation import _prepare_target, _Workflow
from agentworks.vms.target_preparation import VMTargetPreparationFailure, VMTargetPreparationStatus
from tests.execution.carriers.test_lima import process_result
from tests.vms.test_target_preparation import _MARKER, TranscriptCarrier, _success_payload

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agentworks.db import OperationOwnership

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux helper and directory proof")


@dataclass
class LocalLima:
    home: Path
    owner: OperationOwner
    status: str = "Running"
    user: str = "limauser"
    vm_type: str = "qemu"
    calls: list[tuple[list[str], Deadline, str]] = field(default_factory=list)
    raw: bytes | None = None
    complete: bool = True
    has_errors: bool = False

    @property
    def directory(self) -> Path:
        return self.home / "selected"

    def run(
        self,
        argv: list[str],
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        assert env is not None
        self.calls.append((argv, deadline, env["LIMA_HOME"]))
        assert env["LIMA_HOME"] == str(self.home)
        if argv[1] == "start":
            rows = self.owner.list_pending_lifecycle_obligations()
            assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT
            assert rows[0].payload_revision == 0
            assert decode_activation_payload(rows[0].payload).locator.startswith("lima:v1:")
            self.status = "Running"
            return process_result()
        assert argv[1] == "list" and argv[-1] == "selected"
        native = ".config.user.name" in argv[4]
        data = (
            self.raw
            if self.raw is not None
            else json.dumps(
                {
                    "name": "selected",
                    "status": self.status,
                    "dir": str(self.directory),
                    "vmType": self.vm_type,
                    **({"user": self.user} if native else {}),
                    "hasErrors": self.has_errors,
                }
            ).encode()
        )
        assert isinstance(io.output, SinkOutput)
        assert io.output.stdout.try_write(memoryview(data)) == len(data)
        discarded = io.output.stderr.try_write(memoryview(b"synthetic-private-provider-warning"))
        assert discarded is not None and discarded > 0
        output = CapturedOutput(complete=self.complete, retention=Retention.DELIVERED)
        return process_result(stdout=output, stderr=output)


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[LocalLima, LimaOwnedNativePlatformAccess]]:
    home = tmp_path / "lima-home"
    home.mkdir()
    (home / "selected").mkdir()
    (home / "selected/lima.yaml").write_text("vmType: qemu\n")
    database = Database(tmp_path / "state.db")
    database.insert_vm("box", "local", "box", admin_username="different-admin", instance_marker=_MARKER)
    database.update_vm_platform_metadata("box", {"instance_name": "selected"})
    vm = database.get_vm("box")
    assert vm is not None
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "box"), "lima-proof")
    fake = LocalLima(home, owner)
    monkeypatch.setenv("LIMA_HOME", str(home))
    monkeypatch.setattr(shutil, "which", lambda name: "/literal/limactl")
    monkeypatch.setattr(_lima_selection, "run_process", fake.run)
    monkeypatch.setattr(_lima_activation, "run_process", fake.run)
    access = cast(
        "LimaOwnedNativePlatformAccess",
        LimaPlatform("local", {}).build_native_execution_access(
            vm, RunContext(), owner=owner, custody=LocalDeliveryCustody()
        ),
    )
    try:
        yield fake, access
    finally:
        access.custody.close(Deadline.after(2))
        if access.route is not None:
            access.route.close()
        database.close()


def install_guest(monkeypatch: pytest.MonkeyPatch, *, payload: bytes | None = None) -> TranscriptCarrier:
    transcript = TranscriptCarrier(_success_payload() if payload is None else payload, deadlines=[])
    monkeypatch.setattr(
        LimaCarrier,
        "execute",
        lambda self, invocation, *, io, deadline, custody: transcript.execute(
            invocation, io=io, deadline=deadline, custody=custody
        ),
    )
    return transcript


def prepare_native(access: LimaOwnedNativePlatformAccess, power: VMStatus, deadline: Deadline) -> _Workflow:
    workflow = _Workflow(access.owner, deadline, access=access)
    access.native_configuration_check()
    locator = access.ensure_available(power, deadline)
    binding = access.prepare_native_route(deadline)
    _prepare_target(workflow, access.vm, access.platform, locator, binding)
    return workflow


def test_passive_factory_and_missing_prerequisite(local, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, access = local
    assert fake.calls == [] and access.route is None and access.binding is None and access.route_check is None
    assert not access.owner.list_pending_lifecycle_obligations()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ConnectivityError):
        access.observe_power(Deadline.after(5))
    assert fake.calls == [] and access.settle(Deadline.after(5))


def test_projection_binding_and_same_deadline_home_through_start_and_guest(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    guest = install_guest(monkeypatch)
    deadline = Deadline.after(10)
    power = access.observe_power(deadline)
    assert power is VMStatus.STOPPED
    selected = access.selected
    assert selected is not None
    monkeypatch.setenv("LIMA_HOME", "/must-not-follow-new-environment")
    workflow = prepare_native(access, power, deadline)
    assert workflow.preparation is not None and workflow.preparation.status is VMTargetPreparationStatus.PREPARED
    binding = access.binding
    assert binding.delivery_account == fake.user != access.vm.admin_username
    assert binding._early_guest_facts_route.account == fake.user
    assert binding._early_guest_facts_route.root_entry.mode is IdentityMode.SUDO_ROOT
    assert binding._new_managed_delivery() is not binding.carrier
    assert binding.carrier.connection.lima_home == str(fake.home)
    assert all(item[1] is deadline and item[2] == str(fake.home) for item in fake.calls)
    assert guest.deadlines is not None and all(item is deadline for item in guest.deadlines)
    assert sum(argv[1] == "start" for argv, _, _ in fake.calls) == 1
    query = fake.calls[0][0]
    assert query[2] == "--format=json" and query[3] == "--yq"
    activation = access.activation
    assert activation is not None and activation.running_observed
    assert activation.obligation.state is LifecycleObligationState.RESOLVED
    check = access.route_check
    assert check is not None
    check(deadline)
    directory = access.route.directory
    descriptor = directory.descriptor
    assert descriptor is not None
    assert access.settle(Deadline.after(5))
    assert directory.descriptor is None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert all(argv[1] in {"list", "start"} for argv, _, _ in fake.calls)
    assert access.owner.ownership.scope.resource_name == "box"


def test_original_power_change_refuses_before_start(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    original = access.observe_power(Deadline.after(10))
    fake.status = "Running"
    with pytest.raises(StateError):
        prepare_native(access, original, Deadline.after(10))
    assert access.activation is None and not access.owner.list_pending_lifecycle_obligations()


@pytest.mark.parametrize("initial", ["Running", "Stopped"])
@pytest.mark.parametrize("current", ["Running", "Stopped"])
def test_repeated_power_is_fresh_without_replacing_original_selection(local, initial, current) -> None:
    fake, access = local
    fake.status = initial
    deadline = Deadline.after(5)
    original_power = access.observe_power(deadline)
    original = access.selected
    fake.status = current
    assert access.observe_power(deadline) is VMStatus(current.lower())
    assert access.selected is original and original.power is original_power
    assert len(fake.calls) == 2 and all(argv[1] == "list" for argv, _, _ in fake.calls)
    assert all(observed_deadline is deadline for _, observed_deadline, _ in fake.calls)
    assert access.activation is None and access.binding is None and access.route_check is None
    assert not access.owner.list_pending_lifecycle_obligations()
    if initial != current:
        with pytest.raises(StateError):
            access.ensure_available(original_power, deadline)
        assert access.activation is None


def test_repeated_power_requires_original_locator(local, monkeypatch) -> None:
    fake, access = local
    deadline = Deadline.after(5)
    access.observe_power(deadline)
    original = access.selected
    observe = LimaLocalSelection.observe

    def changed(*args, **kwargs):
        return replace(observe(*args, **kwargs), namespace="different-placement")

    monkeypatch.setattr(LimaLocalSelection, "observe", changed)
    with pytest.raises(StateError):
        access.observe_power(deadline)
    assert access.selected is original and len(fake.calls) == 2
    assert access.activation is None and access.binding is None


def test_repeated_power_clears_unavailable_fact_without_native_route(local) -> None:
    fake, access = local
    deadline = Deadline.after(5)
    original_power = access.observe_power(deadline)
    original = access.selected
    access.ensure_available(original_power, deadline)
    assert access.independent_availability is not None
    fake.status = "Stopped"
    assert access.observe_power(deadline) is VMStatus.STOPPED
    assert access.selected is original and original.power is VMStatus.RUNNING
    assert access.independent_availability is None and access.binding is None
    assert access.activation is None and all(argv[1] == "list" for argv, _, _ in fake.calls)


@pytest.mark.parametrize("closed", [False, True])
def test_repeated_power_refuses_closed_or_expired_access(local, closed) -> None:
    fake, access = local
    access.observe_power(Deadline.after(5))
    original = access.selected
    if closed:
        assert access.settle(Deadline.after(5))
    with pytest.raises(StateError if closed else LimitExceededError):
        access.observe_power(Deadline.after(0))
    assert access.selected is original and len(fake.calls) == 1
    assert access.activation is None and access.binding is None


@pytest.mark.parametrize("change", ["user", "vm_type", "replacement"])
def test_selected_route_drift_refuses_before_guest(local, monkeypatch, change: str) -> None:
    fake, access = local
    guest = install_guest(monkeypatch)
    power = access.observe_power(Deadline.after(10))
    original = access.selected.locator
    access.ensure_available(power, Deadline.after(10))
    access.prepare_native_route(Deadline.after(10))
    if change == "replacement":
        fake.directory.rename(fake.home / "old")
        fake.directory.mkdir()
    else:
        setattr(fake, change, "changed")
        if change == "user":
            (fake.directory / "lima.yaml").write_text("vmType: qemu\nuser:\n  name: changed\n")
    with pytest.raises(StateError):
        access.route_check(Deadline.after(10))
    assert guest.calls == 0
    assert access.selected.locator == original
    assert access.settle(Deadline.after(10))


def test_locator_namespace_and_materialized_clone_are_distinct(local) -> None:
    fake, access = local
    deadline = Deadline.after(10)
    access.observe_power(deadline)
    original = access.selected.locator
    route = LimaLocalSelection("other-site", "selected", "box", str(fake.home), "/literal/limactl")
    try:
        assert route.observe(deadline, LocalDeliveryCustody()).locator != original
    finally:
        route.close()
    fake.directory.rename(fake.home / "old")
    fake.directory.mkdir()
    (fake.directory / "lima.yaml").write_text("vmType: qemu\n")
    route = LimaLocalSelection("local", "selected", "box", str(fake.home), "/literal/limactl")
    try:
        assert route.observe(deadline, LocalDeliveryCustody()).locator != original
    finally:
        route.close()


@pytest.mark.parametrize("raw", [b"", b"[]", b"{}", b"\xff", b"{}\n{}", b"x" * 16_385])
def test_missing_invalid_and_oversized_projection_refuses(local, raw: bytes) -> None:
    fake, access = local
    fake.raw = raw
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert access.binding is None and access.activation is None


def test_incomplete_and_late_observations_cannot_select(local, monkeypatch) -> None:
    fake, access = local
    fake.complete = False
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    fake.complete = True
    deadline = Deadline.after(5)
    run = fake.run

    def late(*args, **kwargs):
        result = run(*args, **kwargs)
        object.__setattr__(deadline, "expires_at", 0.0)
        return result

    monkeypatch.setattr(_lima_selection, "run_process", late)
    with pytest.raises(LimitExceededError):
        access.observe_power(deadline)
    assert access.selected is None


def test_finite_deadline_required_before_lookup(local) -> None:
    fake, access = local
    with pytest.raises(ValidationError):
        access.observe_power(Deadline(None))
    assert fake.calls == []


def test_direct_resolver_never_starts_and_preserves_snapshot(local, monkeypatch) -> None:
    fake, access = local
    binding = access.platform.resolve_native_execution_binding(access.vm, access.ctx, deadline=Deadline.after(5))
    assert binding.delivery_account == fake.user
    assert all(argv[1] == "list" for argv, _, _ in fake.calls)
    route = binding.carrier.connection
    assert route.selected_directory is not None
    fake.directory.rename(fake.home / "old")
    fake.directory.mkdir()
    pump = []
    monkeypatch.setattr(lima, "run_process", lambda *a, **kw: pump.append(a))
    with pytest.raises(StateError):
        binding.carrier.execute(
            PreparedInvocation(("/noop",)), io=CarrierIO(), deadline=Deadline.after(5), custody=LocalDeliveryCustody()
        )
    assert pump == []


@pytest.mark.parametrize(
    "method", ["observe_provider_locator", "observe_execution_power", "resolve_native_execution_binding"]
)
def test_remote_refusal_never_discovers_local_or_guest(local, method: str) -> None:
    fake, access = local
    remote = LimaPlatform("remote", {"placement": {"mode": "ssh", "host": "remote-host"}})
    kwargs: dict[str, object] = {"deadline": Deadline.after(5)}
    if method != "resolve_native_execution_binding":
        kwargs["custody"] = LocalDeliveryCustody()
    with pytest.raises(StateError):
        getattr(remote, method)(access.vm, access.ctx, **kwargs)
    assert fake.calls == []


def test_lost_start_registration_reply_reconciles_without_dispatch(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(10))
    register = OperationOwner.register_lifecycle_obligation

    def lost(self: OperationOwner, *args, **kwargs):
        register(self, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(OperationOwner, "register_lifecycle_obligation", lost)
    with pytest.raises(KeyboardInterrupt):
        prepare_native(access, power, Deadline.after(10))
    assert access.activation is not None
    assert sum(argv[1] == "start" for argv, _, _ in fake.calls) == 0
    assert access.settle(Deadline.after(10))
    assert not access.owner.list_pending_lifecycle_obligations()


def test_lost_resolution_reply_reuses_retained_running_evidence(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(10))
    original = OperationRepository.resolve_lifecycle_obligation
    raised = False

    def lost(self: OperationRepository, ownership: OperationOwnership, obligation_id: str) -> None:
        nonlocal raised
        original(self, ownership, obligation_id)
        if not raised:
            raised = True
            raise KeyboardInterrupt

    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", lost)
    with pytest.raises(KeyboardInterrupt):
        prepare_native(access, power, Deadline.after(10))
    assert access.activation.running_observed
    assert access.settle(Deadline.after(10))
    assert sum(argv[1] == "start" for argv, _, _ in fake.calls) == 1


def test_start_completion_without_fresh_readiness_stays_debt(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(10))
    original = fake.run

    def start(*args, **kwargs):
        result = original(*args, **kwargs)
        fake.raw = b"invalid"
        return result

    monkeypatch.setattr(_lima_activation, "run_process", start)
    with pytest.raises(StateError):
        prepare_native(access, power, Deadline.after(10))
    assert access.activation.client_completed and not access.activation.running_observed
    assert not access.settle(Deadline.after(10))
    assert access.owner.list_pending_lifecycle_obligations()
    assert sum(argv[1] == "start" for argv, _, _ in fake.calls) == 1


def test_interrupted_start_retains_actual_process_custody_and_no_replay(local, monkeypatch) -> None:
    fake, access = local
    fake.status = "Stopped"
    power = access.observe_power(Deadline.after(10))
    held = []

    def interrupt(*args, **kwargs):
        held.append(kwargs["custody"].begin_process())
        raise KeyboardInterrupt

    monkeypatch.setattr(_lima_activation, "run_process", interrupt)
    with pytest.raises(KeyboardInterrupt):
        prepare_native(access, power, Deadline.after(10))
    assert not access.custody.settled and access.activation.custody is access.custody
    assert not access.settle(Deadline.after(10))
    assert access.custody.settled and len(held) == 1
    with pytest.raises(StateError):
        access.activation.start(Deadline.after(5))


def test_standalone_interruption_escapes_with_actual_query_custody(local, monkeypatch) -> None:
    fake, access = local
    retained = LocalDeliveryCustody()

    def interrupt(*args, **kwargs):
        custody = kwargs["custody"]
        custody.begin_process()
        assert custody is not retained
        raise KeyboardInterrupt

    monkeypatch.setattr(_lima_selection, "run_process", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        access.platform.resolve_native_execution_binding(access.vm, access.ctx, deadline=Deadline.after(5))
    fact = caught.value.__cause__
    assert isinstance(fact, LimaObservationControlFact)
    assert not fact.custody.settled
    assert fact.close(Deadline.after(5)) and fact.custody.settled


@pytest.mark.parametrize("marker_ok", [True, False])
def test_guest_marker_is_still_checked(local, monkeypatch, marker_ok: bool) -> None:
    fake, access = local
    payload = _success_payload() if marker_ok else _success_payload().replace(_MARKER.encode(), b"f" * 32)
    guest = install_guest(monkeypatch, payload=payload)
    power = access.observe_power(Deadline.after(10))
    workflow = prepare_native(access, power, Deadline.after(10))
    assert guest.calls == 1
    assert workflow.preparation is not None
    assert (workflow.preparation.status is VMTargetPreparationStatus.PREPARED) is marker_ok
    if not marker_ok:
        assert workflow.preparation.failure is VMTargetPreparationFailure.IDENTITY


def test_actual_packed_guest_probe_under_explicit_mocked_admission(local, monkeypatch) -> None:
    fake, access = local
    calls = []

    def execute(self, invocation, *, io, deadline, custody):
        assert isinstance(io.input, EndOfInput) and isinstance(io.output, SinkOutput)
        nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", _NAMED_ADMISSION, nonce, invocation.argv[-2], _MARKER],
            capture_output=True,
            timeout=deadline.remaining(),
            check=False,
        )
        calls.append(result)
        for sink, data in (
            (io.output.stdout, f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode() + result.stdout),
            (io.output.stderr, result.stderr),
        ):
            remaining = memoryview(data)
            while remaining:
                count = sink.try_write(remaining)
                assert count is not None and count > 0
                remaining = remaining[count:]
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=result.returncode), 0, output, output)

    monkeypatch.setattr(LimaCarrier, "execute", execute)
    power = access.observe_power(Deadline.after(10))
    workflow = prepare_native(access, power, Deadline.after(10))
    assert workflow.preparation is not None and workflow.preparation.status is VMTargetPreparationStatus.PREPARED
    assert len(calls) == 1 and calls[0].returncode == 0, calls[0].stderr
    assert access.settle(Deadline.after(5))


def test_fresh_platform_import_does_not_load_retired_transport_packages() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; "
            "from agentworks.capabilities.vm_platform.lima import LimaPlatform; "
            "from agentworks.vms._lima_native_access import LimaOwnedNativePlatformAccess; "
            "assert 'agentworks.transports' not in sys.modules; "
            "assert 'agentworks.ssh' not in sys.modules",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


# Mock only root/account admission and external guest facts. The packed named
# bootstrap, source loader, runtime record, and guest identity decoder run intact.
_NAMED_ADMISSION = """
import builtins,contextlib,os,sys,types
real_exec=builtins.exec
marker=sys.argv[3]
@contextlib.contextmanager
def admit(uid,gid,groups):
 assert (uid,gid,groups)==(1001,1001,(1001,))
 yield lambda:b'1 (init) S '+b'0 '*18+b'4096\\n'
def enter(frame,event,arg):
 if event=='call' and frame.f_code.co_filename=='<agentworks-root-bootstrap>' and frame.f_code.co_name=='_run_named':
  scope=frame.f_globals
  scope['_admit']=admit
  scope['os']=types.SimpleNamespace(**dict(vars(os),getresuid=lambda:(0,0,0),getgrouplist=lambda *a:[1001]))
  import pwd
  pwd.getpwnam=lambda account:types.SimpleNamespace(pw_uid=1001,pw_gid=1001)
  sys.setprofile(None)
def execute(source,scope=None,local=None):
 if scope is None:scope=sys._getframe(1).f_globals
 real_exec(source,scope,local)
 if scope.get('__name__','').endswith('._vm_guest_identity_guest'):
  scope['_read_marker']=lambda *args:marker
  scope['_read_boot_id']=lambda *args:'00000000-0000-4000-8000-000000000001'
wrapper=sys.argv[2]
sys.argv=['fixed-helper',sys.argv[1]]
builtins.exec=execute
sys.setprofile(enter)
real_exec(compile(wrapper,'<delivered-wrapper>','exec'),{'__name__':'__main__'})
"""
