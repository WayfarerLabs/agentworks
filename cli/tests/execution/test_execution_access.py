"""Private foreground access admission and owner-backed behavior."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
    Retention,
)
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.models import Command, Input, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import CheckedExecutionError, ExitCode
from agentworks.operations import OperationOwner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inline execution requires Linux")
type Bound = tuple[Database, OperationOwner, ExecutionOperation, "LocalCarrier", ExecutionAccess, Deadline]


class LocalCarrier:
    def __init__(self) -> None:
        self.calls = 0
        self.deadlines: list[Deadline] = []

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        self.deadlines.append(deadline)
        result = run_process(list(invocation.argv), io=io, deadline=deadline)
        completion = None
        if result.exit_status is not None:
            completion = (
                ExitStatus(signal=-result.exit_status)
                if result.exit_status < 0
                else ExitStatus(code=result.exit_status)
            )
        return CarrierReport(
            Dispatch.SENT if result.started else Dispatch.NOT_SENT,
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )


class UnknownCarrier(LocalCarrier):
    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, io
        self.calls += 1
        self.deadlines.append(deadline)
        return CarrierReport(Dispatch.UNKNOWN)


def _access(
    operation: ExecutionOperation,
    carrier: LocalCarrier,
    deadline: Deadline,
    *,
    target_os: RuntimeTargetOS = RuntimeTargetOS.LINUX,
) -> ExecutionAccess:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    plan = IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)
    return ExecutionAccess(
        operation,
        carrier,
        runtime_selection=RuntimeSelection(target_os, sys.executable if target_os is RuntimeTargetOS.LINUX else None),
        ordinary_plan=plan,
        elevated_plan=None,
        entity_kind="vm",
        entity_name="execution-access-vm",
        deadline=lambda: deadline,
    )


@pytest.fixture
def bound(tmp_path: Path) -> Iterator[Bound]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "execution-access-vm"),
        "execution-access",
    )
    operation = ExecutionOperation(owner)
    carrier = LocalCarrier()
    deadline = Deadline.after(30)
    access = _access(operation, carrier, deadline)
    try:
        yield database, owner, operation, carrier, access, deadline
    finally:
        database.close()


@pytest.mark.parametrize("status", [0, 255])
def test_command_runs_with_exact_default_deadline_and_exit_status(bound: Bound, status: int) -> None:
    _, owner, _, carrier, access, deadline = bound
    result = access.run(Command(("/bin/sh", "-c", f"printf data; exit {status}")), profile=Protection.DIRECT)
    assert result.status == ExitCode(status)
    assert result.stdout.data == b"data"
    assert carrier.deadlines[0] is deadline
    assert carrier.calls == 1
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_script_binary_stdin_discard_and_sensitive_suppression(
    bound: Bound,
) -> None:
    _, owner, _, carrier, access, _ = bound
    script = Script("cat", Shell.SH)
    payload = b"\x00\xffsecret"
    override = Deadline.after(20)
    captured = access.run(script, profile=Protection.DIRECT, stdin=Input.bytes(payload), deadline=override)
    assert captured.stdout.data == payload
    discarded = access.run(script, profile=Protection.DIRECT, stdin=Input.bytes(payload), output=Output.discard())
    assert discarded.stdout.retention is Retention.DISCARDED
    assert discarded.stdout.data == b""
    sensitive = access.run(script, profile=Protection.DIRECT, stdin=Input.sensitive(payload))
    assert sensitive.stdout.retention is Retention.SUPPRESSED
    assert sensitive.stdout.data == b""
    request_sensitive = access.run(script, profile=Protection.DIRECT, stdin=Input.bytes(payload), sensitive=True)
    assert request_sensitive.stdout.retention is Retention.SUPPRESSED
    assert request_sensitive.stdout.data == b""
    assert carrier.deadlines[0] is override
    assert carrier.calls == 4
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_checked_nonzero_uses_bound_diagnostic_identity(
    bound: Bound,
) -> None:
    _, owner, _, _, access, _ = bound
    with pytest.raises(CheckedExecutionError) as failure:
        access.run(Command(("/bin/sh", "-c", "exit 7")), profile=Protection.DIRECT, check=True)
    assert failure.value.result.status == ExitCode(7)
    assert failure.value.entity_kind == "vm"
    assert failure.value.entity_name == "execution-access-vm"
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    "options",
    [
        {"profile": Protection.MANAGED},
        {"profile": Protection.DIRECT, "lifetime": Lifetime.INDEPENDENT},
        {"profile": Protection.DIRECT, "sudo": True},
        {"profile": Protection.DIRECT, "output": Output.capture(4_097)},
        {"profile": Protection.DIRECT, "deadline": Deadline.after(0)},
        {"profile": Protection.DIRECT, "deadline": "invalid"},
        {"profile": Protection.DIRECT, "sudo": 1},
        {"profile": Protection.DIRECT, "stdin": b"raw"},
        {"profile": Protection.DIRECT, "env": {"invalid-name": "value"}},
        {"profile": Protection.DIRECT, "cwd": "relative"},
    ],
)
def test_known_refusals_do_not_borrow_or_dispatch(
    bound: Bound,
    options: dict[str, object],
) -> None:
    database, owner, operation, carrier, access, _ = bound
    with pytest.raises((ValidationError, StateError)):
        access.run(Command(("/bin/true",)), **options)  # type: ignore[arg-type]
    assert carrier.calls == 0
    assert operation.active_inline_calls == ()
    assert operation.unfinished_inline_executions == ()
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.RESERVED
    owner.close()


def test_script_startup_and_non_linux_refuse_before_dispatch(
    bound: Bound,
) -> None:
    _, owner, operation, carrier, access, deadline = bound
    with pytest.raises(ValidationError):
        access.run(Script("true", Shell.SH, login=True), profile=Protection.DIRECT)
    non_linux = _access(operation, carrier, deadline, target_os=RuntimeTargetOS.DARWIN)
    with pytest.raises(StateError):
        non_linux.run(Command(("/bin/true",)), profile=Protection.DIRECT)
    assert carrier.calls == 0 and operation.active_inline_calls == ()
    assert deadline is not None
    owner.close()


def test_uncertain_result_retains_owner(
    bound: Bound,
) -> None:
    _, owner, operation, _, _, deadline = bound
    carrier = UnknownCarrier()
    access = _access(operation, carrier, deadline)
    result = access.run(Command(("/bin/true",)), profile=Protection.DIRECT)
    assert result.dispatch is Dispatch.UNKNOWN
    assert carrier.calls == 1
    assert len(operation.unfinished_inline_executions) == 1
    with pytest.raises(StateError):
        owner.close()


def test_import_does_not_load_retirement_roots() -> None:
    code = """
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
roots = ('agentworks.transports', 'agentworks.ssh', 'agentworks.remote_exec',
         'agentworks.harness_setup.runner', 'agentworks.native_files',
         'agentworks.plugins.proxmox.transport')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == root or fullname.startswith(root + '.') for root in roots):
            raise AssertionError(fullname)
        return None
sys.meta_path.insert(0, Guard())
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.models import Input, Output, Lifetime
from agentworks.execution.profiles import Protection
assert not any(name == root or name.startswith(root + '.') for name in sys.modules for root in roots)
"""
    subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[2])],
        check=True,
        capture_output=True,
        timeout=10,
    )
