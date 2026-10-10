"""Independent, single-attempt Proxmox REST delivery for prepared invocations.

PVE converts QGA's base64 streams into JSON strings. This boundary accepts only
ASCII-armored preparation output, not arbitrary guest bytes disguised as text.
Each HTTP exchange runs in an owned subprocess so its deadline also bounds DNS,
TLS setup and continuously arriving responses, not just socket inactivity.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agentworks.errors import StateError, ValidationError
from agentworks.execution._process import SinkWriteError, try_write_to_sink
from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
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
from agentworks.execution.carriers._proxmox_http import (
    _MAX_RESPONSE_BYTES,
    _Endpoint,
    _valid_control_timeout,
    _valid_task_id,
)
from agentworks.execution.carriers._subprocess import run_process

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody

_MAX_INPUT_BYTES = 65_536
# Older supported PVE 8 HTTP servers limit the complete POST, independently
# of the guest-agent input field. Include argv and JSON escaping in this bound.
_MAX_REQUEST_BYTES = 65_536
# The worker caps the entire status JSON at 8 MiB. Reserve room for JSON
# escaping (up to six bytes per ASCII control byte), stderr and fixed fields.
# Native QGA/PVE capture acceptance still needs its own route proof.
_MAX_COMPLETE_STDOUT_BYTES = 1_048_576


@dataclass(frozen=True)
class ProxmoxConnection:
    """Explicit REST authority for one VM, without credential discovery.

    A CA bundle selects trusted issuers for this connection; omission uses the
    workstation's normal TLS trust defaults. Hostname verification always applies.
    Bundle loading is deferred to delivery, keeping construction passive.
    """

    api_url: str
    node: str
    vmid: int
    token_id: str = field(repr=False)
    token_secret: str = field(repr=False)
    ca_bundle: Path | None = None

    def __post_init__(self) -> None:
        """Validate connection inputs supplied by the composition boundary."""
        parsed = None
        try:
            candidate = urllib.parse.urlsplit(self.api_url)
            port = candidate.port
            if port is None or port > 0:
                parsed = candidate
        except ValueError:
            pass
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValidationError("Proxmox requires an HTTPS origin without user information")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.node) or type(self.vmid) is not int or self.vmid <= 0:
            raise ValidationError("Proxmox requires a node and positive VM identifier")
        if any(
            not isinstance(token, str) or not token or "\r" in token or "\n" in token
            for token in (self.token_id, self.token_secret)
        ):
            raise ValidationError("Proxmox requires a valid API token")
        if self.ca_bundle is not None and (not isinstance(self.ca_bundle, Path) or "\0" in str(self.ca_bundle)):
            raise ValidationError("Proxmox CA bundle must be a filesystem path")


class _WireFailure(Exception):
    """A REST response is unavailable or cannot establish execution evidence."""


class _ResponseBuffer:
    """Private bounded parsing bytes, never carrier output retention."""

    def __init__(self) -> None:
        self.data = bytearray()

    def try_write(self, data: memoryview) -> int:
        if len(self.data) + len(data) > _MAX_RESPONSE_BYTES:
            raise SinkWriteError("Proxmox HTTP response exceeded its bound")
        self.data.extend(data)
        return len(data)


class _DiscardResponse:
    def try_write(self, data: memoryview) -> int:
        return len(data)


class _HelperStatusObserver(Protocol):
    def acknowledge(self, pid: int) -> None: ...

    def before_status(self) -> None: ...

    def record_status(self, status: dict[str, object]) -> None: ...


class _ProxmoxWire:
    def __init__(self, connection: ProxmoxConnection) -> None:
        self._connection = connection

    def request(
        self,
        method: str,
        suffix: str,
        *,
        custody: LocalDeliveryCustody,
        body: bytes | None = None,
        timeout: float | None,
    ) -> dict[str, object]:
        return self._request(_Endpoint.GUEST_AGENT, method, suffix, custody=custody, body=body, timeout=timeout)

    def request_power(self, *, custody: LocalDeliveryCustody, timeout: float) -> dict[str, object]:
        """Read provider power through the fixed, passive status endpoint."""
        return self._request(_Endpoint.POWER, "GET", None, custody=custody, body=None, timeout=timeout)

    def request_current_config(self, *, custody: LocalDeliveryCustody, timeout: float) -> dict[str, object]:
        """Read live provider configuration through one fixed, passive endpoint."""
        return self._request(_Endpoint.CURRENT_CONFIG, "GET", None, custody=custody, body=None, timeout=timeout)

    def request_vm_start(self, *, custody: LocalDeliveryCustody, timeout: float) -> str:
        """Submit one fixed start, returning only a bounded raw acknowledgment."""
        if not _valid_control_timeout(timeout):
            raise ValidationError("Proxmox start requires a positive finite timeout")
        data = self._exchange(_Endpoint.VM_START, "POST", None, custody=custody, body=None, timeout=timeout)
        if not _valid_task_id(data):
            raise _WireFailure("Proxmox returned an invalid start acknowledgment")
        assert isinstance(data, str)
        return data

    def request_task_status(self, upid: str, *, custody: LocalDeliveryCustody, timeout: float) -> dict[str, object]:
        """Read one literal task under the selected node, without identity binding."""
        if not _valid_control_timeout(timeout) or not _valid_task_id(upid):
            raise ValidationError("Proxmox task status requires a bounded identifier and positive finite timeout")
        return self._request(_Endpoint.TASK_STATUS, "GET", upid, custody=custody, body=None, timeout=timeout)

    def request_guest_info(self, *, custody: LocalDeliveryCustody, timeout: float) -> dict[str, object]:
        """Read guest-agent information through one fixed, body-free endpoint."""
        if not _valid_control_timeout(timeout):
            raise ValidationError("Proxmox guest information requires a positive finite timeout")
        return self._request(_Endpoint.GUEST_INFO, "GET", None, custody=custody, body=None, timeout=timeout)

    def _request(
        self,
        endpoint: _Endpoint,
        method: str,
        suffix: str | None,
        *,
        custody: LocalDeliveryCustody,
        body: bytes | None,
        timeout: float | None,
    ) -> dict[str, object]:
        data = self._exchange(endpoint, method, suffix, custody=custody, body=body, timeout=timeout)
        if not isinstance(data, dict):
            raise _WireFailure("Proxmox returned an invalid response envelope")
        return dict(data)

    def _exchange(
        self,
        endpoint: _Endpoint,
        method: str,
        suffix: str | None,
        *,
        custody: LocalDeliveryCustody,
        body: bytes | None,
        timeout: float | None,
    ) -> object:
        """Own one HTTP worker until completion, timeout or propagated interruption."""
        if not custody.settled:
            raise StateError("Previous local delivery cleanup remains unsettled")
        started = time.monotonic()
        connection = asdict(self._connection)
        connection["ca_bundle"] = str(self._connection.ca_bundle) if self._connection.ca_bundle is not None else None
        payload = json.dumps(
            {
                "connection": connection,
                "method": method,
                "suffix": suffix,
                "body": body.decode("ascii") if body is not None else None,
                "timeout": timeout,
                "endpoint": endpoint,
            }
        ).encode("ascii")
        response = _ResponseBuffer()
        result = run_process(
            [sys.executable, "-I", "-m", "agentworks.execution.carriers._proxmox_http"],
            custody=custody,
            io=CarrierIO(input=FiniteInput(payload, sensitive=True), output=SinkOutput(response, _DiscardResponse())),
            deadline=Deadline(None if timeout is None else started + timeout),
        )
        if not custody.settled or result.failure is not None or result.exit_status != 0 or not result.stdout.complete:
            raise _WireFailure("Proxmox request or response failed")
        parsed: object = None
        invalid_json = False
        try:
            parsed = json.loads(response.data)
        except ValueError:
            invalid_json = True
        if invalid_json:
            raise _WireFailure("Proxmox returned invalid JSON")
        if not isinstance(parsed, dict) or "data" not in parsed:
            raise _WireFailure("Proxmox returned an invalid response envelope")
        return parsed["data"]


class ProxmoxCarrier:
    """Deliver prepared ASCII bootstrap work through the QGA REST endpoints.

    Completion records the submitted command's exit, not proof that the
    bootstrap or application ran.
    Deadlines cover preparation, worker startup and each HTTP request. Stopping
    the local HTTP worker does not cancel a command already submitted to QGA.
    No status read is retried after failure: a terminal QGA read reaps its record.
    """

    def __init__(self, connection: ProxmoxConnection) -> None:
        self._wire = _ProxmoxWire(connection)
        self._channel_features = ChannelFeatures()

    @property
    def features(self) -> ChannelFeatures:
        return self._channel_features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        """Check the exact ASCII request envelope before any provider access."""
        self._request_body(invocation, io)

    @staticmethod
    def _request_body(invocation: PreparedInvocation, io: CarrierIO) -> bytes:
        if isinstance(io.input, TerminalInput):
            raise ValidationError("Proxmox does not support terminal input")
        if isinstance(io.input, LiveInput) or isinstance(io.output, SinkOutput) and io.output.require_live:
            raise ValidationError("Proxmox does not support live standard I/O")
        if (
            isinstance(io.output, SinkOutput)
            and io.output.required_complete_stdout_bytes is not None
            and io.output.required_complete_stdout_bytes > _MAX_COMPLETE_STDOUT_BYTES
        ):
            raise ValidationError("Proxmox cannot fit the required complete stdout response")
        input_data = io.input.data if isinstance(io.input, FiniteInput) else b""
        if not input_data.isascii() or any(not arg.isascii() for arg in invocation.argv):
            raise ValidationError("Proxmox proof delivery requires ASCII-armored preparation")
        if len(input_data) > _MAX_INPUT_BYTES:
            raise ValidationError("Prepared Proxmox input exceeds the provider input limit")
        body = json.dumps({"command": invocation.argv, "input-data": input_data.decode("ascii")}).encode("ascii")
        if len(body) > _MAX_REQUEST_BYTES:
            raise ValidationError("Prepared Proxmox request exceeds the supported HTTP body limit")
        return body

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        return self._execute(invocation, io=io, deadline=deadline, custody=custody)

    def _execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody,
        observer: _HelperStatusObserver | None = None,
    ) -> CarrierReport:
        if not custody.settled:
            raise StateError("Previous local delivery cleanup remains unsettled")
        body = self._request_body(invocation, io)
        if deadline.expired:
            return _incomplete(Dispatch.NOT_SENT, io, Failure.DEADLINE)
        try:
            response = self._wire.request("POST", "exec", custody=custody, body=body, timeout=deadline.remaining())
        except Exception:
            return _incomplete(Dispatch.UNKNOWN, io, Failure.DEADLINE if deadline.expired else Failure.DISPATCH)
        pid = response.get("pid")
        if type(pid) is not int or pid <= 0:
            return _incomplete(Dispatch.UNKNOWN, io, Failure.INVALID_RESPONSE)
        if observer is not None:
            observer.acknowledge(pid)
        while not deadline.expired:
            if observer is not None:
                observer.before_status()
            try:
                status = self._wire.request(
                    "GET", f"exec-status?pid={pid}", custody=custody, timeout=deadline.remaining()
                )
            except Exception:
                return _incomplete(Dispatch.SENT, io, Failure.DEADLINE if deadline.expired else Failure.OBSERVATION)
            if observer is not None:
                observer.record_status(status)
            report = _status_report(status, io, deadline)
            if report is not None:
                if deadline.expired:
                    return CarrierReport(
                        dispatch=report.dispatch,
                        completion=report.completion,
                        stdout=report.stdout,
                        stderr=report.stderr,
                        failure=Failure.DEADLINE,
                    )
                return report
            remaining = deadline.remaining()
            if remaining is None or remaining > 0:
                time.sleep(0.1 if remaining is None else min(0.1, remaining))
        return _incomplete(Dispatch.SENT, io, Failure.DEADLINE)


def _retention(io: CarrierIO) -> Retention:
    if isinstance(io.output, SinkOutput):
        return Retention.DELIVERED
    if io.sensitive:
        return Retention.SUPPRESSED
    return Retention.CAPTURED if isinstance(io.output, Capture) else Retention.DISCARDED


def _incomplete(dispatch: Dispatch, io: CarrierIO, failure: Failure) -> CarrierReport:
    return CarrierReport(
        dispatch=dispatch,
        stdout=CapturedOutput(provenance=Provenance.CARRIER_STDOUT, retention=_retention(io)),
        stderr=CapturedOutput(provenance=Provenance.MIXED_STDERR, retention=_retention(io)),
        failure=failure,
    )


def _wire_boolean(value: object) -> bool | None:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _status_report(status: dict[str, object], io: CarrierIO, deadline: Deadline) -> CarrierReport | None:
    """Validate external status fields without discarding independent exit facts."""
    exited = _wire_boolean(status.get("exited"))
    if exited is None:
        return _incomplete(Dispatch.SENT, io, Failure.INVALID_RESPONSE)
    completion_fields = {"exitcode", "signal", "out-data", "err-data", "out-truncated", "err-truncated"}
    if not exited:
        if completion_fields.intersection(status):
            return _incomplete(Dispatch.SENT, io, Failure.INVALID_RESPONSE)
        return None
    code, signal = status.get("exitcode"), status.get("signal")
    completion = None
    if "signal" not in status and type(code) is int and code >= 0:
        completion = ExitStatus(code=code)
    elif "exitcode" not in status and type(signal) is int and signal > 0:
        completion = ExitStatus(signal=signal)
    stdout, out_failure = _output(status, "out", io, Provenance.CARRIER_STDOUT)
    stderr, err_failure = _output(status, "err", io, Provenance.MIXED_STDERR)
    failure = Failure.INVALID_RESPONSE if completion is None else out_failure or err_failure
    report = CarrierReport(Dispatch.SENT, completion=completion, stdout=stdout, stderr=stderr, failure=failure)
    if isinstance(io.output, SinkOutput):
        # Invalid provider streams never reach the collector, while a valid
        # partial stream can still carry independently useful control evidence.
        values = tuple(
            value if isinstance(value, str) and invalid != Failure.INVALID_RESPONSE else ""
            for value, invalid in ((status.get("out-data", ""), out_failure), (status.get("err-data", ""), err_failure))
        )
        report = _deliver(values, report, io.output, deadline)
    return report


def _deliver(values: tuple[str, ...], report: CarrierReport, sinks: SinkOutput, deadline: Deadline) -> CarrierReport:
    """Fairly deliver a bounded provider response without retaining raw report bytes.

    QGA has already buffered these streams. Sink delivery establishes no live
    channel feature and never repeats the destructive terminal status read.
    """
    offsets = [0, 0]
    failure: Failure | None = None
    while any(offset < len(value) for offset, value in zip(offsets, values, strict=True)):
        if deadline.expired:
            failure = Failure.DEADLINE
            break
        progressed = False
        for index, sink in enumerate((sinks.stdout, sinks.stderr)):
            if offsets[index] == len(values[index]):
                continue
            if deadline.expired:
                failure = Failure.DEADLINE
                break
            chunk = values[index][offsets[index] : offsets[index] + 65_536].encode("ascii")
            try:
                count = try_write_to_sink(sink, memoryview(chunk))
            except SinkWriteError:
                failure = Failure.OUTPUT
                break
            if count is not None:
                offsets[index] += count
                progressed = True
        if failure is not None:
            break
        if not progressed:
            remaining = deadline.remaining()
            time.sleep(0.01 if remaining is None else min(0.01, remaining))
    return replace(
        report,
        stdout=replace(report.stdout, complete=report.stdout.complete and offsets[0] == len(values[0])),
        stderr=replace(report.stderr, complete=report.stderr.complete and offsets[1] == len(values[1])),
        failure=failure or report.failure,
    )


def _output(
    status: dict[str, object], prefix: str, io: CarrierIO, provenance: Provenance
) -> tuple[CapturedOutput, Failure | None]:
    value = status.get(f"{prefix}-data", "")
    truncated = _wire_boolean(status.get(f"{prefix}-truncated", False))
    retention = _retention(io)
    if not isinstance(value, str) or not value.isascii() or truncated is None:
        return CapturedOutput(provenance=provenance, retention=retention), Failure.INVALID_RESPONSE
    limit = io.output.max_bytes if isinstance(io.output, Capture) else None
    limited = limit is not None and len(value) > limit
    data = value.encode("ascii")[:limit] if retention == Retention.CAPTURED else b""
    return (
        CapturedOutput(data, complete=not truncated and not limited, provenance=provenance, retention=retention),
        Failure.OUTPUT_LIMIT if truncated or limited else None,
    )
