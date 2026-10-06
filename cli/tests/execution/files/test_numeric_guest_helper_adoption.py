"""Private numeric file-helper routing and mocked admission boundary proofs."""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _file_effect_gate_bundle as gate_bundle
from agentworks.execution import _file_snapshot_bundle as snapshot_bundle
from agentworks.execution import _guest_bootstrap
from agentworks.execution._file_effect_gate import FileEffectGateBinding
from agentworks.execution._file_effect_gate_exchange import GateControlObservationState, exchange_file_effect_gate
from agentworks.execution._file_effect_gate_protocol import GateControlOperation, decode_gate_control_request
from agentworks.execution._file_snapshot_exchange import (
    FileSnapshotObservationState,
    snapshot_begin,
    snapshot_chunk,
    snapshot_cleanup,
    snapshot_reconcile,
    snapshot_stream,
)
from agentworks.execution._file_snapshot_protocol import decode_file_snapshot_request
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
    build_root_guest_bootstrap_argv,
    build_runtime_identity_helper_argv,
)
from agentworks.execution._scratch import _cleanup_debt
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from tests.execution.files._file_snapshot_support import LocalCarrier
from tests.execution.files._runtime_support import runtime_nonce, runtime_ready_record

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="private numeric helpers require Linux")

_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 1234)
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX)
_TOKEN = bytes(range(16))
_GATE_PATH = "/run/agentworks/file-gates-v1/0/" + "a" * 64 + ".db"


def _plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


def _gate(guest: VMGuestIdentity = _GUEST) -> FileEffectGateBinding:
    return FileEffectGateBinding(_GATE_PATH, b"i" * 16, b"g" * 16, guest, _plan().expected.euid, "vm-a", 1, 2)


class _CapturedCarrier:
    features = ChannelFeatures(live_stdio=True)

    def __init__(self) -> None:
        self.calls = 0
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        del invocation, io

    def _run(self, invocation: PreparedInvocation, io: CarrierIO) -> int:
        del invocation, io
        return 125

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        self.calls += 1
        self.invocation, self.io = invocation, io
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        ready = runtime_ready_record(invocation)
        assert io.output.stdout.try_write(memoryview(ready)) == len(ready)
        status = self._run(invocation, io)
        output = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=status), 0, output, output, None)


def _call(family: str, carrier: Any, *, plan: IdentityPlan | None = None, **kwargs: Any) -> Any:
    common = dict(plan=plan or _plan(), deadline=Deadline.after(10), runtime_selection=_RUNTIME)
    common.update(kwargs)
    if family == "gate":
        return exchange_file_effect_gate(
            carrier,
            operation=GateControlOperation.SETUP,
            path=_gate().path,
            guest=_GUEST,
            scope_name="vm-a",
            **common,
        )
    return snapshot_begin(
        carrier,
        trusted_root_path="/missing/numeric-helper-test",
        relative_path="payload",
        max_bytes=100,
        token=_TOKEN,
        **common,
    )


@pytest.mark.parametrize("family", ["gate", "snapshot"])
@pytest.mark.parametrize("root_mode", [None, IdentityMode.DIRECT, IdentityMode.SUDO_ROOT])
def test_exchange_selects_matching_argv_prefix_and_keeps_runtime_separate(
    family: str,
    root_mode: IdentityMode | None,
) -> None:
    carrier = _CapturedCarrier()
    context = None if root_mode is None else _NumericGuestBootstrap(replace(_ROOT, mode=root_mode), _GUEST)
    plan = IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT)
    result = _call(family, carrier, plan=plan, bootstrap=context)
    bundle = gate_bundle if family == "gate" else snapshot_bundle
    invocation, io = carrier.invocation, carrier.io
    assert invocation is not None and io is not None and isinstance(io.input, FiniteInput)
    nonce = runtime_nonce(invocation)
    if context is None:
        argv, _, _ = build_runtime_identity_helper_argv(
            plan,
            selection=_RUNTIME,
            fixed_source=bundle.FIXED_BUNDLE.bootstrap,
            nonce=nonce,
        )
        prefix = bundle.FIXED_BUNDLE.prefix
    else:
        argv, _, _ = build_root_guest_bootstrap_argv(
            context.root_entry,
            plan.expected,
            selection=_RUNTIME,
            program=bundle.ROOT_PROGRAM,
            nonce=nonce,
            expected_guest=_GUEST,
        )
        prefix = bundle.ROOT_PROGRAM.prefix
    assert invocation.argv == argv
    assert io.input.sensitive and io.sensitive and io.input.data.startswith(prefix)
    decode = decode_gate_control_request if family == "gate" else decode_file_snapshot_request
    request = decode(io.input.data[len(prefix) :])
    assert request.identity == plan.expected
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.carrier_completion == ExitStatus(code=125)
    assert result.observation is not None
    assert result.observation.state.value in {"incomplete", "uncertain"}
    assert carrier.calls == 1


@pytest.mark.parametrize("family", ["gate", "snapshot"])
@pytest.mark.parametrize(
    "runtime", [RuntimeSelection(RuntimeTargetOS.DARWIN), RuntimeSelection(RuntimeTargetOS.LINUX, "/tmp/python3")]
)
def test_invalid_runtime_refuses_before_dispatch(family: str, runtime: RuntimeSelection) -> None:
    carrier = _CapturedCarrier()
    with pytest.raises(ValidationError):
        _call(family, carrier, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST), runtime_selection=runtime)
    assert carrier.calls == 0


@pytest.mark.parametrize(
    "root",
    [
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
        IdentityPlan(IdentityExpectation(0, 0, (1,)), IdentityMode.DIRECT),
    ],
)
def test_context_refuses_invalid_root(root: IdentityPlan) -> None:
    with pytest.raises(ValidationError):
        _NumericGuestBootstrap(root, _GUEST)


def test_context_requires_exact_full_guest_type() -> None:
    invalid: Any = (_GUEST.instance_marker, _GUEST.boot_id, _GUEST.init_start_ticks)
    with pytest.raises(ValidationError):
        _NumericGuestBootstrap(_ROOT, invalid)


@pytest.mark.parametrize("field", ["instance_marker", "boot_id", "init_start_ticks"])
@pytest.mark.parametrize("family", ["gate", "snapshot"])
def test_competing_guest_facts_refuse_before_dispatch(family: str, field: str) -> None:
    changed = {
        "instance_marker": "b" * 32,
        "boot_id": "223e4567-e89b-12d3-a456-426614174000",
        "init_start_ticks": 1235,
    }[field]
    context = _NumericGuestBootstrap(_ROOT, replace(_GUEST, **{field: changed}))
    carrier = _CapturedCarrier()
    with pytest.raises(ValidationError):
        _call(family, carrier, bootstrap=context, **({"effect_gate": _gate()} if family == "snapshot" else {}))
    assert carrier.calls == 0


class _MockAdmissionCarrier(_CapturedCarrier):
    """Execute real packed modules with mocked credentials and fixed-path facts."""

    def __init__(
        self,
        family: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        plan: IdentityPlan,
        *,
        observed: VMGuestIdentity = _GUEST,
        change_after_checkpoint: bool = False,
    ) -> None:
        super().__init__()
        self.family, self.tmp_path, self.monkeypatch, self.plan = family, tmp_path, monkeypatch, plan
        self.observed, self.change_after_checkpoint = observed, change_after_checkpoint
        self.events: list[str] = []
        self.reads = 0
        self.descriptors: list[int] = []

    def _run(self, invocation: PreparedInvocation, io: CarrierIO) -> int:
        bundle = gate_bundle if self.family == "gate" else snapshot_bundle
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        payload = io.input.data
        output = io.output.stdout
        stat_path = self.tmp_path / "init-stat"
        stat_path.write_bytes(b"1 (init) S " + b"0 " * 18 + f"{self.observed.init_start_ticks}\n".encode())
        scratch = self.tmp_path / "scratch"
        scratch.mkdir(exist_ok=True)
        scratch.chmod(0o1777)
        actual_uid = os.geteuid()
        namespace = self.tmp_path / "run" / "agentworks" / "file-gates-v1"
        (namespace / str(self.plan.expected.euid)).mkdir(parents=True, exist_ok=True, mode=0o700)
        original_exec, original_read_init = builtins.exec, _guest_bootstrap._read_init
        offset = 0
        current = _ROOT.expected

        def drop(uid: int, gid: int, groups: tuple[int, ...]) -> None:
            nonlocal current
            assert IdentityExpectation(uid, gid, groups) == self.plan.expected
            current = self.plan.expected
            self.events.append("drop")

        def read_init(descriptor: int) -> bytes:
            assert current == self.plan.expected
            self.events.append("init")
            self.reads += 1
            self.descriptors.append(descriptor)
            if self.change_after_checkpoint and self.reads > 1:
                stat_path.write_bytes(b"1 (init) S " + b"0 " * 18 + b"1235\n")
            return original_read_init(descriptor)

        def read(descriptor: int, count: int) -> bytes:
            nonlocal offset
            if descriptor != 0:
                return real_read(descriptor, count)
            assert current == self.plan.expected
            self.events.append("prefix" if offset < len(bundle.ROOT_PROGRAM.prefix) else "request")
            block = payload[offset : offset + count]
            offset += len(block)
            return block

        def execute_module(source: Any, scope: dict[str, Any], local: dict[str, Any] | None = None) -> None:
            original_exec(source, scope, local)
            name = scope.get("__name__", "")
            if name == bundle._PACKAGE + "._vm_guest_identity_guest":
                scope["_read_marker"] = lambda *_args: self.observed.instance_marker
                scope["_read_boot_id"] = lambda *_args: self.observed.boot_id
            elif name.startswith(bundle._PACKAGE + ".") and not name.endswith("._vm_guest_identity_protocol"):
                self.events.append("remaining")
                if name.endswith("._scratch_root"):
                    scope["_LINUX_SCRATCH_ROOT"], scope["_EXPECTED_OWNER_UID"] = str(scratch), actual_uid
                elif name.endswith("._file_effect_gate"):
                    scope["_GATE_NAMESPACE"], scope["_ROOT_UID"] = str(namespace), actual_uid
                elif name.endswith("._file_effect_gate_guest") or name.endswith("._file_snapshot_guest"):
                    real_main = scope["main"]

                    def main(nonce: str) -> int:
                        self.events.append("body")
                        result = real_main(nonce)
                        assert isinstance(result, int)
                        return result

                    scope["main"] = main

        real_read, real_write = os.read, os.write
        previous = {name: module for name, module in sys.modules.items() if name.startswith(bundle._PACKAGE)}
        try:
            with self.monkeypatch.context() as patch:
                patch.setattr(_guest_bootstrap, "_INIT_PATH", str(stat_path))
                patch.setattr(_guest_bootstrap, "_drop_and_verify", drop)
                patch.setattr(_guest_bootstrap, "_read_init", read_init)
                patch.setattr(os, "getresuid", lambda: (current.euid,) * 3)
                patch.setattr(os, "getresgid", lambda: (current.egid,) * 3)
                patch.setattr(os, "geteuid", lambda: current.euid)
                patch.setattr(os, "getegid", lambda: current.egid)
                patch.setattr(os, "getgroups", lambda: list(current.groups))
                patch.setattr(os, "read", read)
                patch.setattr(
                    os,
                    "write",
                    lambda fd, data: output.try_write(memoryview(data)) if fd == 1 else real_write(fd, data),
                )
                patch.setattr(sys, "argv", ["agentworks-fixed-helper", runtime_nonce(invocation)])
                patch.setattr(builtins, "exec", execute_module)
                expected = self.plan.expected
                return _guest_bootstrap.main(
                    expected.euid,
                    expected.egid,
                    expected.groups,
                    bundle.ROOT_PROGRAM.loader_source,
                    (_GUEST.instance_marker, _GUEST.boot_id, _GUEST.init_start_ticks),
                )
        finally:
            for name in tuple(sys.modules):
                if name.startswith(bundle._PACKAGE):
                    del sys.modules[name]
            sys.modules.update(previous)


@pytest.mark.parametrize("field", ["instance_marker", "boot_id", "init_start_ticks"])
@pytest.mark.parametrize("family", ["gate", "snapshot"])
def test_full_checkpoint_refuses_before_remaining_modules_body_or_request(
    family: str,
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {"instance_marker": "b" * 32, "boot_id": "223e4567-e89b-12d3-a456-426614174000", "init_start_ticks": 1235}
    carrier = _MockAdmissionCarrier(
        family, tmp_path, monkeypatch, _plan(), observed=replace(_GUEST, **{field: values[field]})
    )
    result = _call(family, carrier, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST))
    assert result.carrier_completion == ExitStatus(code=125)
    assert carrier.events == ["drop", "prefix", "init"]
    assert len(set(carrier.descriptors)) == 1
    with pytest.raises(OSError):
        os.fstat(carrier.descriptors[0])


def test_snapshot_binary_lifecycle_forwards_context_after_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    carrier = _MockAdmissionCarrier("snapshot", tmp_path, monkeypatch, plan)
    context = _NumericGuestBootstrap(_ROOT, _GUEST)
    source = tmp_path / "source"
    source.mkdir()
    content = bytes(range(256)) + b"\x00\xffnumeric-guest"
    (source / "payload").write_bytes(content)
    common = dict(plan=plan, deadline=Deadline.after(10), runtime_selection=_RUNTIME, bootstrap=context)
    begun = snapshot_begin(
        carrier, trusted_root_path=str(source), relative_path="payload", max_bytes=len(content), token=_TOKEN, **common
    )
    assert begun.observation is not None and begun.observation.snapshot is not None
    ready = begun.observation.snapshot.ready
    chunk = snapshot_chunk(carrier, ready=ready, offset=0, length=len(content), token=_TOKEN, **common)
    assert chunk.observation is not None and chunk.observation.chunk is not None
    assert chunk.observation.chunk.data == content
    downloaded = bytearray()
    streamed = snapshot_stream(
        carrier, ready=ready, token=_TOKEN, write_data=lambda block: downloaded.extend(block) or True, **common
    )
    assert streamed.observation is not None and streamed.observation.state is FileSnapshotObservationState.STREAM
    assert bytes(downloaded) == content
    recovered = snapshot_reconcile(carrier, token=_TOKEN, **common)
    assert recovered.observation is not None and recovered.observation.cleanup_debt == _cleanup_debt(ready)
    cleaned = snapshot_cleanup(carrier, token=_TOKEN, cleanup_debt=_cleanup_debt(ready), **common)
    assert cleaned.observation is not None and cleaned.observation.state is FileSnapshotObservationState.CLEANED
    assert carrier.calls == 5 and carrier.events.count("drop") == 5 and carrier.events.count("body") == 5
    assert carrier.events[:3] == ["drop", "prefix", "init"]
    assert carrier.events.index("body") < carrier.events.index("request")


def test_gate_body_uses_fresh_held_reader_after_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    path = tmp_path / "run" / "agentworks" / "file-gates-v1" / str(plan.expected.euid) / ("a" * 64 + ".db")
    carrier = _MockAdmissionCarrier("gate", tmp_path, monkeypatch, plan)
    common = dict(
        operation=GateControlOperation.SETUP,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=_RUNTIME,
        bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST),
    )
    result = exchange_file_effect_gate(carrier, **common)
    assert result.observation is not None and result.observation.state is GateControlObservationState.RESOLVED
    assert carrier.reads >= 3 and len(set(carrier.descriptors)) == 1
    carrier.change_after_checkpoint = True
    carrier.reads = 0
    changed = exchange_file_effect_gate(carrier, **common)
    assert changed.observation is not None and changed.observation.state is GateControlObservationState.REFUSED
    assert carrier.reads == 2 and carrier.calls == 2


def test_explicit_root_body_identity_is_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = _MockAdmissionCarrier("snapshot", tmp_path, monkeypatch, _ROOT)
    result = _call("snapshot", carrier, plan=_ROOT, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST))
    assert result.observation is not None and result.observation.state is FileSnapshotObservationState.ABSENT
    assert "body" in carrier.events


@pytest.mark.parametrize("changed", [False, True])
def test_gated_snapshot_refreshes_guest_before_its_effect(
    changed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    context = _NumericGuestBootstrap(_ROOT, _GUEST)
    common = dict(plan=plan, deadline=Deadline.after(10), runtime_selection=_RUNTIME, bootstrap=context)
    gate_carrier = _MockAdmissionCarrier("gate", tmp_path, monkeypatch, plan)
    path = tmp_path / "run" / "agentworks" / "file-gates-v1" / str(plan.expected.euid) / ("a" * 64 + ".db")
    gate = exchange_file_effect_gate(
        gate_carrier, operation=GateControlOperation.SETUP, path=str(path), guest=_GUEST, scope_name="vm-a", **common
    )
    assert gate.observation is not None and gate.observation.binding is not None
    carrier = _MockAdmissionCarrier("snapshot", tmp_path, monkeypatch, plan, change_after_checkpoint=changed)
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"\x00\xffsnapshot")
    result = snapshot_begin(
        carrier,
        trusted_root_path=str(source),
        relative_path="payload",
        max_bytes=100,
        token=_TOKEN,
        effect_gate=gate.observation.binding,
        **common,
    )
    assert result.observation is not None and carrier.reads >= 2
    if changed:
        assert result.observation.state is FileSnapshotObservationState.REFUSED
        assert not list((tmp_path / "scratch").iterdir())
    else:
        assert result.observation.snapshot is not None
        snapshot_cleanup(
            carrier,
            token=_TOKEN,
            cleanup_debt=_cleanup_debt(result.observation.snapshot.ready),
            effect_gate=gate.observation.binding,
            **common,
        )


@pytest.mark.parametrize("family", ["gate", "snapshot"])
def test_generated_nonroot_entry_refuses_on_python311(family: str) -> None:
    runtime = Path("/usr/bin/python3.11")
    if os.geteuid() == 0 or not runtime.is_file():
        pytest.skip("requires nonroot Linux with distribution Python 3.11")
    carrier = _CapturedCarrier()
    _call(family, carrier, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST))
    assert carrier.invocation is not None and carrier.io is not None and isinstance(carrier.io.input, FiniteInput)
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", carrier.invocation.argv[-2]],
        input=carrier.io.input.data,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 125 and completed.stdout == completed.stderr == b""
    actual = _call(family, LocalCarrier(), bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST))
    assert actual.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert actual.carrier_completion == ExitStatus(code=125)
    assert actual.observation is not None and actual.observation.state.value in {"uncertain", "incomplete"}


@pytest.mark.parametrize("family", ["gate", "snapshot"])
def test_numeric_exchange_keeps_actual_provider_aggregate_preflight(
    family: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.example:8006", "node-a", 101, "root@pam!token", "secret"))
    monkeypatch.setattr(carrier._wire, "request", lambda *_a, **_kw: pytest.fail("provider dispatch reached"))
    # The closed manifest fits; the actual launcher plus input exceeds the provider envelope.
    groups = tuple(sorted({1001} | {(index * 2654435761) % (2**32) for index in range(2900)}))
    oversized = IdentityPlan(IdentityExpectation(1001, 1001, groups), IdentityMode.DIRECT)
    with pytest.raises(ValidationError):
        _call(family, carrier, plan=oversized, bootstrap=_NumericGuestBootstrap(_ROOT, _GUEST))
