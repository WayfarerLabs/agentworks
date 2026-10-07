"""Python 3.11 exact-source lease helper and source-compaction behavior."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution import _managed_lease_bundle as bundle
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._managed_lease_protocol import ClockObservation, LeaseRequest, encode_request, encode_result
from agentworks.execution._managed_lease_wire import sampled_lease

from .test_managed_operation_lease import launch
from .test_managed_start import GUEST, NONCE, ROOT


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_exact_lease_bundle_refuses_malformed_input_without_installed_package(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", bundle.FIXED_BUNDLE.bootstrap, NONCE],
        input=bundle.FIXED_BUNDLE.prefix + b"invalid",
        cwd=tmp_path,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stderr == b""
    assert b"FAILED" in result.stdout and b"FINISHED" in result.stdout


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
@pytest.mark.parametrize("compact", [False, True])
def test_trusted_source_compaction_preserves_lease_codecs(interpreter: str, tmp_path: Path, compact: bool) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    source = build_helper_modules("_agw_lease_parity", bundle._MODULES, compact=compact)
    source += (
        "p=sys.modules['_agw_lease_parity._managed_lease_protocol']\n"
        "data=sys.stdin.buffer.read()\n"
        "request=p.decode_request(data)\n"
        "assert p.encode_request(request)==data\n"
        "assert request.lease.expires_ns==request.lease.sampled_ns+60000000000\n"
        "assert type(p.decode_result(p.encode_result(p.ClockObservation(123)))) is p.ClockObservation\n"
        "assert type(p.decode_result(p.encode_result(p.LeasePublication(123)))) is p.LeasePublication\n"
        "assert 'agentworks' not in sys.modules\n"
    )
    request = LeaseRequest(NONCE, ROOT, GUEST, launch(), sampled_lease(launch(), 1000))
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source],
        input=encode_request(request),
        cwd=tmp_path,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_packed_clock_helper_returns_only_sample_without_store(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    source = build_helper_modules("_agw_lease_clock", bundle._MODULES)
    source += (
        "g=sys.modules['_agw_lease_clock._managed_lease_guest']\n"
        "p=sys.modules['_agw_lease_clock._managed_lease_protocol']\n"
        "w=sys.modules['_agw_lease_clock._managed_lease_wire']\n"
        "request=p.decode_request(sys.stdin.buffer.read())\n"
        "g._read_request=lambda:request\n"
        "g.matches_current_identity=lambda identity:True\n"
        "g._identity=lambda:request.guest\n"
        "w.time.clock_gettime_ns=lambda clock:123\n"
        "def forbidden(*args,**kwargs):raise AssertionError('store opened')\n"
        "g.ManagedJobStore=forbidden\n"
        "assert g.main(request.nonce)==0\n"
    )
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source],
        input=encode_request(LeaseRequest(NONCE, ROOT, GUEST)),
        cwd=tmp_path,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stderr == b""
    assert result.stdout == (
        encode_file_record(NONCE, FileRecord(0, FileRecordKind.RESULT, encode_result(ClockObservation(123))))
        + encode_file_record(NONCE, FileRecord(1, FileRecordKind.FINISHED, b""))
    )
    assert not list(tmp_path.iterdir())
