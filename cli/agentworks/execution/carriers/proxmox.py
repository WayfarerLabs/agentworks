"""Independent, single-attempt Proxmox REST delivery for prepared invocations.

PVE converts QGA's base64 streams into JSON strings. This boundary accepts only
ASCII-armored preparation output, not arbitrary guest bytes disguised as text.
Each HTTP exchange runs in an owned subprocess so its deadline also bounds DNS,
TLS setup and continuously arriving responses, not just socket inactivity.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
from contextlib import suppress
from dataclasses import asdict, dataclass, field

from agentworks.errors import ValidationError
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
    PreparedInvocation,
    Provenance,
    Retention,
)

_MAX_INPUT_BYTES = 65_536
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ProxmoxConnection:
    """Explicit REST authority for one VM, without credential discovery."""

    api_url: str
    node: str
    vmid: int
    token_id: str = field(repr=False)
    token_secret: str = field(repr=False)
    verify_tls: bool = True

    def __post_init__(self) -> None:
        """Validate connection inputs supplied by the composition boundary."""
        parsed = urllib.parse.urlsplit(self.api_url)
        if (
            parsed.scheme != "https"
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
        if (
            not self.token_id
            or not self.token_secret
            or any(char in self.token_id + self.token_secret for char in "\r\n")
        ):
            raise ValidationError("Proxmox requires a valid API token")


class _WireFailure(Exception):
    """A REST response is unavailable or cannot establish execution evidence."""


class _ProxmoxWire:
    def __init__(self, connection: ProxmoxConnection) -> None:
        self._connection = connection

    def request(
        self, method: str, suffix: str, *, body: bytes | None = None, timeout: float | None
    ) -> dict[str, object]:
        """Own one HTTP worker until completion, timeout or propagated interruption."""
        started = time.monotonic()
        payload = json.dumps(
            {
                "connection": asdict(self._connection),
                "method": method,
                "suffix": suffix,
                "body": body.decode("ascii") if body is not None else None,
                "timeout": timeout,
            }
        ).encode("ascii")
        process = subprocess.Popen(
            [sys.executable, "-I", "-m", "agentworks.execution.carriers._proxmox_http"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
            encoded, _ = process.communicate(payload, timeout=remaining)
        except BaseException:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.kill()
            process.communicate()
            raise
        if process.returncode != 0 or len(encoded) > _MAX_RESPONSE_BYTES:
            raise _WireFailure("Proxmox request or response failed")
        try:
            parsed = json.loads(encoded)
        except ValueError:
            raise _WireFailure("Proxmox returned invalid JSON") from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), dict):
            raise _WireFailure("Proxmox returned an invalid response envelope")
        return dict(parsed["data"])


class ProxmoxCarrier:
    """Deliver prepared ASCII bootstrap work through the QGA REST endpoints.

    Completion establishes the bootstrap's exit, not a nested application's.
    Deadlines cover preparation, worker startup and each HTTP request. Stopping
    the local HTTP worker does not cancel a command already submitted to QGA.
    No status read is retried after failure: a terminal QGA read reaps its record.
    """

    def __init__(self, connection: ProxmoxConnection) -> None:
        self._wire = _ProxmoxWire(connection)

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        input_data = io.input.data if isinstance(io.input, FiniteInput) else b""
        if not input_data.isascii() or any(not arg.isascii() for arg in invocation.argv):
            raise ValidationError("Proxmox proof delivery requires ASCII-armored preparation")
        if len(input_data) > _MAX_INPUT_BYTES:
            raise ValidationError("Prepared Proxmox input exceeds the provider input limit")
        body = json.dumps({"command": invocation.argv, "input-data": input_data.decode("ascii")}).encode("ascii")
        if deadline.expired:
            return _incomplete(Dispatch.NOT_SENT, io, Failure.DEADLINE)
        try:
            response = self._wire.request("POST", "exec", body=body, timeout=deadline.remaining())
        except Exception:
            return _incomplete(Dispatch.UNKNOWN, io, Failure.DEADLINE if deadline.expired else Failure.DISPATCH)
        pid = response.get("pid")
        if type(pid) is not int or pid <= 0:
            return _incomplete(Dispatch.UNKNOWN, io, Failure.INVALID_RESPONSE)
        while not deadline.expired:
            try:
                status = self._wire.request("GET", f"exec-status?pid={pid}", timeout=deadline.remaining())
            except Exception:
                return _incomplete(Dispatch.SENT, io, Failure.DEADLINE if deadline.expired else Failure.OBSERVATION)
            report = _status_report(status, io)
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


def _status_report(status: dict[str, object], io: CarrierIO) -> CarrierReport | None:
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
    return CarrierReport(Dispatch.SENT, completion=completion, stdout=stdout, stderr=stderr, failure=failure)


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
