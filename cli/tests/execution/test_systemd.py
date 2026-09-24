"""Conformance tests for the private Linux systemd foreground experiment."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import systemd
from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader, encode_frame
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._inline_control import (
    FailureCode,
    FailureFact,
    FailurePhase,
    StreamEnd,
    StreamName,
    StreamRetention,
    WaitFact,
    WaitKind,
    empty_body,
    encode_failure,
    encode_stream_end,
    encode_wait,
)
from agentworks.execution._inline_observer import InlineObservation, InlineObserver, ObservationError
from agentworks.execution._inline_request import OutputMode
from agentworks.execution.carrier import (
    ByteSink,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.models import Command
from agentworks.execution.systemd import (
    BoundaryState,
    ManagedRun,
    PrerequisiteState,
    check_systemd_prerequisites,
    prepare_managed_candidate,
    run_managed_candidate,
)

_LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="the private systemd candidate requires Linux")

_SUPERVISOR_DRIVER = r"""
import builtins, io, os as real_os, sys, time, types

source = sys.argv.pop()
root = sys.argv[2].encode('ascii')
event_fds = set()
kill_fds = {}
shim = types.ModuleType('os')
for name in dir(real_os):
 setattr(shim, name, getattr(real_os, name))

def raw_path(path):
 return path.encode('ascii') if isinstance(path, str) else path

def remove_membership(path):
 raw = raw_path(path)
 if raw.endswith(b'/cgroup.procs') and real_os.path.exists(root + b'/.agw-test-remove-membership'):
  real_os.unlink(raw)
  return True
 return False

def deny_kill_write():
 return real_os.path.exists(root + b'/.agw-test-deny-kill-write')

def mkdir(path, mode=0o777):
 real_os.mkdir(path, mode)
 raw = raw_path(path)
 if raw.startswith(root + b'/service/agw-workload-'):
  for name in (b'cgroup.events', b'cgroup.procs', b'cgroup.kill'):
   fd = real_os.open(raw + b'/' + name, real_os.O_CREAT | real_os.O_RDWR, 0o600)
   real_os.close(fd)

def open(path, flags, mode=0o777):
 raw = raw_path(path)
 remove_membership(raw)
 fd = real_os.open(path, flags, mode)
 if raw.endswith(b'/cgroup.events'):
  event_fds.add(fd)
 if raw.endswith(b'/cgroup.kill'):
  kill_fds[fd] = raw.rsplit(b'/', 1)[0] + b'/cgroup.events'
 return fd

def read(fd, count):
 data = real_os.read(fd, count)
 if fd not in event_fds or data:
  return data
 deadline = time.monotonic() + 2
 while not data and time.monotonic() < deadline:
  time.sleep(.001)
  real_os.lseek(fd, 0, real_os.SEEK_SET)
  data = real_os.read(fd, count)
 return data

def write(fd, data):
 if fd in kill_fds:
  if deny_kill_write():
   raise OSError
  written = real_os.write(fd, data)
  events_fd = real_os.open(kill_fds[fd], real_os.O_WRONLY | real_os.O_TRUNC)
  try: real_os.write(events_fd, b'populated 0\n')
  finally: real_os.close(events_fd)
  return written
 return real_os.write(fd, data)

real_open = builtins.open
def builtin_open(path, *args, **kwargs):
 if raw_path(path) == b'/proc/self/cgroup':
  return io.BytesIO(b'0::/service\n')
 if remove_membership(path):
  raise FileNotFoundError
 return real_open(path, *args, **kwargs)

shim.mkdir = mkdir
shim.open = open
shim.read = read
shim.write = write
sys.modules['os'] = shim
namespace = {'__name__': '__main__', '__builtins__': dict(vars(builtins))}
namespace['__builtins__']['open'] = builtin_open
exec(compile(source, '<agw-supervisor>', 'exec'), namespace)
"""


def _write(sink: ByteSink, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)
        assert written is not None and 0 < written <= len(remaining)
        remaining = remaining[written:]


def _terminal_observation(
    run: ManagedRun,
    *,
    wait: WaitFact | None = None,
    failure: FailureFact | None = None,
    carrier_stdout_complete: bool = True,
) -> InlineObservation:
    """Build one grammar-valid terminal transcript through the real observer."""
    if wait is None:
        wait = WaitFact(WaitKind.EXIT, 0)
    observer = InlineObserver(OutputMode.CAPTURE, 4_096)
    reader = FrameReader(run.token, observer.accept)

    def record(sequence: int, kind: FrameKind, body: bytes) -> bytes:
        return encode_frame(run.token, Frame(sequence, kind, body))

    def stream_end(stream: StreamName) -> bytes:
        return encode_stream_end(
            StreamEnd(stream, 0, hashlib.sha256(b"").hexdigest(), True, False, StreamRetention.CAPTURED)
        )

    records = [
        record(0, FrameKind.LAUNCHING, empty_body()),
        record(1, FrameKind.STREAM_END, stream_end(StreamName.STDOUT)),
        record(2, FrameKind.STREAM_END, stream_end(StreamName.STDERR)),
        record(3, FrameKind.WAITED, encode_wait(wait)),
    ]
    if failure is not None:
        records.append(record(4, FrameKind.FAILED, encode_failure(failure)))
    records.append(record(5 if failure is not None else 4, FrameKind.FINISHED, empty_body()))
    _write(reader, b"".join(records))
    reader.finish()
    return observer.finish(reader.error, carrier_stdout_complete=carrier_stdout_complete)


def _report(stdout: bytes = b"", *, status: int = 0, dispatch: Dispatch = Dispatch.SENT) -> CarrierReport:
    return CarrierReport(
        dispatch,
        ExitStatus(code=status) if dispatch is Dispatch.SENT else None,
        status,
        CapturedOutput(stdout, True),
        CapturedOutput(complete=True),
    )


@dataclass
class SystemdFake:
    marker: str = "empty:exit:0"
    launch: bytes = b""
    dispatch: Dispatch = Dispatch.SENT
    completion: int = 0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        pass

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        del deadline
        argv = invocation.argv
        self.calls.append(argv)
        if argv[:5] == ("/usr/bin/python3", "-I", "-S", "-B", "-c") and "AGW_MANAGED_CONTROL_1" in argv[5]:
            return _report(b"AGW_MANAGED_CONTROL_1:root\n")
        if argv == ("/usr/bin/systemd-run", "--version"):
            return _report(b"systemd 252 (252.39-1~deb12u1)\n")
        if argv[:2] == ("/usr/bin/test", "-f"):
            return _report()
        if argv[0] == "/usr/bin/systemd-run":
            if self.dispatch is not Dispatch.SENT:
                return _report(dispatch=self.dispatch)
            assert isinstance(io.output, SinkOutput)
            _write(io.output.stdout, self.launch)
            unit = next(value for value in argv if value.startswith("--unit="))
            token = unit.removeprefix("--unit=agw-managed-").removesuffix(".service")
            _write(io.output.stderr, f"AGW_MANAGED_BOUNDARY_1:{token}:{self.marker}\n".encode())
            return CarrierReport(
                Dispatch.SENT,
                ExitStatus(code=self.completion),
                self.completion,
                CapturedOutput(complete=True, retention=Retention.DELIVERED),
                CapturedOutput(complete=True, retention=Retention.DELIVERED),
            )
        raise AssertionError(argv)


@pytest.fixture
def identity() -> IdentityExpectation:
    return IdentityExpectation(1001, 1002, (1002, 1003))


def _prepared(identity: IdentityExpectation, *, sensitive: bool = False):
    return prepare_managed_candidate(
        Command(("/bin/echo", "private-payload-canary")),
        identity=identity,
        prerequisite=PrerequisiteState.READY,
        stdin=b"private-input-canary",
        sensitive=sensitive,
    )


def test_fixed_unit_shape_has_no_stdio_properties_or_caller_controls(identity: IdentityExpectation) -> None:
    prepared = _prepared(identity)
    argv = prepared.invocation.argv
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--quiet" in argv
    assert "--pipe" in argv and "--wait" in argv and "--collect" in argv
    assert "--service-type=exec" in argv
    assert "--property=Delegate=yes" in argv
    assert "--property=KillMode=control-group" in argv
    assert "--property=ExitType=main" in argv
    assert not any("StandardInput" in value or "StandardOutput" in value or "StandardError" in value for value in argv)
    assert "--property=SupplementaryGroups=1002 1003" in argv
    assert "private-payload-canary" not in argv
    assert "private-input-canary" not in repr(prepared)
    with pytest.raises(TypeError):
        prepare_managed_candidate(  # type: ignore[call-arg]
            Command(("/bin/true",)),
            identity=identity,
            prerequisite=PrerequisiteState.READY,
            unit="untrusted.service",
        )


def test_fresh_identity_and_malformed_boundary_marker_are_not_reused(identity: IdentityExpectation) -> None:
    first, second = _prepared(identity), _prepared(identity)
    assert first.run != second.run
    assert first.run.unit.startswith("agw-managed-")
    fake = SystemdFake(marker="not-a-boundary-marker")
    result = run_managed_candidate(fake, first, deadline=Deadline.after(1))
    assert result.boundary.state is BoundaryState.INVALID
    assert not result.complete


@pytest.mark.parametrize("marker", ("empty:exit:256", "empty:signal:0", "empty:unknown:1"))
def test_boundary_marker_rejects_invalid_helper_status_without_raising(
    identity: IdentityExpectation, marker: str
) -> None:
    result = run_managed_candidate(SystemdFake(marker=marker), _prepared(identity), deadline=Deadline.after(1))
    assert result.boundary.state is BoundaryState.INVALID
    assert result.boundary.helper_completion is None


def test_boundary_marker_preserves_unknown_helper_status(identity: IdentityExpectation) -> None:
    result = run_managed_candidate(
        SystemdFake(marker="empty:unknown:0"), _prepared(identity), deadline=Deadline.after(1)
    )
    assert result.boundary.state is BoundaryState.EMPTY
    assert result.boundary.helper_completion is None
    assert not result.complete


def test_boundary_marker_survives_asymmetric_stdout_output_failure() -> None:
    run = ManagedRun.fresh()
    sink = systemd._BoundaryMarkerSink(run)
    _write(sink, f"AGW_MANAGED_BOUNDARY_1:{run.token}:empty:exit:0\n".encode())
    report = CarrierReport(
        Dispatch.SENT,
        ExitStatus(code=0),
        0,
        CapturedOutput(complete=False, retention=Retention.DELIVERED),
        CapturedOutput(complete=True, retention=Retention.DELIVERED),
        Failure.OUTPUT,
    )
    boundary = sink.finish(report)
    assert boundary.state is BoundaryState.EMPTY
    assert boundary.failure is Failure.OUTPUT
    helper = _terminal_observation(run)
    result = systemd.ManagedForegroundResult(
        run,
        PrerequisiteState.READY,
        Dispatch.SENT,
        ExitStatus(code=0),
        Failure.OUTPUT,
        helper,
        boundary,
    )
    assert not result.complete


def test_unknown_payload_wait_is_not_complete() -> None:
    run = ManagedRun.fresh()
    helper = _terminal_observation(run, wait=WaitFact(WaitKind.UNKNOWN, None))
    result = systemd.ManagedForegroundResult(
        run,
        PrerequisiteState.READY,
        Dispatch.SENT,
        ExitStatus(code=0),
        None,
        helper,
        systemd.BoundaryObservation(run, BoundaryState.EMPTY, ExitStatus(code=0), Dispatch.SENT, ExitStatus(code=0)),
    )
    assert not result.complete


@pytest.mark.parametrize(
    ("phase", "code"),
    (
        (FailurePhase.OBSERVE, FailureCode.INPUT),
        (FailurePhase.OBSERVE, FailureCode.OUTPUT),
        (FailurePhase.OBSERVE, FailureCode.OBSERVATION),
        (FailurePhase.CLEANUP, FailureCode.RESOURCE),
    ),
)
def test_valid_post_wait_helper_failure_remains_terminal_lifecycle_evidence(
    phase: FailurePhase, code: FailureCode
) -> None:
    run = ManagedRun.fresh()
    failure = FailureFact(phase, code)
    helper = _terminal_observation(run, failure=failure)
    result = systemd.ManagedForegroundResult(
        run,
        PrerequisiteState.READY,
        Dispatch.SENT,
        ExitStatus(code=0),
        None,
        helper,
        systemd.BoundaryObservation(run, BoundaryState.EMPTY, ExitStatus(code=0), Dispatch.SENT, ExitStatus(code=0)),
    )
    assert result.helper is not None and result.helper.failure == failure
    assert result.complete


def test_terminal_helper_carrier_error_is_not_complete() -> None:
    run = ManagedRun.fresh()
    helper = _terminal_observation(run, carrier_stdout_complete=False)
    assert helper.trusted_terminal
    assert helper.error is ObservationError.CARRIER
    result = systemd.ManagedForegroundResult(
        run,
        PrerequisiteState.READY,
        Dispatch.SENT,
        ExitStatus(code=0),
        None,
        helper,
        systemd.BoundaryObservation(run, BoundaryState.EMPTY, ExitStatus(code=0), Dispatch.SENT, ExitStatus(code=0)),
    )
    assert not result.complete


@pytest.mark.parametrize("boundary_state", (BoundaryState.POPULATED, BoundaryState.INVALID))
def test_nonempty_boundary_is_not_complete(boundary_state: BoundaryState) -> None:
    run = ManagedRun.fresh()
    helper = _terminal_observation(run)
    assert helper.error is None
    result = systemd.ManagedForegroundResult(
        run,
        PrerequisiteState.READY,
        Dispatch.SENT,
        ExitStatus(code=0),
        None,
        helper,
        systemd.BoundaryObservation(run, boundary_state, ExitStatus(code=0), Dispatch.SENT, ExitStatus(code=0)),
    )
    assert not result.complete


def test_missing_or_lost_launch_is_not_boundary_success(identity: IdentityExpectation) -> None:
    prepared = _prepared(identity)
    result = run_managed_candidate(SystemdFake(dispatch=Dispatch.UNKNOWN), prepared, deadline=Deadline.after(1))
    assert result.boundary.state is BoundaryState.UNKNOWN
    assert not result.complete
    with pytest.raises(ValidationError):
        run_managed_candidate(SystemdFake(), prepared, deadline=Deadline.after(1))


def test_candidate_claim_is_atomic_before_dispatch(identity: IdentityExpectation) -> None:
    prepared = _prepared(identity)
    carrier = SystemdFake()
    errors: list[ValidationError] = []
    underlying_lock = threading.Lock()
    attempts = threading.Event()
    attempt_count = 0
    count_lock = threading.Lock()

    class AttemptLock:
        def __enter__(self) -> AttemptLock:
            nonlocal attempt_count
            with count_lock:
                attempt_count += 1
                if attempt_count == 2:
                    attempts.set()
            underlying_lock.acquire()
            return self

        def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
            del exc_type, exc_value, traceback
            underlying_lock.release()

    prepared._claim_lock = cast(Any, AttemptLock())

    def invoke() -> None:
        try:
            run_managed_candidate(carrier, prepared, deadline=Deadline.after(1))
        except ValidationError as error:
            errors.append(error)

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    underlying_lock.acquire()
    try:
        first.start()
        second.start()
        assert attempts.wait(timeout=1)
        assert carrier.calls == []
    finally:
        underlying_lock.release()
    first.join(timeout=1)
    second.join(timeout=1)
    assert not first.is_alive() and not second.is_alive()
    assert len(carrier.calls) == 1
    assert len(errors) == 1


def test_private_input_and_output_bounds_do_not_leak_through_fake_dispatch(identity: IdentityExpectation) -> None:
    prepared = _prepared(identity, sensitive=True)
    result = run_managed_candidate(SystemdFake(), prepared, deadline=Deadline.after(1))
    assert not result.complete
    assert "private-payload-canary" not in repr(result)
    assert "private-input-canary" not in repr(result)
    with pytest.raises(ValidationError):
        prepare_managed_candidate(
            Command(("/bin/true",)),
            identity=identity,
            prerequisite=PrerequisiteState.READY,
            capture_limit=4_097,
        )


def test_prerequisite_requires_root_v252_and_cgroup_v2() -> None:
    current = SystemdFake()
    assert check_systemd_prerequisites(current, deadline=Deadline.after(1)) is PrerequisiteState.READY
    assert all("cgroup.kill" not in part for call in current.calls for part in call)

    class Older(SystemdFake):
        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            self.validate(invocation, io=io)
            if invocation.argv == ("/usr/bin/systemd-run", "--version"):
                return _report(b"systemd 251\n")
            return super().execute(invocation, io=io, deadline=deadline)

    assert check_systemd_prerequisites(Older(), deadline=Deadline.after(1)) is PrerequisiteState.UNSUPPORTED

    class Newer(SystemdFake):
        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            self.validate(invocation, io=io)
            if invocation.argv == ("/usr/bin/systemd-run", "--version"):
                return _report(b"systemd 253\n")
            return super().execute(invocation, io=io, deadline=deadline)

    assert check_systemd_prerequisites(Newer(), deadline=Deadline.after(1)) is PrerequisiteState.READY

    class Unprivileged(SystemdFake):
        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            self.validate(invocation, io=io)
            if invocation.argv[:5] == ("/usr/bin/python3", "-I", "-S", "-B", "-c"):
                return _report(b"AGW_MANAGED_CONTROL_1:unavailable\n")
            return super().execute(invocation, io=io, deadline=deadline)

    assert (
        check_systemd_prerequisites(Unprivileged(), deadline=Deadline.after(1)) is PrerequisiteState.CONTROL_UNAVAILABLE
    )


def _current_identity() -> IdentityExpectation:
    return IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()})))


def _prepared_supervisor(prepared) -> tuple[str, str, str]:
    argv = prepared.invocation.argv
    separator = argv.index("--")
    runner = argv[separator + 1 :]
    assert runner[:5] == ("/usr/bin/python3", "-I", "-S", "-B", "-c")
    source, run_id, cgroup_root, helper = runner[5:]
    assert run_id == prepared.run.token
    assert cgroup_root == "/sys/fs/cgroup"
    return source, run_id, helper


def _run_bounded_supervisor(
    tmp_path: Path,
    manifest: bytes,
    nonce: str,
    helper: str,
    *,
    remove_membership: bool = False,
    deny_kill_write: bool = False,
    supervisor_source: str = systemd._SUPERVISOR_SOURCE,
) -> subprocess.CompletedProcess[bytes]:
    """Execute only self-bounded, non-detached work through the source harness.

    A timeout SIGKILLs and reaps the owned supervisor/helper session. The fixed
    helper may create a separate payload session, so this fixture does not own
    or clean up a detached payload. Native cgroup proof covers that behavior.
    """
    root = tmp_path / "cgroup"
    service = root / "service"
    service.mkdir(parents=True)
    if remove_membership:
        (root / ".agw-test-remove-membership").touch()
    if deny_kill_write:
        (root / ".agw-test-deny-kill-write").touch()

    def kernel() -> None:
        child = service / f"agw-workload-{nonce}"
        limit = time.monotonic() + 2
        while not child.exists() and time.monotonic() < limit:
            time.sleep(0.001)
        if not child.exists():
            return
        membership = child / "cgroup.procs"
        raw_pid = b""
        while time.monotonic() < limit:
            try:
                raw_pid = membership.read_bytes().strip()
            except FileNotFoundError:
                time.sleep(0.001)
                continue
            if raw_pid:
                break
            time.sleep(0.001)
        if not raw_pid:
            return
        pid = int(raw_pid)
        while Path(f"/proc/{pid}").exists() and time.monotonic() < limit:
            stat = Path(f"/proc/{pid}/stat")
            try:
                if stat.exists() and ") Z " in stat.read_text():
                    break
            except OSError:
                break
            time.sleep(0.001)
        events = child / "cgroup.events"
        if events.exists():
            events.write_text("populated 1\n")

    watcher = threading.Thread(target=kernel)
    watcher.start()
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            _SUPERVISOR_DRIVER,
            nonce,
            str(root),
            helper,
            supervisor_source,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(manifest)
    process.stdin.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    assert process.stdout is not None and process.stderr is not None
    stdout, stderr = process.stdout.read(), process.stderr.read()
    watcher.join(timeout=2)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _run_prepared_supervisor(tmp_path: Path, prepared) -> subprocess.CompletedProcess[bytes]:
    assert isinstance(prepared.io.input, FiniteInput)
    source, run_id, helper = _prepared_supervisor(prepared)
    return _run_bounded_supervisor(tmp_path, prepared.io.input.data, run_id, helper, supervisor_source=source)


@_LINUX_ONLY
def test_bundled_supervisor_executes_fast_helper_and_observes_child_empty(tmp_path: Path) -> None:
    prepared = prepare_managed_candidate(
        Command(("/bin/true",)),
        identity=_current_identity(),
        prerequisite=PrerequisiteState.READY,
    )
    result = _run_prepared_supervisor(tmp_path, prepared)
    assert result.returncode == 0
    assert b"AGWE1 " in result.stdout
    assert result.stderr == f"AGW_MANAGED_BOUNDARY_1:{prepared.run.token}:empty:exit:0\n".encode()


@_LINUX_ONLY
def test_bundled_supervisor_preserves_payload_exit(tmp_path: Path) -> None:
    prepared = prepare_managed_candidate(
        Command(("/bin/sh", "-c", "exit 23")),
        identity=_current_identity(),
        prerequisite=PrerequisiteState.READY,
    )
    result = _run_prepared_supervisor(tmp_path, prepared)
    observer = InlineObserver(OutputMode.CAPTURE, 4_096)
    reader = FrameReader(prepared.run.token, observer.accept)
    _write(reader, result.stdout)
    reader.finish()
    transcript = observer.finish(reader.error, carrier_stdout_complete=True)
    assert transcript.trusted_terminal
    assert transcript.wait == WaitFact(WaitKind.EXIT, 23)
    assert result.stderr == f"AGW_MANAGED_BOUNDARY_1:{prepared.run.token}:empty:exit:0\n".encode()


@_LINUX_ONLY
def test_nonzero_helper_status_rejects_plausible_terminal_payload_frames(tmp_path: Path) -> None:
    prepared = prepare_managed_candidate(
        Command(("/bin/true",)),
        identity=_current_identity(),
        prerequisite=PrerequisiteState.READY,
    )
    transcript = _run_prepared_supervisor(tmp_path, prepared)
    result = run_managed_candidate(
        SystemdFake(marker="empty:exit:9", launch=transcript.stdout), prepared, deadline=Deadline.after(1)
    )
    assert result.helper is not None and result.helper.trusted_terminal
    assert result.boundary.helper_completion == ExitStatus(code=9)
    assert result.boundary.state is BoundaryState.EMPTY
    assert result.helper_integrity_failed
    assert not result.complete


@_LINUX_ONLY
def test_systemd_completion_remains_separate_from_helper_and_boundary_evidence(tmp_path: Path) -> None:
    prepared = prepare_managed_candidate(
        Command(("/bin/true",)),
        identity=_current_identity(),
        prerequisite=PrerequisiteState.READY,
    )
    transcript = _run_prepared_supervisor(tmp_path, prepared)
    result = run_managed_candidate(
        SystemdFake(launch=transcript.stdout, completion=1), prepared, deadline=Deadline.after(1)
    )
    assert result.launch_completion == ExitStatus(code=1)
    assert result.boundary.helper_completion == ExitStatus(code=0)
    assert result.boundary.state is BoundaryState.EMPTY
    assert not result.complete


@_LINUX_ONLY
def test_bundled_supervisor_keeps_inner_failure_separate_from_empty_boundary(tmp_path: Path) -> None:
    run = ManagedRun.fresh()
    result = _run_bounded_supervisor(tmp_path, b"{}", run.token, "raise SystemExit(9)")
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == f"AGW_MANAGED_BOUNDARY_1:{run.token}:empty:exit:9\n".encode()


@_LINUX_ONLY
def test_bundled_supervisor_requires_the_created_workload_cgroup_kill_file(tmp_path: Path) -> None:
    run = ManagedRun.fresh()
    result = _run_bounded_supervisor(tmp_path, b"{}", run.token, "raise SystemExit(0)", deny_kill_write=True)
    assert result.returncode == 0
    assert result.stderr == f"AGW_MANAGED_BOUNDARY_1:{run.token}:invalid:exit:0\n".encode()


@_LINUX_ONLY
def test_bundled_supervisor_does_not_exec_without_workload_cgroup_membership(tmp_path: Path) -> None:
    run = ManagedRun.fresh()
    result = _run_bounded_supervisor(
        tmp_path,
        b"private-manifest-canary",
        run.token,
        "raise SystemExit(0)",
        remove_membership=True,
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == f"AGW_MANAGED_BOUNDARY_1:{run.token}:invalid:exit:127\n".encode()
