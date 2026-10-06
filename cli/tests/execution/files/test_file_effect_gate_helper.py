"""Fixed private gate-control request, guest and one-attempt exchange proofs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.execution import _file_effect_gate_exchange
from agentworks.execution._file_effect_gate_bundle import _MODULE_NAMES, _PACKAGE, FIXED_BUNDLE
from agentworks.execution._file_effect_gate_exchange import GateControlObservationState, exchange_file_effect_gate
from agentworks.execution._file_effect_gate_protocol import (
    GateControlOperation,
    GateControlProtocolError,
    GateControlRequest,
    decode_gate_control_request,
    encode_gate_control_request,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind
from agentworks.execution._file_wire_reader import FileRecordReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, build_runtime_identity_helper_argv
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, Dispatch, PreparedInvocation, SinkOutput
from agentworks.execution.carriers.ssh.connection import SSHConnection, build_ssh_argv
from tests.execution.files._file_snapshot_support import LocalCarrier
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._runtime_support import runtime_selection

if TYPE_CHECKING:
    from agentworks.execution._file_effect_gate import FileEffectGateBinding

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file gate is Linux-only")


@pytest.fixture(autouse=True)
def _gate_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.execution import _file_effect_gate

    namespace = tmp_path / "run" / "agentworks" / "file-gates-v1"
    (namespace / str(os.geteuid())).mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(_file_effect_gate, "_GATE_NAMESPACE", str(namespace))
    monkeypatch.setattr(_file_effect_gate, "_ROOT_UID", os.geteuid())


def _gate_path(tmp_path: Path) -> Path:
    return tmp_path / "run" / "agentworks" / "file-gates-v1" / str(os.geteuid()) / ("a" * 64 + ".db")


_NONCE = "a" * 32
_GUEST = VMGuestIdentity("b" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)


def _plan() -> IdentityPlan:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)


def _request(path: Path, operation: GateControlOperation = GateControlOperation.SETUP) -> GateControlRequest:
    expected = _plan().expected
    return GateControlRequest(_NONCE, operation, str(path), _GUEST, expected.euid, "vm-a", expected, 5.0)


def _fixture(monkeypatch: pytest.MonkeyPatch, guest: VMGuestIdentity = _GUEST) -> None:
    from agentworks.execution import _file_effect_gate

    fixture = fixture_file_bundle(
        _PACKAGE,
        _MODULE_NAMES,
        "_file_effect_gate_guest",
        f"import os,sys\ngate=sys.modules[{(_PACKAGE + '._file_effect_gate')!r}]\n"
        f"gate._GATE_NAMESPACE={_file_effect_gate._GATE_NAMESPACE!r}\n"
        "gate._ROOT_UID=os.geteuid()\n"
        "from _agw_file_effect_gate._vm_guest_identity_protocol import VMGuestIdentity\n"
        f"guest._identity=lambda: VMGuestIdentity({guest.instance_marker!r}, {guest.boot_id!r}, "
        f"{guest.init_start_ticks!r})\n",
    )
    monkeypatch.setattr(_file_effect_gate_exchange, "FIXED_BUNDLE", fixture)


def _exchange(
    path: Path,
    operation: GateControlOperation,
    *,
    binding: FileEffectGateBinding | None = None,
) -> tuple[LocalCarrier, GateControlObservationState]:
    carrier = LocalCarrier()
    candidate = exchange_file_effect_gate(
        carrier,
        operation=operation,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
        binding=binding,
    )
    assert candidate.observation is not None
    return carrier, candidate.observation.state


def test_request_is_canonical_closed_and_exactly_bounded(tmp_path: Path) -> None:
    request = _request(_gate_path(tmp_path))
    encoded = encode_gate_control_request(request)
    assert decode_gate_control_request(encoded) == request
    with pytest.raises(GateControlProtocolError):
        decode_gate_control_request(encoded[:-1] + b',"nonce":"a"}')
    with pytest.raises(GateControlProtocolError):
        decode_gate_control_request(encoded + b" ")
    with pytest.raises(GateControlProtocolError):
        decode_gate_control_request(b"{" + b" " * 32_768 + b"}")


def test_bundle_reports_invalid_request_as_closed_records() -> None:
    runtime = Path("/usr/bin/python3.11")
    if not runtime.is_file():
        pytest.skip("distribution Python 3.11 is unavailable")
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", FIXED_BUNDLE.bootstrap, _NONCE],
        input=FIXED_BUNDLE.prefix + b"{}",
        capture_output=True,
        timeout=10,
        check=False,
    )
    records: list[FileRecord] = []
    reader = FileRecordReader(_NONCE, records.append)
    reader.try_write(memoryview(completed.stdout))
    reader.finish()
    assert completed.returncode == 0
    assert completed.stderr == b""
    assert reader.error is None
    assert [record.kind for record in records] == [FileRecordKind.FAILED, FileRecordKind.FINISHED]
    assert json.loads(records[0].body) == {"code": "invalid_request"}
    assert records[1].body == b"{}"


def test_fixed_guest_setup_inspect_and_advance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(monkeypatch)
    path = _gate_path(tmp_path)
    carrier = LocalCarrier()
    setup = exchange_file_effect_gate(
        carrier,
        operation=GateControlOperation.SETUP,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert setup.observation is not None
    assert setup.observation.state is GateControlObservationState.RESOLVED
    binding = setup.observation.binding
    assert binding is not None
    assert path.is_file()
    inspect = exchange_file_effect_gate(
        LocalCarrier(),
        operation=GateControlOperation.INSPECT,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert inspect.observation is not None
    assert inspect.observation.binding == binding
    proposed = replace(binding, proposed_generation=b"c" * 16)
    advanced = exchange_file_effect_gate(
        LocalCarrier(),
        operation=GateControlOperation.ADVANCE,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
        binding=proposed,
    )
    assert advanced.observation is not None
    assert advanced.observation.state is GateControlObservationState.RESOLVED
    assert advanced.observation.binding == replace(binding, generation=b"c" * 16)


def test_stale_guest_is_refused_before_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixture(monkeypatch, replace(_GUEST, init_start_ticks=11))
    path = _gate_path(tmp_path)
    _, state = _exchange(path, GateControlOperation.SETUP)
    assert state is GateControlObservationState.REFUSED
    assert not path.exists()


class _LostOutputCarrier(LocalCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        report = super().execute(invocation, io=io, deadline=deadline)
        return replace(report, stdout=replace(report.stdout, complete=False))


class _BlackholeSink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


class _LostCompleteOutputCarrier(LocalCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        assert isinstance(io.output, SinkOutput)
        dropped = replace(io, output=replace(io.output, stdout=_BlackholeSink()))
        report = super().execute(invocation, io=dropped, deadline=deadline)
        return replace(report, stdout=replace(report.stdout, complete=False))


def test_lost_setup_output_remains_uncertain_until_noncreating_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(monkeypatch)
    path = _gate_path(tmp_path)
    setup = exchange_file_effect_gate(
        _LostOutputCarrier(),
        operation=GateControlOperation.SETUP,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert path.is_file()
    assert setup.observation is not None
    assert setup.observation.state is GateControlObservationState.UNCERTAIN
    assert setup.observation.binding is None
    lost_inspection = exchange_file_effect_gate(
        _LostCompleteOutputCarrier(),
        operation=GateControlOperation.INSPECT,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert lost_inspection.runtime_prerequisite.state is RuntimePrerequisiteState.UNKNOWN
    assert lost_inspection.observation is None
    inspection = exchange_file_effect_gate(
        LocalCarrier(),
        operation=GateControlOperation.INSPECT,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert inspection.observation is not None
    assert inspection.observation.state is GateControlObservationState.RESOLVED
    assert inspection.observation.binding is not None


def test_complete_runtime_prefix_loss_after_setup_is_explicitly_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(monkeypatch)
    path = _gate_path(tmp_path)
    setup = exchange_file_effect_gate(
        _LostCompleteOutputCarrier(),
        operation=GateControlOperation.SETUP,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert path.is_file()
    assert setup.dispatch is Dispatch.SENT
    assert setup.carrier_completion is not None and setup.carrier_completion.code == 0
    assert setup.carrier_failure is None
    assert setup.runtime_prerequisite.state is RuntimePrerequisiteState.UNKNOWN
    assert setup.observation is not None
    assert setup.observation.state is GateControlObservationState.UNCERTAIN
    assert setup.observation.binding is None
    inspection = exchange_file_effect_gate(
        LocalCarrier(),
        operation=GateControlOperation.INSPECT,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert inspection.observation is not None
    assert inspection.observation.state is GateControlObservationState.RESOLVED


def test_complete_runtime_prefix_loss_after_advance_is_explicitly_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(monkeypatch)
    path = _gate_path(tmp_path)
    setup = exchange_file_effect_gate(
        LocalCarrier(),
        operation=GateControlOperation.SETUP,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert setup.observation is not None
    binding = setup.observation.binding
    assert binding is not None
    proposed = replace(binding, proposed_generation=b"c" * 16)
    advance = exchange_file_effect_gate(
        _LostCompleteOutputCarrier(),
        operation=GateControlOperation.ADVANCE,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
        binding=proposed,
    )
    assert advance.dispatch is Dispatch.SENT
    assert advance.carrier_completion is not None and advance.carrier_completion.code == 0
    assert advance.carrier_failure is None
    assert advance.runtime_prerequisite.state is RuntimePrerequisiteState.UNKNOWN
    assert advance.observation is not None
    assert advance.observation.state is GateControlObservationState.UNCERTAIN
    assert advance.observation.binding is None
    inspection = exchange_file_effect_gate(
        LocalCarrier(),
        operation=GateControlOperation.INSPECT,
        path=str(path),
        guest=_GUEST,
        scope_name="vm-a",
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection("/usr/bin/python3"),
    )
    assert inspection.observation is not None
    assert inspection.observation.binding == replace(binding, generation=b"c" * 16)


@pytest.mark.parametrize(
    "plan",
    [
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
        IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT),
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE),
    ],
)
def test_complete_proxmox_body_stays_below_provider_bound(plan: IdentityPlan) -> None:
    argv = build_runtime_identity_helper_argv(
        plan,
        selection=runtime_selection("/usr/bin/python3"),
        fixed_source=FIXED_BUNDLE.bootstrap,
        nonce=_NONCE,
    )[0]
    invocation = PreparedInvocation(argv)
    representative = json.dumps(
        {"command": invocation.argv, "input-data": (FIXED_BUNDLE.prefix + b"x" * 32_768).decode("ascii")}
    ).encode("ascii")
    assert len(representative) < 65_536


def test_windows_ssh_command_contains_only_fixed_bootstrap() -> None:
    argv = build_runtime_identity_helper_argv(
        IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE),
        selection=runtime_selection("/usr/bin/python3"),
        fixed_source=FIXED_BUNDLE.bootstrap,
        nonce=_NONCE,
    )[0]
    connection = SSHConnection("host.example", "agent", Path("/keys/identity"), Path("/keys/known-hosts"))
    windows_command = subprocess.list2cmdline(build_ssh_argv(connection, PreparedInvocation(argv)))
    assert len(windows_command) < 32_767
    assert FIXED_BUNDLE.prefix.decode("ascii") not in windows_command
