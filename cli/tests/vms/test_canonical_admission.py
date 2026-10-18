"""Effective call budgets precede provider admission in the shared VM context.

Provider and delivery boundaries are synthetic; the common kernel, owned
platform access, SQLite claim and packed helpers remain intact.
"""

from __future__ import annotations

import sys
import time

import pytest

from agentworks.errors import ExternalError, LimitExceededError, StateError, ValidationError
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHCarrier
from agentworks.execution.files import Create, FileKind, JsonStrategy, NewMetadata
from agentworks.execution.models import Command, JobRef, Lifetime, Output
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


@pytest.mark.parametrize("lifetime", [Lifetime.OPERATION, Lifetime.INDEPENDENT])
@pytest.mark.parametrize("invalid", ["cwd", "capture"])
def test_invalid_managed_body_refuses_before_provider_even_during_outage(
    canonical, database, monkeypatch, lifetime, invalid
):
    """The injected fact permits testing RESOURCE input, not WSL job safety."""
    p = canonical
    monkeypatch.setattr(
        WSL2OwnedNativePlatformAccess,
        "independent_availability",
        property(lambda _: _IndependentJobAvailability.NO_IDLE_STOP),
    )
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        reads = []

        def outage(deadline):
            reads.append(deadline)
            raise ExternalError("synthetic provider outage")

        monkeypatch.setattr(p.accesses[0], "observe_power", outage)
        monkeypatch.setattr(p.accesses[0], "observe_locator", outage)
        before = len(p.connections)
        with pytest.raises(ValidationError):
            execution.start(
                Command(["/bin/true"]),
                profile=Protection.MANAGED,
                lifetime=lifetime,
                cwd="relative" if invalid == "cwd" else None,
                output=Output.capture(1 << 30) if invalid == "capture" else Output.capture(),
            )
        assert not reads and len(p.connections) == before
        assert not execution._operation.managed_runs and not execution._operation.active_inline_calls
        assert database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize(
    "invalid",
    [
        "upload-size",
        "upload-condition",
        "upload-mode",
        "json-document",
        "json-strategy",
        "json-limit",
        "json-mode",
        "metadata-owner",
        "metadata-mode",
        "remove-kind",
        "remove-revision",
    ],
)
def test_invalid_file_request_refuses_before_provider_even_during_outage(canonical, database, monkeypatch, invalid):
    from pathlib import Path

    p = canonical

    class Source:
        calls = 0

        def read(self, limit):
            self.calls += 1
            pytest.fail("invalid upload consumed its caller-owned source")

    source = Source()
    metadata = NewMetadata("admin", "admin", 0o600)
    with p.operation() as ctx:
        files = ctx.admin_execution_target().files()
        path = p.root / "file"
        revision = None
        if invalid == "remove-kind":
            Path(p.root).mkdir()
            Path(path).write_bytes(b"untouched")
            revision = files.stat(path).revision
        reads = []

        def outage(deadline):
            reads.append(deadline)
            raise ExternalError("synthetic provider outage")

        monkeypatch.setattr(p.accesses[0], "observe_power", outage)
        monkeypatch.setattr(p.accesses[0], "observe_locator", outage)
        before = len(p.connections)
        with pytest.raises(ValidationError):
            if invalid.startswith("upload-"):
                files.upload(
                    path,
                    source,
                    size=-1 if invalid == "upload-size" else 0,
                    condition="invalid" if invalid == "upload-condition" else Create(),
                    create_metadata=NewMetadata("admin", "admin", 0o4000) if invalid == "upload-mode" else metadata,
                )
            elif invalid.startswith("json-"):
                files.update_json(
                    path,
                    [] if invalid == "json-document" else {},
                    strategy="invalid" if invalid == "json-strategy" else JsonStrategy.REPLACE,
                    create=True,
                    create_metadata=NewMetadata("admin", "admin", 0o4000) if invalid == "json-mode" else metadata,
                    max_depth=0 if invalid == "json-limit" else 64,
                )
            elif invalid.startswith("metadata-"):
                files.set_metadata(
                    path,
                    owner="" if invalid == "metadata-owner" else "admin",
                    group="admin",
                    mode=-1 if invalid == "metadata-mode" else 0o600,
                )
            else:
                files.remove(
                    path,
                    expected_kind="invalid" if invalid == "remove-kind" else FileKind.REGULAR,
                    expected=revision if invalid == "remove-kind" else "invalid",
                )
        assert not reads and len(p.connections) == before and source.calls == 0
        if revision is not None:
            assert Path(path).read_bytes() == b"untouched"
    assert database.operations.inspect(_scope()) is None


def test_unavailable_resource_lifetime_refuses_before_provider(canonical, database, monkeypatch):
    p = canonical
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        assert execution._operation._native_binding._independent_availability is None
        reads = record_provider_reads(monkeypatch, p.accesses[0])
        before = len(p.connections)
        with pytest.raises(StateError):
            execution.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT)
        assert not reads and len(p.connections) == before and not execution._operation.managed_runs
        assert database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0
    assert database.operations.inspect(_scope()) is None


def test_valid_file_request_checks_route_before_packed_dispatch(canonical, database, monkeypatch):
    p = canonical
    with p.operation() as ctx:
        files = ctx.admin_execution_target().files()
        reads = record_provider_reads(monkeypatch, p.accesses[0])
        ordinary = SSHCarrier.execute
        dispatched = []

        def execute(carrier, invocation, *, io, deadline, custody):
            assert reads and all(item is deadline is p.deadline for item in reads)
            dispatched.append(deadline)
            return ordinary(carrier, invocation, io=io, deadline=deadline, custody=custody)

        monkeypatch.setattr(SSHCarrier, "execute", execute)
        assert files.stat(p.root / "missing") is None
        assert dispatched
    assert database.operations.inspect(_scope()) is None
