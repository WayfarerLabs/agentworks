"""Exact-source Python 3.11 checks for the private managed reader."""

from __future__ import annotations

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
