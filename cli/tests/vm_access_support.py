"""A recording ``VMAccess`` for manager tests, which need no ssh.

``RecordingVMAccess`` subclasses ``VMAccess``, overrides its four operations to
record what was asked and return a canned outcome, and spawns nothing. The
``AccessRecorder`` stands in for ``agentworks.execution.vm_access``,
``enrollment_access`` and ``resolve_ssh``, keeping one access object per
identity (and one for enrollment) and one ordered call list, so a test can
assert both what each identity was asked and in what order. Manager tests built
on it run on every CI host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentworks.execution import trust
from agentworks.execution.access import Admin, Agent, Identity, VMAccess
from agentworks.execution.client import SSHClient
from agentworks.execution.results import Completed, Result
from agentworks.execution.trust import TrustMode
from tests.ssh_fixtures import fake_host_key

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import BinaryIO

    from agentworks.execution.requests import Forward, LoginShell, RunRequest


@dataclass(frozen=True)
class Call:
    """One operation asked of an access object."""

    identity: Identity
    operation: str
    request: Any
    options: dict[str, Any] = field(default_factory=dict)
    enrolling: bool = False


class RecordingVMAccess(VMAccess):
    """Records ``run``, ``stream``, ``attach`` and ``forward`` and returns canned outcomes.

    ``run_outcomes`` is consumed in order, then ``Completed(0)`` results follow.
    An outcome that is an exception is raised. ``stream_status``,
    ``attach_status`` and ``forward_status`` are returned (or raised) by their
    operation.
    """

    def __init__(
        self, calls: list[Call], identity: Identity, *, vm_name: str = "box", trust_mode: TrustMode = TrustMode.VM
    ) -> None:
        super().__init__(
            vm_name=vm_name,
            host="100.64.0.9",
            user="agentworks" if isinstance(identity, Admin) else identity.linux_user,
            identity=identity,
            identity_file=Path("/nonexistent/key"),
            trust_mode=trust_mode,
        )
        self.calls = calls
        self.run_outcomes: list[Result | BaseException] = []
        self.stream_status: int | BaseException = 0
        self.attach_status: int | BaseException = 0
        self.forward_status: int | BaseException = 0

    def run(self, request: RunRequest, *, check: bool = False) -> Result:
        self.calls.append(
            Call(self.identity, "run", request, {"check": check}, enrolling=self.trust_mode is TrustMode.ENROLL)
        )
        outcome = self.run_outcomes.pop(0) if self.run_outcomes else Result(Completed(0))
        if isinstance(outcome, BaseException):
            raise outcome
        enrolls = self.trust_mode is TrustMode.ENROLL and isinstance(outcome.completion, Completed)
        if enrolls and not trust.has_record(self.vm_name):
            # What accept-new does when a connection reaches sshd and none is recorded.
            trust.write_records(self.vm_name, [fake_host_key()])
        if check and outcome.require_completed(self.vm_name) != 0:
            raise AssertionError("a canned non-zero result under check=True; script an exception instead")
        return outcome

    def stream(self, request: RunRequest, *, stdin: BinaryIO | None, stdout: BinaryIO, stderr: BinaryIO) -> int:
        self.calls.append(Call(self.identity, "stream", request, {"stdin": stdin, "stdout": stdout, "stderr": stderr}))
        return self._status(self.stream_status)

    def attach(self, request: LoginShell) -> int:
        self.calls.append(Call(self.identity, "attach", request))
        return self._status(self.attach_status)

    def forward(self, forwards: Sequence[Forward], *, verbose: bool = False) -> int:
        self.calls.append(Call(self.identity, "forward", tuple(forwards), {"verbose": verbose}))
        return self._status(self.forward_status)

    @staticmethod
    def _status(outcome: int | BaseException) -> int:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class AccessRecorder:
    """Stands in for ``agentworks.execution.vm_access``: one recording access per identity."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.built: list[tuple[str, Identity]] = []
        self.rows: list[Any] = []
        self.logs: list[Any] = []
        self.client = SSHClient("/usr/bin/ssh", (9, 2))
        self._accesses: dict[Identity, RecordingVMAccess] = {}
        self._enrollment: RecordingVMAccess | None = None
        self.enrolled: list[str] = []

    def vm_access(self, vm: Any, config: Any, identity: Identity, *, log: Any = None) -> RecordingVMAccess:
        self.built.append((vm.name, identity))
        self.rows.append(vm)
        self.logs.append(log)
        return self.access(identity, vm_name=vm.name)

    def enrollment_access(self, vm: Any, config: Any, *, log: Any = None) -> RecordingVMAccess:
        """Stands in for ``execution.enrollment_access``; its first completed run records a host key."""
        self.enrolled.append(vm.name)
        self.rows.append(vm)
        self.logs.append(log)
        return self.enrollment(vm_name=vm.name)

    def enrollment(self, *, vm_name: str = "box") -> RecordingVMAccess:
        """The admin's Enroll-mode access, created on first use; its calls join the one ordered list."""
        if self._enrollment is None:
            self._enrollment = RecordingVMAccess(self.calls, Admin(), vm_name=vm_name, trust_mode=TrustMode.ENROLL)
        return self._enrollment

    def access(self, identity: Identity | None = None, *, vm_name: str = "box") -> RecordingVMAccess:
        """The access object for ``identity`` (the admin by default), created on first use."""
        identity = identity or Admin()
        if identity not in self._accesses:
            self._accesses[identity] = RecordingVMAccess(self.calls, identity, vm_name=vm_name)
        return self._accesses[identity]

    @property
    def operations(self) -> list[tuple[str, str]]:
        """``(identity label, operation)`` for each recorded call, in order."""
        return [(_label(call), call.operation) for call in self.calls]

    def requests(self, operation: str) -> list[Any]:
        return [call.request for call in self.calls if call.operation == operation]


def _label(call: Call) -> str:
    if call.enrolling:
        return "enroll"
    return "admin" if isinstance(call.identity, Admin) else call.identity.name


__all__ = ["AccessRecorder", "Admin", "Agent", "Call", "RecordingVMAccess"]
