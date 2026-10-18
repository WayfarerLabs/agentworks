"""One acknowledged QGA helper, without replay or application output recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution._runtime_prerequisite import (
    MAX_RUNTIME_RECORD_BYTES,
    HelperClosureExpectation,
    RuntimePrefixSink,
    RuntimePrerequisiteState,
)
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    Deadline,
    Discard,
    ExitStatus,
    Failure,
    PreparedInvocation,
)
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection, _status_report

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody


class _Discard:
    @staticmethod
    def try_write(data: memoryview) -> int:
        return len(data)


class ProxmoxHelperDelivery(ProxmoxCarrier):
    """Passive per-call custody of the original route and known acknowledged PID."""

    def __init__(self, connection: ProxmoxConnection, expectation: HelperClosureExpectation) -> None:
        super().__init__(connection)
        self._expectation = expectation
        self._pid: int | None = None
        self._status_available = False
        self._closure_proven = False

    @property
    def closure_proven(self) -> bool:
        return self._closure_proven

    def acknowledge(self, pid: int) -> None:
        self._pid = pid
        self._status_available = True

    def before_status(self) -> None:
        # Even a failed GET may consume the terminal record. Never retry it.
        self._status_available = False

    def record_status(self, status: dict[str, object]) -> None:
        report = _status_report(status, CarrierIO(output=Discard()), Deadline.after(None))
        if report is None:
            self._status_available = True
            return
        if report.completion != ExitStatus(code=0) or report.failure is Failure.INVALID_RESPONSE:
            return
        value = status.get("out-data", "")
        if not isinstance(value, str) or not value.isascii():
            return
        expectation = self._expectation
        reader = RuntimePrefixSink(expectation.nonce, expectation.candidates, _Discard(), expectation.system_shim)
        reader.try_write(memoryview(value[:MAX_RUNTIME_RECORD_BYTES].encode("ascii")))
        self._closure_proven = reader.observation.state in (
            RuntimePrerequisiteState.READY,
            RuntimePrerequisiteState.MISSING,
            RuntimePrerequisiteState.UNUSABLE,
            RuntimePrerequisiteState.SHIM,
            RuntimePrerequisiteState.UNSUPPORTED_VERSION,
            RuntimePrerequisiteState.MISSING_MODULES,
        )
        reader.clear()

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        return self._execute(invocation, io=io, deadline=deadline, custody=custody, observer=self)

    def observe_closure(self, *, deadline: Deadline, custody: LocalDeliveryCustody) -> bool:
        if self._closure_proven and custody.settled:
            return True
        if deadline.expires_at is None or deadline.expired:
            raise ValidationError("Helper closure observation requires a fresh finite deadline")
        if not custody.settled:
            raise StateError("Helper closure retains unsettled local delivery")
        if self._pid is None or not self._status_available:
            return False
        self.before_status()
        try:
            status = self._wire.request(
                "GET", f"exec-status?pid={self._pid}", custody=custody, timeout=deadline.remaining()
            )
        except Exception:
            return False
        self.record_status(status)
        return self._closure_proven
