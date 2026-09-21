"""Private Linux systemd foreground MANAGED experiment.

The systemd service is the target-identity supervisor. It owns a manually
created delegated child cgroup for the workload, so it can observe that child
empty before its service main process exits. This is intentionally not a jobs
or RunContext API.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._evidence_wire import FrameReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._inline import _manifest
from agentworks.execution._inline_bundle import FIXED_SOURCE
from agentworks.execution._inline_control import WaitKind
from agentworks.execution._inline_observer import InlineObservation, InlineObserver
from agentworks.execution._inline_request import ManifestError, ManifestErrorCode, OutputMode, encode_manifest
from agentworks.execution.carrier import (
    Capture,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution.carrier import Carrier
    from agentworks.execution.models import Command, Script


_SYSTEMD_RUN = "/usr/bin/systemd-run"
_PYTHON = "/usr/bin/python3"
_CGROUP_ROOT = "/sys/fs/cgroup"
_UNIT_PREFIX = "agw-managed-"
_VERSION_RE = re.compile(rb"systemd ([0-9]{1,5})(?:\n| )")
_BOUNDARY_RE = re.compile(
    rb"AGW_MANAGED_BOUNDARY_1:([0-9a-f]{32}):(empty|populated|invalid|launch_failed):(exit|signal|unknown):([0-9]{1,3})\n\Z"
)
_WAIT_STATUS_MAX = 255
_CONTROL_PROBE = (
    "import os,sys;sys.stdout.write("
    "'AGW_MANAGED_CONTROL_1:root\\n' if os.getresuid()==(0,0,0) "
    "else 'AGW_MANAGED_CONTROL_1:unavailable\\n')"
)

# Fixed service-main code. It creates its child workload cgroup before the
# child execs any caller-controlled shell/source. The child inherits the
# service pipe endpoints. The parent observes the dedicated cgroup after it
# waits for the helper, then emits its marker on service stderr.
_SUPERVISOR_SOURCE = r"""
import os, sys, time
run_id, root, helper = sys.argv[1:4]
state = 'invalid'
helper_kind, helper_value = 'unknown', 0
child = None
events_fd = -1
try:
 with open('/proc/self/cgroup', 'rb') as stream: records = stream.read(4096).splitlines()
 selected = [record[3:] for record in records if record.startswith(b'0::')]
 if len(selected) != 1 or not selected[0].startswith(b'/'): raise ValueError
 service = selected[0]
 parts = service.split(b'/')[1:]
 if not parts or any(part in (b'', b'.', b'..') for part in parts): raise ValueError
 base = root.encode('ascii') + service
 child = base + b'/agw-workload-' + run_id.encode('ascii')
 os.mkdir(child)
 events_fd = os.open(child + b'/cgroup.events', os.O_RDONLY)
 kill_fd = os.open(child + b'/cgroup.kill', os.O_WRONLY)
 os.close(kill_fd)
 child_pid = os.fork()
 if child_pid == 0:
  try:
   with open(child + b'/cgroup.procs', 'wb', buffering=0) as membership:
    membership.write(('%d\n' % os.getpid()).encode('ascii'))
   null = os.open('/dev/null', os.O_WRONLY)
   os.dup2(null, 2)
   os.execv('/usr/bin/python3', ['/usr/bin/python3', '-I', '-S', '-B', '-c', helper, run_id])
  except BaseException: os._exit(127)
 _, child_status = os.waitpid(child_pid, 0)
 if os.WIFEXITED(child_status): helper_kind, helper_value = 'exit', os.WEXITSTATUS(child_status)
 elif os.WIFSIGNALED(child_status): helper_kind, helper_value = 'signal', os.WTERMSIG(child_status)
 deadline = time.monotonic() + 5
 while time.monotonic() < deadline:
  os.lseek(events_fd, 0, os.SEEK_SET)
  events = os.read(events_fd, 4096).splitlines()
  if b'populated 0' in events:
   state = 'empty'
   break
  if b'populated 1' not in events: break
  state = 'populated'
  try:
   kill_fd = os.open(child + b'/cgroup.kill', os.O_WRONLY)
   try: os.write(kill_fd, b'1')
   finally: os.close(kill_fd)
  except OSError:
   state = 'invalid'
   break
  time.sleep(.01)
 if state not in ('empty', 'invalid'): state = 'populated'
except BaseException:
 state = 'launch_failed' if child is None else 'invalid'
finally:
 if events_fd >= 0:
  try: os.close(events_fd)
  except OSError: pass
 if child is not None:
  try: os.rmdir(child)
  except OSError: pass
try: os.write(2, ('AGW_MANAGED_BOUNDARY_1:%s:%s:%s:%d\n' % (run_id, state, helper_kind, helper_value)).encode('ascii'))
except BaseException: pass
"""


class PrerequisiteState(StrEnum):
    """Closed local proof state for the Linux supervisor mechanism."""

    UNOBSERVED = "unobserved"
    READY = "ready"
    UNSUPPORTED = "unsupported"
    CONTROL_UNAVAILABLE = "control_unavailable"
    UNKNOWN = "unknown"


class BoundaryState(StrEnum):
    """Only EMPTY proves the dedicated workload cgroup was observed empty."""

    UNOBSERVED = "unobserved"
    EMPTY = "empty"
    POPULATED = "populated"
    INVALID = "invalid"
    LAUNCH_FAILED = "launch_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ManagedRun:
    """Fresh private identity for one generated transient service."""

    token: str

    def __post_init__(self) -> None:
        if (
            type(self.token) is not str
            or len(self.token) != 32
            or any(char not in "0123456789abcdef" for char in self.token)
        ):
            raise ValidationError("Managed run identity must be 32 lowercase hexadecimal characters")

    @property
    def unit(self) -> str:
        return f"{_UNIT_PREFIX}{self.token}.service"

    @classmethod
    def fresh(cls) -> ManagedRun:
        return cls(secrets.token_hex(16))


@dataclass(frozen=True, slots=True)
class BoundaryObservation:
    """Final marker from the service main, never inferred from a missing unit."""

    run: ManagedRun
    state: BoundaryState
    helper_completion: ExitStatus | None = None
    dispatch: Dispatch = Dispatch.NOT_SENT
    completion: ExitStatus | None = None
    failure: Failure | None = None


@dataclass
class PreparedManagedCandidate:
    """One fixed service launch with no caller-selected systemd controls."""

    run: ManagedRun
    invocation: PreparedInvocation
    io: CarrierIO
    prerequisite: PrerequisiteState
    _reader: FrameReader = field(repr=False)
    _observer: InlineObserver = field(repr=False)
    _boundary_marker: _BoundaryMarkerSink = field(repr=False)
    _claimed: bool = field(default=False, init=False, repr=False)
    _claim_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def claim(self) -> None:
        with self._claim_lock:
            if self._claimed:
                raise ValidationError("A managed candidate cannot be dispatched more than once")
            self._claimed = True


@dataclass(frozen=True, slots=True)
class ManagedForegroundResult:
    """Carrier, helper/payload and dedicated-boundary facts remain separate."""

    run: ManagedRun
    prerequisite: PrerequisiteState
    launch_dispatch: Dispatch
    launch_completion: ExitStatus | None
    launch_failure: Failure | None
    helper: InlineObservation | None
    boundary: BoundaryObservation

    @property
    def helper_integrity_failed(self) -> bool:
        """A known nonzero helper exit invalidates otherwise plausible frames."""
        return self.boundary.helper_completion is not None and self.boundary.helper_completion != ExitStatus(code=0)

    @property
    def complete(self) -> bool:
        """Require execution and lifecycle evidence, not full output retention."""
        return (
            self.prerequisite is PrerequisiteState.READY
            and self.launch_dispatch is Dispatch.SENT
            and self.launch_completion == ExitStatus(code=0)
            and self.launch_failure is None
            and self.helper is not None
            and self.helper.trusted_terminal
            and self.helper.wait is not None
            and self.helper.wait.kind in (WaitKind.EXIT, WaitKind.SIGNAL)
            and self.helper.error is None
            and self.boundary.helper_completion == ExitStatus(code=0)
            and self.boundary.state is BoundaryState.EMPTY
        )


@dataclass(slots=True)
class _BoundaryMarkerSink:
    """Bounded stderr-only receiver for the service-main boundary fact."""

    run: ManagedRun
    _data: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _overflow: bool = field(default=False, init=False, repr=False)

    def try_write(self, data: memoryview) -> int:
        if len(self._data) + len(data) > 128:
            self._data.clear()
            self._overflow = True
            return len(data)
        self._data.extend(data)
        return len(data)

    def finish(self, report: CarrierReport) -> BoundaryObservation:
        if (
            self._overflow
            or report.dispatch is not Dispatch.SENT
            or report.completion is None
            or not report.stderr.complete
            or report.stderr.retention is not Retention.DELIVERED
        ):
            return BoundaryObservation(
                self.run, BoundaryState.UNKNOWN, None, report.dispatch, report.completion, report.failure
            )
        marker = _BOUNDARY_RE.fullmatch(bytes(self._data))
        self._data.clear()
        if marker is None or marker.group(1).decode("ascii") != self.run.token:
            return BoundaryObservation(
                self.run, BoundaryState.INVALID, None, report.dispatch, report.completion, report.failure
            )
        kind, raw_status = marker.group(3), marker.group(4)
        status = int(raw_status)
        helper_completion: ExitStatus | None
        if kind == b"unknown":
            if status != 0:
                return BoundaryObservation(
                    self.run, BoundaryState.INVALID, None, report.dispatch, report.completion, report.failure
                )
            helper_completion = None
        elif kind == b"exit" and 0 <= status <= _WAIT_STATUS_MAX:
            helper_completion = ExitStatus(code=status)
        elif kind == b"signal" and 0 < status <= _WAIT_STATUS_MAX:
            helper_completion = ExitStatus(signal=status)
        else:
            return BoundaryObservation(
                self.run, BoundaryState.INVALID, None, report.dispatch, report.completion, report.failure
            )
        return BoundaryObservation(
            self.run,
            BoundaryState(marker.group(2).decode("ascii")),
            helper_completion,
            report.dispatch,
            report.completion,
            report.failure,
        )


def _output(capture_limit: int | None, sensitive: bool) -> tuple[OutputMode, int]:
    if capture_limit is not None and (type(capture_limit) is not int or not 0 <= capture_limit <= 4_096):
        raise ValidationError("Managed capture limit must be between 0 and 4096 bytes")
    if sensitive:
        return OutputMode.SUPPRESS, 0
    if capture_limit is None:
        return OutputMode.DISCARD, 0
    return OutputMode.CAPTURE, capture_limit


def _unit_properties(expected: IdentityExpectation) -> tuple[str, ...]:
    """Return the entire trusted service property set, including delegation."""
    groups = " ".join(str(group) for group in expected.groups)
    return (
        f"--property=User={expected.euid}",
        f"--property=Group={expected.egid}",
        f"--property=SupplementaryGroups={groups}",
        "--property=Delegate=yes",
        "--property=KillMode=control-group",
        "--property=ExitType=main",
    )


def _manifest_data(
    request: Command | Script,
    *,
    run: ManagedRun,
    identity: IdentityExpectation,
    stdin: bytes,
    env: Mapping[str, str] | None,
    cwd: str | None,
    output_mode: OutputMode,
    capture_limit: int,
) -> bytes:
    manifest = _manifest(
        request,
        nonce=run.token,
        identity=identity,
        stdin=stdin,
        env=env,
        cwd=cwd,
        output_mode=output_mode,
        capture_limit=capture_limit,
    )
    try:
        return encode_manifest(manifest)
    except ManifestError as error:
        if error.code is ManifestErrorCode.OVERSIZED:
            raise ValidationError("Managed invocation exceeds the 32768-byte candidate manifest bound") from None
        raise ValidationError("Managed invocation contains an invalid field") from None


def prepare_managed_candidate(
    request: Command | Script,
    *,
    identity: IdentityExpectation,
    prerequisite: PrerequisiteState,
    stdin: bytes = b"",
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    capture_limit: int | None = 4_096,
    sensitive: bool = False,
) -> PreparedManagedCandidate:
    """Prepare a fresh fixed service, not a replayable or caller-configured unit."""
    if type(identity) is not IdentityExpectation or type(prerequisite) is not PrerequisiteState:
        raise ValidationError("Managed execution requires an observed systemd prerequisite result")
    run = ManagedRun.fresh()
    output_mode, retained_limit = _output(capture_limit, sensitive)
    manifest_data = _manifest_data(
        request,
        run=run,
        identity=identity,
        stdin=stdin,
        env=env,
        cwd=cwd,
        output_mode=output_mode,
        capture_limit=retained_limit,
    )
    observer = InlineObserver(output_mode, retained_limit)
    reader = FrameReader(run.token, observer.accept)
    marker = _BoundaryMarkerSink(run)
    invocation = PreparedInvocation(
        (
            _SYSTEMD_RUN,
            "--quiet",
            "--pipe",
            "--wait",
            "--collect",
            "--service-type=exec",
            f"--unit={run.unit}",
            *_unit_properties(identity),
            "--",
            _PYTHON,
            "-I",
            "-S",
            "-B",
            "-c",
            _SUPERVISOR_SOURCE,
            run.token,
            _CGROUP_ROOT,
            FIXED_SOURCE,
        )
    )
    return PreparedManagedCandidate(
        run=run,
        invocation=invocation,
        io=CarrierIO(
            input=FiniteInput(manifest_data, sensitive=sensitive),
            output=SinkOutput(reader, marker, require_live=False),
            sensitive=sensitive,
        ),
        prerequisite=prerequisite,
        _reader=reader,
        _observer=observer,
        _boundary_marker=marker,
    )


def check_systemd_prerequisites(carrier: Carrier, *, deadline: Deadline) -> PrerequisiteState:
    """Require root control, systemd v252 floor and cgroup v2."""
    control = carrier.execute(
        PreparedInvocation((_PYTHON, "-I", "-S", "-B", "-c", _CONTROL_PROBE)),
        io=CarrierIO(output=Capture(64)),
        deadline=deadline,
    )
    if control.dispatch is not Dispatch.SENT:
        return PrerequisiteState.UNKNOWN
    if (
        control.failure is not None
        or control.completion is None
        or control.completion.code != 0
        or not control.stdout.complete
    ):
        return PrerequisiteState.UNKNOWN
    if control.stdout.data != b"AGW_MANAGED_CONTROL_1:root\n":
        return PrerequisiteState.CONTROL_UNAVAILABLE
    version = carrier.execute(
        PreparedInvocation((_SYSTEMD_RUN, "--version")), io=CarrierIO(output=Capture(4_096)), deadline=deadline
    )
    match = _VERSION_RE.match(version.stdout.data)
    if (
        version.dispatch is not Dispatch.SENT
        or version.failure is not None
        or version.completion is None
        or version.completion.code != 0
        or not version.stdout.complete
        or match is None
        or int(match.group(1)) < 252
    ):
        return PrerequisiteState.UNSUPPORTED if version.dispatch is Dispatch.SENT else PrerequisiteState.UNKNOWN
    cgroup = carrier.execute(
        PreparedInvocation(("/usr/bin/test", "-f", f"{_CGROUP_ROOT}/cgroup.controllers")),
        io=CarrierIO(output=Capture(0)),
        deadline=deadline,
    )
    if cgroup.dispatch is not Dispatch.SENT:
        return PrerequisiteState.UNKNOWN
    supported = cgroup.failure is None and cgroup.completion is not None and cgroup.completion.code == 0
    return PrerequisiteState.READY if supported else PrerequisiteState.UNSUPPORTED


def run_managed_candidate(
    carrier: Carrier,
    prepared: PreparedManagedCandidate,
    *,
    deadline: Deadline,
) -> ManagedForegroundResult:
    """Dispatch exactly once and retain helper and boundary observations separately."""
    if prepared.prerequisite is not PrerequisiteState.READY:
        return ManagedForegroundResult(
            prepared.run,
            prepared.prerequisite,
            Dispatch.NOT_SENT,
            None,
            None,
            None,
            BoundaryObservation(prepared.run, BoundaryState.UNOBSERVED),
        )
    prepared.claim()
    try:
        launch = carrier.execute(prepared.invocation, io=prepared.io, deadline=deadline)
    finally:
        prepared._reader.finish()
    delivered = launch.stdout.retention is Retention.DELIVERED and launch.stderr.retention is Retention.DELIVERED
    helper = prepared._observer.finish(
        prepared._reader.error,
        carrier_stdout_complete=delivered and launch.stdout.complete,
    )
    return ManagedForegroundResult(
        prepared.run,
        prepared.prerequisite,
        launch.dispatch,
        launch.completion,
        launch.failure,
        helper,
        prepared._boundary_marker.finish(launch),
    )
