"""Exact-source Python 3.11 checks for the private managed reader."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_observation_guest as guest
from agentworks.execution._file_wire import FileRecord, FileRecordKind
from agentworks.execution._file_wire_reader import FileRecordReader
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_observation_bundle import FIXED_BUNDLE
from agentworks.execution._managed_observation_protocol import (
    MAX_OUTPUT_PAGE_BYTES,
    ControllerState,
    ManagedObservationRequest,
    ManagedOperation,
    ManagedResultControl,
    decode_result,
    encode_request,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import CarrierIO, FiniteInput
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection

NONCE = "b" * 32
RUN = "a" * 32
GUEST = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)


def _launch() -> bytes:
    return wire.encode_fact(
        {
            "version": 1,
            "kind": "launch",
            "run_id": RUN,
            "unit": f"agw-managed-{RUN}.service",
            "target": {
                "kind": "vm",
                "name": "vm-one",
                "incarnation": "v1:" + "c" * 64,
                "boot_id": vm_guest_boot_id(GUEST),
            },
            "workload": {"euid": 1001, "egid": 1001, "groups": [1001]},
            "shell": {"requested": None, "resolved_executable": None, "login": False, "interactive": False},
            "owner": {"kind": "resource", "owner_id": "session-7"},
            "lifetime": "independent",
            "profile_revision": 1,
            "receipt_namespace": "agentworks-managed-runs-v1",
            "receipt_protocol_version": 1,
        }
    )


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_exact_bundle_runs_without_installed_package(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).exists():
        pytest.skip("interpreter unavailable")
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", FIXED_BUNDLE.bootstrap, NONCE],
        input=FIXED_BUNDLE.prefix + b"invalid-request",
        capture_output=True,
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert b"FAILED" in result.stdout and b"FINISHED" in result.stdout


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_exact_source_codec_parity(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).exists():
        pytest.skip("interpreter unavailable")
    source = build_helper_modules(
        "_agw_observation_parity",
        (
            "_helper_identity",
            "_managed_job_wire",
            "_managed_lease_wire",
            "_managed_job_request",
            "_managed_job_store",
            "_file_wire",
            "_vm_guest_identity_protocol",
            "_managed_observation_protocol",
        ),
    )
    source += (
        "p=sys.modules['_agw_observation_parity._managed_observation_protocol']\n"
        "data=bytes.fromhex(sys.argv[1])\n"
        "assert p.encode_request(p.decode_request(data))==data\n"
        "result=p.encode_result(p.ManagedResultControl("
        "(sys.modules['_agw_observation_parity._managed_job_store'].FactName.LAUNCH,)))\n"
        "assert p.decode_result(result).facts[0].value=='launch'\n"
    )
    request = encode_request(
        ManagedObservationRequest(NONCE, ManagedOperation.OBSERVE, _launch(), IdentityExpectation(0, 0, (0,)), GUEST)
    )
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source, request.hex()],
        capture_output=True,
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b""


def test_guest_writes_bounded_data_records_then_terminal() -> None:
    records: list[tuple[FileRecordKind, bytes]] = []

    class Writer:
        def write(self, kind: FileRecordKind, body: bytes) -> None:
            records.append((kind, body))

    prepared = guest._PreparedResult(
        ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
        (_launch(), b"end-fact"),
        b"x" * 5000,
    )
    guest._write_result(Writer(), prepared)  # type: ignore[arg-type]
    assert [kind for kind, _ in records] == [
        FileRecordKind.RESULT,
        FileRecordKind.DATA,
        FileRecordKind.DATA,
        FileRecordKind.DATA,
        FileRecordKind.DATA,
        FileRecordKind.FINISHED,
    ]
    assert [len(body) for kind, body in records if kind is FileRecordKind.DATA][-2:] == [4096, 904]


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
@pytest.mark.parametrize("operation", list(ManagedOperation))
def test_packed_helper_queries_finite_child_and_preserves_borrowed_store_anchor(
    interpreter: str,
    operation: ManagedOperation,
    tmp_path: Path,
) -> None:
    from agentworks.execution._managed_job_store import Stream

    from .test_managed_controller_observation import _properties

    if not Path(interpreter).exists():
        pytest.skip("interpreter unavailable")
    request = ManagedObservationRequest(
        NONCE,
        operation,
        _launch(),
        IdentityExpectation(0, 0, (0,)),
        GUEST,
        Stream.STDOUT if operation is ManagedOperation.READ_OUTPUT else None,
        0 if operation is ManagedOperation.READ_OUTPUT else None,
        262144 if operation is ManagedOperation.READ_OUTPUT else None,
    )
    setup = (
        "g=sys.modules['_agw_managed_observation._managed_observation_guest']\n"
        "s=sys.modules['_agw_managed_observation._managed_job_store']\n"
        "n=sys.modules['_agw_managed_observation._managed_controller_guest']\n"
        "request=g._read_request()\n"
        "g._read_request=lambda:request\n"
        "g._identity=lambda:request.guest\n"
        "g.matches_current_identity=lambda expected:True\n"
        "os.chmod(sys.argv[2],0o700)\n"
        "anchor=os.open(sys.argv[2],os.O_RDONLY|os.O_DIRECTORY)\n"
        "def store(run_id):\n"
        " return s.ManagedJobStore(run_id,_namespace='managed',_owner_uid=os.getuid(),_anchor_fd=anchor)\n"
        "g.ManagedJobStore=store\n"
        "with store(" + repr(RUN) + ") as target:target.publish_fact(s.FactName.LAUNCH,request.expected_launch)\n"
        "calls=[]\n"
        "def argv(run_id):\n"
        " calls.append(run_id)\n"
        " return (sys.executable,'-I','-S','-c'," + repr("import os; os.write(1," + repr(_properties()) + ")") + ")\n"
        "n._query_argv=argv\n"
        "try:\n"
        " result=g.main(sys.argv[1])\n"
        " os.fstat(anchor)\n"
        " assert calls==(" + repr([RUN] if operation is ManagedOperation.OBSERVE else []) + ")\n"
        "finally:os.close(anchor)\n"
        "raise SystemExit(result)\n"
    )
    source = FIXED_BUNDLE.bootstrap.rsplit("raise SystemExit(", 1)[0] + setup
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source, NONCE, str(tmp_path)],
        input=FIXED_BUNDLE.prefix + encode_request(request),
        capture_output=True,
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stderr == b""
    records: list[FileRecord] = []
    reader = FileRecordReader(NONCE, records.append)
    reader.try_write(memoryview(result.stdout))
    reader.finish()
    assert reader.error is None
    assert [record.kind for record in records] == [FileRecordKind.RESULT, FileRecordKind.DATA, FileRecordKind.FINISHED]
    control = decode_result(records[0].body)
    assert (control.controller.state if control.controller is not None else None) is (
        ControllerState.RUNNING if operation is ManagedOperation.OBSERVE else None
    )


def test_complete_observation_fits_exact_qga_envelope() -> None:
    from .test_managed_observation import ScriptedCarrier, _exchange, _records

    carrier = ScriptedCarrier(
        lambda request: _records(request.nonce, ManagedResultControl((FactName.LAUNCH,)), (_launch(),))
    )
    _exchange(carrier)
    assert carrier.invocation is not None and isinstance(carrier.io, CarrierIO)
    assert isinstance(carrier.io.input, FiniteInput)
    qga = ProxmoxCarrier(ProxmoxConnection("https://pve.example", "node", 101, "operator!token", "synthetic"))
    qga.validate(carrier.invocation, io=carrier.io)


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_standalone_closed_binary_range_fits_complete_fake_qga_exchange(
    interpreter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
    from agentworks.execution._managed_job_store import Stream
    from agentworks.execution._managed_observation_exchange import (
        ManagedObservationState,
        execute_managed_read,
        prepare_managed_read,
    )
    from agentworks.execution._managed_observation_protocol import decode_request
    from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
    from agentworks.execution.carrier import Deadline, SinkOutput
    from agentworks.execution.carriers.proxmox import _ProxmoxWire

    if not Path(interpreter).exists():
        pytest.skip("interpreter unavailable")
    deadline = Deadline.after(10)
    prepared = prepare_managed_read(
        operation=ManagedOperation.READ_OUTPUT,
        expected_launch=_launch(),
        stream=Stream.STDOUT,
        offset=MAX_OUTPUT_PAGE_BYTES + 3,
        count=MAX_OUTPUT_PAGE_BYTES,
        plan=IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT),
        deadline=deadline,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, interpreter),
        guest=GUEST,
    )
    assert (
        prepared.io is not None
        and isinstance(prepared.io.input, FiniteInput)
        and isinstance(prepared.io.output, SinkOutput)
    )
    request_bytes = prepared.io.input.data
    request = decode_request(request_bytes[len(FIXED_BUNDLE.prefix) :])
    bound = prepared.io.output.required_complete_stdout_bytes
    assert bound is not None and bound <= 1048576
    setup = """
assert 'agentworks' not in sys.modules
g=sys.modules['_agw_managed_observation._managed_observation_guest']
s=sys.modules['_agw_managed_observation._managed_job_store']
w=sys.modules['_agw_managed_observation._managed_job_wire']
request=g._read_request()
g._read_request=lambda:request
g._identity=lambda:request.guest
g.matches_current_identity=lambda expected:True
os.chmod(sys.argv[2],0o700)
anchor=os.open(sys.argv[2],os.O_RDONLY|os.O_DIRECTORY)
def store(run_id):
 return s.ManagedJobStore(run_id,_namespace='managed',_owner_uid=os.getuid(),_anchor_fd=anchor)
g.ManagedJobStore=store
try:
 with store('a'*32) as target:
  target.publish_fact(s.FactName.LAUNCH,request.expected_launch)
  content=bytes(range(256))*3072+b'tail'
  writer=target.open_capture(s.Stream.STDOUT,len(content))
  writer.write(content)
  captured=writer.finish()
  launch=w.decode_fact(request.expected_launch)
  target.publish_fact(s.FactName.STDOUT_END,w.encode_fact(dict(version=1,kind='stream-end',run_id=launch['run_id'],unit=launch['unit'],receipt_sha256=w.launch_sha256(launch),stream='stdout',retained_bytes=captured.length,retained_sha256=captured.sha256,disposition=captured.disposition.value)))
 result=g.main(sys.argv[1])
 os.fstat(anchor)
finally:os.close(anchor)
raise SystemExit(result)
"""
    source = FIXED_BUNDLE.bootstrap.rsplit("raise SystemExit(", 1)[0] + setup
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source, request.nonce, str(tmp_path)],
        input=request_bytes,
        capture_output=True,
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == b""
    payload = f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + result.stdout
    assert len(payload) <= bound
    status = {"exited": True, "exitcode": 0, "out-data": payload.decode("ascii")}
    assert len(json.dumps({"data": status}).encode("ascii")) <= 8 * 1024 * 1024
    calls: list[tuple[str, str]] = []

    def exchange(_self, method, suffix, *, custody, timeout, body=None):  # type: ignore[no-untyped-def]
        assert custody.settled and timeout is not None and timeout > 0
        calls.append((method, suffix))
        if method == "POST":
            assert body is not None and len(body) <= 65536
            post = json.loads(body)
            assert post["input-data"].encode("ascii") == request_bytes
            return {"pid": 71}
        assert suffix == "exec-status?pid=71"
        return status

    monkeypatch.setattr(_ProxmoxWire, "request", exchange)
    native = ProxmoxCarrier(ProxmoxConnection("https://pve.example", "node", 101, "operator!token", "synthetic"))

    class Borrow:
        def execute(self, invocation, *, io, deadline):  # type: ignore[no-untyped-def]
            return native.execute(invocation, io=io, deadline=deadline, custody=LocalDeliveryCustody())

    candidate = execute_managed_read(Borrow(), prepared, deadline=deadline)  # type: ignore[arg-type]
    assert calls == [("POST", "exec"), ("GET", "exec-status?pid=71")]
    assert candidate.observation is not None and candidate.observation.state is ManagedObservationState.AVAILABLE
    expected = (bytes(range(256)) * 3072 + b"tail")[MAX_OUTPUT_PAGE_BYTES + 3 : 2 * MAX_OUTPUT_PAGE_BYTES + 3]
    assert candidate.observation.output == expected
    assert len(expected) == MAX_OUTPUT_PAGE_BYTES
    end = wire.decode_fact(candidate.observation.facts[1][1])
    assert end["retained_bytes"] == 3 * MAX_OUTPUT_PAGE_BYTES + 4
    assert end["retained_sha256"] != hashlib.sha256(expected).hexdigest()
    assert prepared.io is None and prepared._collector is None and prepared._reader is None
