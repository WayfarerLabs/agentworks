"""Effective call budgets precede provider admission in the shared VM context.

Provider observations and SSH credential delivery are synthetic; the common
kernel, held WSL operation, SQLite claim and packed helpers remain intact.
"""

from __future__ import annotations

import sys
import time

import pytest

from agentworks.errors import ExternalError, LimitExceededError, StateError, ValidationError
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHCarrier
from agentworks.execution.models import Command, JobRef, Lifetime
from agentworks.execution.profiles import Protection
from agentworks.vms._native_execution_access import OwnedNativePlatformAccess
from agentworks.vms._wsl2_native_access import WSL2OwnedNativePlatformAccess
from tests.vms.test_canonical_context import canonical as canonical
from tests.vms.test_native_operation import _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="packed Linux helper fixture")


def record_provider_reads(monkeypatch: pytest.MonkeyPatch, access: OwnedNativePlatformAccess) -> list[Deadline]:
    reads: list[Deadline] = []
    power, locator = access.observe_power, access.observe_locator

    def observe_power(deadline):
        reads.append(deadline)
        return power(deadline)

    def observe_locator(deadline):
        reads.append(deadline)
        return locator(deadline)

    monkeypatch.setattr(access, "observe_power", observe_power)
    monkeypatch.setattr(access, "observe_locator", observe_locator)
    return reads


@pytest.mark.parametrize("method", ["direct", "start", "observe"])
@pytest.mark.parametrize("deadline", [Deadline(0), "invalid"])
def test_invalid_or_expired_call_budget_precedes_provider_and_ssh(canonical, database, monkeypatch, method, deadline):
    p = canonical
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        reads = record_provider_reads(monkeypatch, p.accesses[0])
        before = len(p.connections)
        with pytest.raises(ValidationError):
            if method == "direct":
                execution.run(Command(["/bin/true"]), profile=Protection.DIRECT, deadline=deadline)
            elif method == "start":
                execution.start(
                    Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, deadline=deadline
                )
            else:
                execution.observe(JobRef("f" * 32), deadline=deadline)
        assert not reads and len(p.connections) == before
        assert not execution._operation.active_inline_calls and not execution._operation.managed_runs
    assert database.operations.inspect(_scope()) is None


def test_direct_provider_and_dispatch_use_exact_shorter_deadline(canonical, database, monkeypatch):
    p = canonical
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        reads = record_provider_reads(monkeypatch, p.accesses[0])
        selected = Deadline.after(5)
        assert execution.run(Command(["/bin/true"]), profile=Protection.DIRECT, deadline=selected).ok
        assert reads and all(item is selected for item in reads)
        assert p.route.local_deadlines and all(item is selected for item in p.route.local_deadlines)
    assert database.operations.inspect(_scope()) is None


def test_managed_start_and_job_reads_use_exact_shorter_deadline(canonical, database, monkeypatch):
    from tests.execution.test_managed_execution_access import Delivery

    p = canonical
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        reads = record_provider_reads(monkeypatch, p.accesses[0])
        ordinary, managed = SSHCarrier.execute, Delivery()

        def execute(carrier, invocation, *, io, deadline, custody):
            try:
                managed.selected(io)
            except (AssertionError, AttributeError):
                return ordinary(carrier, invocation, io=io, deadline=deadline, custody=custody)
            return managed.execute(invocation, io=io, deadline=deadline, custody=custody)

        monkeypatch.setattr(SSHCarrier, "execute", execute)
        selected = Deadline.after(5)
        reference = execution.start(
            Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, deadline=selected
        )
        assert reads and all(item is selected for item in reads)
        reads.clear()
        execution.observe(reference, deadline=selected)
        assert reads and all(item is selected for item in reads)
    assert managed.start.calls == 1
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("lifetime", [Lifetime.OPERATION, Lifetime.INDEPENDENT])
def test_managed_drift_precedes_initial_clock_or_resource_launch(canonical, database, monkeypatch, lifetime):
    """The injected availability fact does not prove WSL independent-job safety."""
    p = canonical
    monkeypatch.setattr(
        WSL2OwnedNativePlatformAccess,
        "independent_availability",
        property(lambda _: _IndependentJobAvailability.NO_IDLE_STOP),
    )
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        before = len(p.connections)
        database.update_vm_tailscale("box", "100.64.0.8")
        with pytest.raises(StateError):
            execution.start(
                Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=lifetime, deadline=Deadline.after(5)
            )
        assert len(p.connections) == before and not execution._operation.managed_runs
        assert database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
    assert database.operations.inspect(_scope()) is None


def test_provider_expiry_uses_call_budget_without_helper_borrow_or_dispatch(canonical, database, monkeypatch):
    p = canonical
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        access = p.accesses[0]
        now = time.monotonic()
        selected = Deadline(now + 2)
        observed = []

        def power(deadline):
            observed.append(deadline)
            monkeypatch.setattr(time, "monotonic", lambda: now + 3)
            return access.platform.observe_execution_power(
                access.vm, access.ctx, deadline=deadline, custody=access.custody
            )

        def deny_borrow():
            pytest.fail("route refusal borrowed helper ownership")

        monkeypatch.setattr(access, "observe_power", power)
        before = len(p.connections)
        with monkeypatch.context() as m:
            m.setattr(execution._operation._owner, "borrow", deny_borrow)
            with pytest.raises(LimitExceededError):
                execution.run(Command(["/bin/true"]), profile=Protection.DIRECT, deadline=selected)
        assert observed == [selected] and observed[0] is selected
        assert selected.expired and not p.deadline.expired
        assert len(p.connections) == before and not execution._operation.active_inline_calls
    assert database.operations.inspect(_scope()) is None


def test_expired_file_budget_keeps_deadline_reduction_without_provider_or_ssh(canonical, database, monkeypatch):
    p = canonical
    with p.operation() as ctx:
        files = ctx.admin_execution_target().files()
        reads = record_provider_reads(monkeypatch, p.accesses[0])
        before = len(p.connections)
        monkeypatch.setattr(files, "_deadline", lambda: Deadline(0))
        with pytest.raises(ExternalError) as caught:
            files.stat(p.root / "missing")
        assert caught.value.details is not None and caught.value.details.reason.value == "deadline"
        assert not reads and len(p.connections) == before
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("account", ["admin", "agent"])
def test_both_file_views_refuse_canonical_selection_drift_before_dispatch(canonical, database, account):
    p = canonical
    database.insert_agent("worker", "box", "worker")
    p.route.accounts.identities["worker"] = p.route.accounts.identities["admin"]
    with p.operation(agent_name="worker") as ctx:
        target = ctx.admin_execution_target() if account == "admin" else ctx.agent_execution_target()
        files = target.files()
        before = len(p.connections)
        database.update_vm_tailscale("box", "100.64.0.8")
        with pytest.raises(StateError):
            files.stat(p.root / "missing")
        assert len(p.connections) == before
    assert database.operations.inspect(_scope()) is None
