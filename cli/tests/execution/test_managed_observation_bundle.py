"""Exact-source Python 3.11 checks for the private managed reader."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_observation_guest as guest
from agentworks.execution._file_wire import FileRecordKind
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_observation_bundle import FIXED_BUNDLE
from agentworks.execution._managed_observation_protocol import (
    ManagedObservationRequest,
    ManagedOperation,
    ManagedResultControl,
    encode_request,
)

NONCE = "b" * 32
RUN = "a" * 32


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
                "boot_id": "00000000-0000-4000-8000-000000000001",
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
            "_managed_job_request",
            "_managed_job_store",
            "_file_wire",
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
        ManagedObservationRequest(NONCE, ManagedOperation.OBSERVE, _launch(), IdentityExpectation(0, 0, (0,)))
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
