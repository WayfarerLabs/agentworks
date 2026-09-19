"""Unit evidence for the private WSL2 carrier candidate."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Discard,
    Dispatch,
    ExitStatus,
    Failure,
    PreparedInvocation,
    Provenance,
    Retention,
)
from agentworks.execution.carriers import wsl2
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection


def connection(**changes: object) -> WSL2Connection:
    values: dict[str, object] = {
        "distribution": "debian-synthetic",
        "user": "delivery-user",
        "wsl_executable": r"C:\Windows\System32\wsl.exe",
    }
    values.update(changes)
    return WSL2Connection(**values)  # type: ignore[arg-type]


def process_result(
    *,
    started: bool = True,
    local_status: int | None = 0,
    exit_status: int | None = 0,
    stdout: CapturedOutput | None = None,
    stderr: CapturedOutput | None = None,
    failure: Failure | None = None,
) -> ProcessResult:
    return ProcessResult(
        started,
        local_status,
        exit_status,
        stdout or CapturedOutput(b"out", complete=True),
        stderr or CapturedOutput(b"err", complete=True),
        failure,
    )


def execute(monkeypatch: pytest.MonkeyPatch, result: ProcessResult) -> tuple[CarrierReport, MagicMock]:
    pump = MagicMock(return_value=result)
    monkeypatch.setattr(wsl2, "run_process", pump)
    report = WSL2Carrier(connection()).execute(
        PreparedInvocation(("/prepared/bootstrap",)), io=CarrierIO(), deadline=Deadline(None)
    )
    return report, pump


def test_construction_and_feature_inspection_are_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    pump = MagicMock()
    monkeypatch.setattr(wsl2, "run_process", pump)
    carrier = WSL2Carrier(connection())
    assert carrier.features == carrier.features
    assert not carrier.features.live_stdio
    assert not carrier.features.terminal
    pump.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"distribution": ""},
        {"distribution": None},
        {"distribution": "secret-canary\0"},
        {"user": ""},
        {"user": 23},
        {"user": "secret-canary\0"},
        {"wsl_executable": ""},
        {"wsl_executable": None},
        {"wsl_executable": "-wsl.exe"},
        {"wsl_executable": "secret-canary\n"},
    ],
)
def test_invalid_bound_values_fail_safely_without_delivery(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        connection(**changes)
    assert "secret-canary" not in str(raised.value)
    assert "secret-canary" not in repr(raised.value)
    assert raised.value.__cause__ is None


def test_literal_argv_is_forwarded_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    pump = MagicMock(return_value=process_result())
    monkeypatch.setattr(wsl2, "run_process", pump)
    carrier = WSL2Carrier(connection(distribution="Distro Name", user="user.name"))
    invocation = PreparedInvocation(
        ("/program", "", "'single' and \"double\"", "snowman-\u2603", "line\nbreak", r"C:\literal\backslash")
    )
    io = CarrierIO()
    deadline = Deadline(None)

    report = carrier.execute(invocation, io=io, deadline=deadline)

    assert report.completion == ExitStatus(code=0)
    pump.assert_called_once_with(
        [
            r"C:\Windows\System32\wsl.exe",
            "--distribution",
            "Distro Name",
            "--user",
            "user.name",
            "--exec",
            "/program",
            "",
            "'single' and \"double\"",
            "snowman-\u2603",
            "line\nbreak",
            r"C:\literal\backslash",
        ],
        io=io,
        deadline=deadline,
    )


@pytest.mark.parametrize(
    "io,retention",
    [
        (CarrierIO(), Retention.CAPTURED),
        (CarrierIO(output=Discard()), Retention.DISCARDED),
        (CarrierIO(sensitive=True), Retention.SUPPRESSED),
    ],
)
def test_expired_deadline_refuses_without_process_creation(
    monkeypatch: pytest.MonkeyPatch, io: CarrierIO, retention: Retention
) -> None:
    spawn = MagicMock(side_effect=AssertionError("expired execution attempted to create a process"))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    report = WSL2Carrier(connection()).execute(
        PreparedInvocation(("/prepared/bootstrap",)), io=io, deadline=Deadline.after(0)
    )
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.completion is None
    assert report.failure == Failure.DEADLINE
    assert report.stdout.retention == report.stderr.retention == retention
    assert report.stdout.provenance == Provenance.CARRIER_STDOUT
    assert report.stderr.provenance == Provenance.MIXED_STDERR
    spawn.assert_not_called()


def test_output_evidence_is_preserved_while_provenance_is_assigned(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = CapturedOutput(b"partial-out", complete=False, retention=Retention.CAPTURED)
    stderr = CapturedOutput(complete=True, retention=Retention.DISCARDED)
    report, _ = execute(monkeypatch, process_result(stdout=stdout, stderr=stderr, failure=Failure.OUTPUT_LIMIT))
    assert report.stdout.data == b"partial-out"
    assert not report.stdout.complete
    assert report.stdout.retention == Retention.CAPTURED
    assert report.stdout.provenance == Provenance.CARRIER_STDOUT
    assert report.stderr.data == b""
    assert report.stderr.complete
    assert report.stderr.retention == Retention.DISCARDED
    assert report.stderr.provenance == Provenance.MIXED_STDERR
    assert report.failure == Failure.OUTPUT_LIMIT


@pytest.mark.parametrize("status", [0, 1, 255])
def test_finite_guest_status_is_candidate_completion(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    report, pump = execute(monkeypatch, process_result(local_status=status, exit_status=status))
    assert report.dispatch == Dispatch.SENT
    assert report.completion == ExitStatus(code=status)
    assert report.local_status == status
    assert report.failure is None
    pump.assert_called_once()


@pytest.mark.parametrize("status", [None, -1, -15, 256, 4_294_967_295, 0xC0000005])
def test_status_outside_guest_exit_range_is_not_completion(monkeypatch: pytest.MonkeyPatch, status: int | None) -> None:
    report, _ = execute(monkeypatch, process_result(local_status=status, exit_status=status))
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert report.local_status == status
    assert report.failure == Failure.OBSERVATION


def test_local_cleanup_status_is_never_promoted_to_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    report, _ = execute(
        monkeypatch,
        process_result(local_status=-9, exit_status=None, failure=Failure.DEADLINE),
    )
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert report.local_status == -9
    assert report.failure == Failure.DEADLINE


@pytest.mark.parametrize("failure", [Failure.INPUT, Failure.OUTPUT, Failure.OUTPUT_LIMIT])
def test_io_failure_does_not_erase_independently_observed_completion(
    monkeypatch: pytest.MonkeyPatch, failure: Failure
) -> None:
    report, _ = execute(
        monkeypatch,
        process_result(local_status=23, exit_status=23, failure=failure),
    )
    assert report.dispatch == Dispatch.SENT
    assert report.completion == ExitStatus(code=23)
    assert report.failure == failure


def test_local_nonstart_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    report, _ = execute(
        monkeypatch,
        process_result(started=False, local_status=None, exit_status=None, failure=Failure.DISPATCH),
    )
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.completion is None
    assert report.local_status is None
    assert report.failure == Failure.DISPATCH


def test_import_and_construction_do_not_load_legacy_execution() -> None:
    script = r"""
import sys
class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('agentworks.ssh', 'agentworks.transports', 'agentworks.remote_exec',
                   'agentworks.harness_setup', 'agentworks.native_files', 'agentworks.plugins')
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Blocker())
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
WSL2Carrier(WSL2Connection('synthetic', 'delivery-user', r'C:\Windows\System32\wsl.exe'))
"""
    subprocess.run([sys.executable, "-I", "-c", script], check=True, capture_output=True, timeout=10)
