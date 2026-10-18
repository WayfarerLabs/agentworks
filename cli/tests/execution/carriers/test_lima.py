"""Local synthetic evidence for the private Lima carrier, not native Lima proof."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Discard,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    LiveInput,
    PreparedInvocation,
    Provenance,
    Retention,
    SinkOutput,
    TerminalInput,
)
from agentworks.execution.carriers import lima
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.lima import LimaCarrier, LimaConnection


def connection(**changes: object) -> LimaConnection:
    values: dict[str, object] = {"instance": "synthetic-instance", "limactl_executable": "/literal path/limactl"}
    values.update(changes)
    return LimaConnection(**values)  # type: ignore[arg-type]


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
    monkeypatch.setattr(lima, "run_process", pump)
    report = LimaCarrier(connection()).execute(
        PreparedInvocation(("/prepared/bootstrap",)),
        io=CarrierIO(),
        deadline=Deadline(None),
        custody=LocalDeliveryCustody(),
    )
    return report, pump


class Sink:
    def __init__(self) -> None:
        self.data = bytearray()

    def try_write(self, data: memoryview) -> int:
        self.data.extend(data)
        return len(data)


class Source:
    def try_read(self, limit: int) -> bytes:
        raise AssertionError("Unsupported source was read")


def test_construction_validation_and_features_are_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    pump = MagicMock()
    monkeypatch.setattr(lima, "run_process", pump)
    route = connection(instance="s" * 20 + "-" + "v" * 38)
    carrier = LimaCarrier(route)
    carrier.validate(PreparedInvocation(("/bootstrap",)), io=CarrierIO())
    assert carrier.connection is route
    assert carrier.features is carrier.features
    assert not carrier.features.live_stdio
    assert not carrier.features.terminal
    assert route.instance not in repr(route)
    assert route.limactl_executable not in repr(route)
    with pytest.raises(FrozenInstanceError):
        route.instance = "different"  # type: ignore[misc]
    pump.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"instance": ""},
        {"instance": None},
        {"instance": 3},
        {"instance": "-canary"},
        {"instance": "canary-"},
        {"instance": "canary/name"},
        {"instance": "canary name"},
        {"instance": "canary\n"},
        {"instance": "canary\0"},
        {"instance": "canary;touch"},
        {"instance": "CANARY"},
        {"instance": "canary-\u2603"},
        {"limactl_executable": ""},
        {"limactl_executable": None},
        {"limactl_executable": 3},
        {"limactl_executable": "-canary"},
        {"limactl_executable": "canary\n"},
        {"limactl_executable": "canary\0"},
        {"limactl_executable": "canary\x7f"},
    ],
)
def test_invalid_routes_have_safe_diagnostics(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        connection(**changes)
    assert "canary" not in str(raised.value)
    assert "canary" not in repr(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "io",
    [
        CarrierIO(input=LiveInput(Source())),
        CarrierIO(input=TerminalInput(0, 1, "xterm", Source()), output=SinkOutput(Sink(), Sink())),
        CarrierIO(output=SinkOutput(Sink(), Sink(), require_live=True)),
    ],
)
def test_validate_and_execute_refuse_unsupported_io_before_custody_effects(
    monkeypatch: pytest.MonkeyPatch, io: CarrierIO
) -> None:
    pump = MagicMock()
    monkeypatch.setattr(lima, "run_process", pump)
    custody = LocalDeliveryCustody()
    begin = MagicMock(side_effect=AssertionError("Refusal touched custody"))
    monkeypatch.setattr(custody, "begin_process", begin)
    carrier = LimaCarrier(connection())
    invocation = PreparedInvocation(("/bootstrap",))
    with pytest.raises(ValidationError):
        carrier.validate(invocation, io=io)
    with pytest.raises(ValidationError):
        carrier.execute(invocation, io=io, deadline=Deadline(None), custody=custody)
    assert custody.settled
    begin.assert_not_called()
    pump.assert_not_called()


def test_literal_argv_and_original_controls_reach_one_pump_call(monkeypatch: pytest.MonkeyPatch) -> None:
    pump = MagicMock(return_value=process_result())
    monkeypatch.setattr(lima, "run_process", pump)
    invocation = PreparedInvocation(
        ("=bootstrap", "", "'single' and \"double\"", "$(false);*", "\u2603", "line\nbreak")
    )
    io = CarrierIO(input=FiniteInput(b"\0\xff"), output=SinkOutput(Sink(), Sink()))
    deadline = Deadline.after(10)
    custody = LocalDeliveryCustody()
    LimaCarrier(connection()).execute(invocation, io=io, deadline=deadline, custody=custody)
    pump.assert_called_once_with(
        [
            "/literal path/limactl",
            "shell",
            "--tty=false",
            "--start=false",
            "--reconnect=false",
            "--preserve-env=false",
            "--workdir=/",
            "--shell=/bin/bash",
            "synthetic-instance",
            "--",
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            'exec -- "$@"',
            "agentworks-lima-bootstrap",
            *invocation.argv,
        ],
        io=io,
        deadline=deadline,
        custody=custody,
        env=None,
    )
    assert pump.call_args.kwargs["io"] is io
    assert pump.call_args.kwargs["deadline"] is deadline
    assert pump.call_args.kwargs["custody"] is custody


@pytest.mark.parametrize("status", [0, 1, 255, -1, -15, 256, None])
def test_only_natural_zero_status_is_candidate_completion(monkeypatch: pytest.MonkeyPatch, status: int | None) -> None:
    report, pump = execute(monkeypatch, process_result(local_status=status, exit_status=status))
    assert report.dispatch == (Dispatch.SENT if status == 0 else Dispatch.UNKNOWN)
    assert report.completion == (ExitStatus(code=0) if status == 0 else None)
    assert report.local_status == status
    assert report.failure == (None if status == 0 else Failure.OBSERVATION)
    pump.assert_called_once()


@pytest.mark.parametrize("local_status", [0, 1, 255, -9, None])
@pytest.mark.parametrize("failure", [None, Failure.DEADLINE, Failure.OBSERVATION])
def test_cleanup_status_is_never_completion(
    monkeypatch: pytest.MonkeyPatch, local_status: int | None, failure: Failure | None
) -> None:
    report, pump = execute(monkeypatch, process_result(local_status=local_status, exit_status=None, failure=failure))
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert report.local_status == local_status
    assert report.failure == (failure or Failure.OBSERVATION)
    pump.assert_called_once()


@pytest.mark.parametrize("failure", [None, Failure.DISPATCH, Failure.DEADLINE, Failure.OBSERVATION])
def test_clean_known_nonstart_preserves_failure(monkeypatch: pytest.MonkeyPatch, failure: Failure | None) -> None:
    report, _ = execute(
        monkeypatch, process_result(started=False, local_status=None, exit_status=None, failure=failure)
    )
    assert report.dispatch == (Dispatch.UNKNOWN if failure is Failure.OBSERVATION else Dispatch.NOT_SENT)
    assert report.completion is None
    assert report.failure == failure


def test_unsettled_custody_refuses_dispatch_and_unclean_nonstart_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    custody = LocalDeliveryCustody()
    custody.begin_process()
    pump = MagicMock()
    monkeypatch.setattr(lima, "run_process", pump)
    carrier = LimaCarrier(connection())
    invocation = PreparedInvocation(("/bootstrap",))
    with pytest.raises(StateError):
        carrier.execute(invocation, io=CarrierIO(), deadline=Deadline(None), custody=custody)
    pump.assert_not_called()

    clean = LocalDeliveryCustody()

    def uncertain_start(*args: object, **kwargs: object) -> ProcessResult:
        assert kwargs["custody"] is clean
        clean.begin_process()
        return process_result(started=False, local_status=None, exit_status=None, failure=Failure.DISPATCH)

    monkeypatch.setattr(lima, "run_process", uncertain_start)
    report = carrier.execute(invocation, io=CarrierIO(), deadline=Deadline(None), custody=clean)
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.failure == Failure.DISPATCH
    assert report.completion is None
    assert clean.close(Deadline.after(1))
    assert custody.close(Deadline.after(1))


@pytest.mark.parametrize("failure", [Failure.INPUT, Failure.OUTPUT, Failure.OUTPUT_LIMIT, Failure.DEADLINE])
@pytest.mark.parametrize("status", [0, 1])
def test_failure_and_stream_evidence_survive_status_interpretation(
    monkeypatch: pytest.MonkeyPatch, failure: Failure, status: int
) -> None:
    stdout = CapturedOutput(b"partial\0\xff", complete=False)
    stderr = CapturedOutput(complete=True, retention=Retention.SUPPRESSED)
    report, _ = execute(
        monkeypatch,
        process_result(exit_status=status, local_status=status, stdout=stdout, stderr=stderr, failure=failure),
    )
    assert report.dispatch == (Dispatch.SENT if status == 0 else Dispatch.UNKNOWN)
    assert report.completion == (ExitStatus(code=0) if status == 0 else None)
    assert report.failure == failure
    assert report.stdout.data == stdout.data
    assert not report.stdout.complete
    assert report.stdout.retention == Retention.CAPTURED
    assert report.stdout.provenance == Provenance.CARRIER_STDOUT
    assert report.stderr.data == b""
    assert report.stderr.complete
    assert report.stderr.retention == Retention.SUPPRESSED
    assert report.stderr.provenance == Provenance.MIXED_STDERR


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(42)])
def test_control_interruption_propagates_with_original_custody(
    monkeypatch: pytest.MonkeyPatch, interruption: BaseException
) -> None:
    custody = LocalDeliveryCustody()

    def interrupted(*args: object, **kwargs: object) -> ProcessResult:
        assert kwargs["custody"] is custody
        custody.begin_process()
        raise interruption

    pump = MagicMock(side_effect=interrupted)
    monkeypatch.setattr(lima, "run_process", pump)
    with pytest.raises(type(interruption)) as raised:
        LimaCarrier(connection()).execute(
            PreparedInvocation(("/bootstrap",)), io=CarrierIO(), deadline=Deadline.after(1), custody=custody
        )
    assert raised.value is interruption
    assert not custody.settled
    assert custody.close(Deadline.after(1))
    pump.assert_called_once()


@pytest.mark.parametrize(
    "io,retention",
    [
        (CarrierIO(), Retention.CAPTURED),
        (CarrierIO(output=Discard()), Retention.DISCARDED),
        (CarrierIO(sensitive=True), Retention.SUPPRESSED),
    ],
)
def test_expired_deadline_refuses_before_process_creation(
    monkeypatch: pytest.MonkeyPatch, io: CarrierIO, retention: Retention
) -> None:
    spawn = MagicMock(side_effect=AssertionError("Expired invocation spawned a process"))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    custody = LocalDeliveryCustody()
    report = LimaCarrier(connection()).execute(
        PreparedInvocation(("/bootstrap",)), io=io, deadline=Deadline.after(0), custody=custody
    )
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.completion is None
    assert report.failure == Failure.DEADLINE
    assert report.stdout.retention == report.stderr.retention == retention
    assert custody.settled
    spawn.assert_not_called()


@pytest.fixture
def synthetic_limactl(tmp_path: Path) -> Path:
    """Echo local argv and stdin only; this does not emulate Lima's guest shell."""
    path = tmp_path / "synthetic limactl"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "data = sys.stdin.buffer.read()\n"
        "os.write(1, json.dumps(sys.argv[1:]).encode() + b'\\n' + data)\n"
        "os.write(2, b'stderr\\x00\\xff')\n"
    )
    path.chmod(0o700)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="Synthetic executable uses a POSIX shebang")
@pytest.mark.parametrize("mode", ["capture", "eof", "discard", "sensitive", "sink"])
def test_owned_local_process_preserves_literal_argv_binary_io_and_retention(synthetic_limactl: Path, mode: str) -> None:
    invocation = PreparedInvocation(("=bootstrap", "space value", "$(exit 1);*", "\u2603", "line\nbreak"))
    stdout, stderr = Sink(), Sink()
    io = CarrierIO(
        input=FiniteInput(b"input\0\xff") if mode != "eof" else CarrierIO().input,
        output=SinkOutput(stdout, stderr) if mode == "sink" else Discard() if mode == "discard" else Capture(),
        sensitive=mode == "sensitive",
    )
    custody = LocalDeliveryCustody()
    report = LimaCarrier(connection(limactl_executable=str(synthetic_limactl))).execute(
        invocation, io=io, deadline=Deadline.after(5), custody=custody
    )
    assert report.dispatch == Dispatch.SENT
    assert report.completion == ExitStatus(code=0)
    assert report.failure is None
    assert report.stdout.complete and report.stderr.complete
    assert custody.settled
    expected_prefix = [
        "shell",
        "--tty=false",
        "--start=false",
        "--reconnect=false",
        "--preserve-env=false",
        "--workdir=/",
        "--shell=/bin/bash",
        "synthetic-instance",
        "--",
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        'exec -- "$@"',
        "agentworks-lima-bootstrap",
    ]
    expected_out = json.dumps([*expected_prefix, *invocation.argv]).encode() + b"\n"
    if mode != "eof":
        expected_out += b"input\0\xff"
    if mode == "sink":
        assert stdout.data == expected_out
        assert stderr.data == b"stderr\0\xff"
        assert report.stdout.retention == report.stderr.retention == Retention.DELIVERED
        assert report.stdout.data == report.stderr.data == b""
    elif mode in {"sensitive", "discard"}:
        assert report.stdout.data == report.stderr.data == b""
        expected_retention = Retention.SUPPRESSED if mode == "sensitive" else Retention.DISCARDED
        assert report.stdout.retention == report.stderr.retention == expected_retention
    else:
        assert report.stdout.data == expected_out
        assert report.stderr.data == b"stderr\0\xff"


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="Requires local Bash for trampoline proof")
@pytest.mark.parametrize("executable_name", ["=bootstrap", "NAME=value", "-bootstrap", "absolute=bootstrap"])
def test_fixed_trampoline_executes_literal_executable_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable_name: str
) -> None:
    """Execute the trampoline locally, without modeling Lima's remote shell."""
    shim = tmp_path / "synthetic limactl executor"
    shim.write_text(
        f"#!{sys.executable}\nimport os, sys\nargv = sys.argv[sys.argv.index('--') + 1:]\nos.execv(argv[0], argv)\n"
    )
    shim.chmod(0o700)
    program = tmp_path / executable_name
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "os.write(1, json.dumps(sys.argv[1:]).encode() + b'\\n' + sys.stdin.buffer.read())\n"
    )
    program.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    literal_args = ("", "space value", "$(exit 1);*", "\u2603", "line\nbreak", "NAME=arg")
    executable = str(program) if executable_name == "absolute=bootstrap" else executable_name
    custody = LocalDeliveryCustody()
    report = LimaCarrier(connection(limactl_executable=str(shim))).execute(
        PreparedInvocation((executable, *literal_args)),
        io=CarrierIO(input=FiniteInput(b"input\0\xff")),
        deadline=Deadline.after(5),
        custody=custody,
    )
    assert report.dispatch == Dispatch.SENT
    assert report.completion == ExitStatus(code=0)
    assert report.failure is None
    assert report.stdout.complete and report.stderr.complete
    assert report.stdout.data == json.dumps(literal_args).encode() + b"\ninput\0\xff"
    assert report.stderr.data == b""
    assert custody.settled
