"""Scripted stopped startup with real SQLite custody and local Linux body helpers.

Provider observations are doubles, not native Proxmox acceptance evidence.
"""

from __future__ import annotations

import builtins
import sys
from contextlib import AbstractContextManager
from pathlib import PurePosixPath

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.db import Database, LifecycleObligationState, OperationOwnership, VMStatus
from agentworks.db.operations import LifecycleObligation, OperationRepository
from agentworks.errors import StateError
from agentworks.execution._proxmox_activation import OBLIGATION_KIND, TaskOutcome, decode_activation_payload
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.proxmox import _ProxmoxWire
from agentworks.execution.models import Command
from agentworks.execution.profiles import Protection
from agentworks.operations import OperationOwner
from agentworks.vms import _native_operation as native
from agentworks.vms._native_operation import NativeVMOperation, NativeVMOperationControlFact, native_vm_operation
from tests.vms.test_proxmox_native_operation import _install, _scope
from tests.vms.test_proxmox_native_operation import database as database

UPID = "UPID:node:00000001:00000002:00000003:qmstart:101:token:"
INFO: dict[str, object] = {"result": {"version": "9.0", "supported_commands": []}}
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux packed helper proof")


def task(*, phase="stopped", outcome="OK", upid=UPID) -> dict[str, object]:
    return {
        "upid": upid,
        "node": "node",
        "pid": 1,
        "pstart": 2,
        "starttime": 3,
        "type": "hastart" if "hastart" in upid else "qmstart",
        "id": "101",
        "user": "token",
        "status": phase,
        "exitstatus": outcome,
    }


class Startup:
    def __init__(self, database: Database, monkeypatch) -> None:
        self.database = database
        self.platform, self.route = _install(database, monkeypatch)
        self.events: list[str] = []
        self.receipt: str | BaseException = UPID
        self.tasks: list[dict[str, object] | BaseException] = [task(phase="running"), task()]
        self.infos: list[dict[str, object] | BaseException] = [OSError("agent unavailable"), INFO]
        self.power: dict[str, object] = {"status": "running"}
        self.owner: OperationOwnership | None = None
        self.activation_id: str | None = None
        self.workflow: native._Workflow | None = None
        self.responsive = False
        monkeypatch.setattr(self.platform, "observe_execution_power", lambda *args, **kwargs: VMStatus.STOPPED)
        for method in ("start", "stop"):
            monkeypatch.setattr(self.platform, method, lambda *args, **kwargs: pytest.fail("legacy lifecycle called"))
        monkeypatch.setattr(_ProxmoxWire, "request_vm_start", lambda wire, **kwargs: self.start(wire, **kwargs))
        monkeypatch.setattr(_ProxmoxWire, "request_task_status", lambda wire, upid, **kwargs: self.poll(upid, **kwargs))
        monkeypatch.setattr(_ProxmoxWire, "request_power", lambda wire, **kwargs: self.current_power(**kwargs))
        monkeypatch.setattr(_ProxmoxWire, "request_guest_info", lambda wire, **kwargs: self.info(**kwargs))
        guest_execute = self.route.guest.execute

        def guest(*args, **kwargs):
            assert self.responsive
            self.events.append("guest")
            return guest_execute(*args, **kwargs)

        monkeypatch.setattr(self.route.guest, "execute", guest)
        close = native._Workflow.close

        def closing(workflow, **kwargs):
            self.workflow = workflow
            return close(workflow, **kwargs)

        monkeypatch.setattr(native._Workflow, "close", closing)

    def activation_row(self) -> LifecycleObligation:
        claim = self.database.operations.inspect(_scope())
        assert claim is not None
        if self.owner is None:
            self.owner = claim.ownership
        assert claim.ownership == self.owner
        if self.activation_id is None:
            pending = next(
                row
                for row in self.database.operations.list_pending_lifecycle_obligations(claim.ownership)
                if row.obligation_kind == OBLIGATION_KIND
            )
            self.activation_id = pending.obligation_id
        row = self.database.operations.inspect_lifecycle_obligation(claim.ownership, self.activation_id)
        assert row is not None
        return row

    def start(self, wire, *, timeout, custody) -> str:
        assert timeout > 0 and wire._connection.vmid == 101
        row = self.activation_row()
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_activation_payload(row.payload).upid is None
        assert self.route.resolutions == 1 and not self.route.guest.calls and not self.route.accounts.calls
        self.events.append("start")
        if isinstance(self.receipt, BaseException):
            raise self.receipt
        return self.receipt

    def poll(self, upid, *, timeout, custody) -> dict[str, object]:
        assert timeout > 0 and upid == self.receipt
        assert decode_activation_payload(self.activation_row().payload).upid == upid
        assert not self.route.guest.calls and not self.route.accounts.calls
        self.events.append("task")
        response = self.tasks.pop(0) if len(self.tasks) > 1 else self.tasks[0]
        if isinstance(response, BaseException):
            raise response
        return response

    def current_power(self, *, timeout, custody) -> dict[str, object]:
        assert timeout > 0 and self.activation_row().state is LifecycleObligationState.RESOLVED
        self.events.append("power")
        return self.power

    def info(self, *, timeout, custody) -> dict[str, object]:
        assert timeout > 0 and self.activation_row().state is LifecycleObligationState.RESOLVED
        assert not self.route.guest.calls and not self.route.accounts.calls
        self.events.append("info")
        response = self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]
        if isinstance(response, BaseException):
            raise response
        self.responsive = response == INFO
        return response

    def operation(self, root, deadline) -> AbstractContextManager[NativeVMOperation]:
        return native_vm_operation(
            self.database,
            "box",
            self.platform,
            RunContext(),
            deadline=deadline,
            trusted_root=PurePosixPath(root),
        )


@pytest.mark.parametrize("outcome,expected", [("OK", TaskOutcome.OK), ("WARNINGS: 2", TaskOutcome.WARNINGS)])
def test_stopped_start_prepares_file_and_direct_body_under_same_owner(
    database, tmp_path, monkeypatch, outcome, expected
):
    startup = Startup(database, monkeypatch)
    startup.tasks[-1] = task(outcome=outcome)
    original_import = builtins.__import__
    retired = (
        "agentworks.transports",
        "agentworks.ssh",
        "agentworks.remote_exec",
        "agentworks.harness_setup.runner",
        "agentworks.native_files",
        "agentworks.plugins.proxmox.transport",
    )

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        requested = (name, *(f"{name}.{member}" for member in fromlist or ()))
        assert not any(
            candidate == root or candidate.startswith(root + ".") for candidate in requested for root in retired
        )
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    deadline = Deadline.after(30)
    source = tmp_path / "source"
    source.write_bytes(b"source")
    effect = tmp_path / "effect"
    with startup.operation(tmp_path, deadline) as views:
        assert views.owner.ownership == startup.owner
        assert views.file_operation._owner is views.execution_operation._owner is views.owner
        assert views.file_operation._bootstrap is views.execution_operation._bootstrap
        metadata = views.files.stat(PurePosixPath(source))
        assert metadata is not None
        views.files.remove(PurePosixPath(source), expected_kind=metadata.kind, expected=metadata.revision)
        assert views.execution.run(Command(["/usr/bin/touch", str(effect)]), profile=Protection.DIRECT, check=True).ok
    assert effect.exists() and not source.exists()
    assert startup.route.ownership == startup.owner and startup.route.resolutions == 1
    assert startup.events == ["start", "task", "task", "power", "info", "info", "guest"]
    assert all(value is deadline for value in startup.route.deadlines)
    assert startup.route.guest.calls == 1 and startup.route.local.calls == 3
    assert startup.workflow is not None and startup.workflow.activation_observation is not None
    assert startup.workflow.activation_observation.outcome is expected
    assert startup.workflow.activation_observation.warning_count == (2 if expected is TaskOutcome.WARNINGS else None)
    assert startup.workflow.activation_observation.request_settled
    assert database.operations.inspect(_scope()) is None
    with pytest.raises(StateError):
        views.execution.run(Command(["/usr/bin/true"]), profile=Protection.DIRECT)


@pytest.mark.parametrize(
    "receipt,response",
    [
        ("bad receipt", task()),
        (UPID.replace(":101:", ":102:"), task()),
        (UPID, task(outcome="untrusted provider text")),
        (UPID, task(outcome=None)),
        (UPID, {"status": "stopped"}),
        (UPID, task(upid=UPID + "foreign")),
        (UPID.replace("qmstart", "hastart"), task(upid=UPID.replace("qmstart", "hastart"))),
        (OSError("untrusted provider text"), task()),
        (UPID, OSError("missing task log")),
    ],
)
def test_uncertain_activation_never_prepares_or_replays(database, tmp_path, monkeypatch, receipt, response):
    startup = Startup(database, monkeypatch)
    startup.receipt, startup.tasks = receipt, [response]
    with pytest.raises(StateError) as caught, startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("uncertain activation admitted body")
    assert "untrusted provider text" not in str(caught.value)
    fact = caught.value.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    assert fact._workflow.activation is not None
    assert database.operations.inspect(_scope()) is not None
    assert startup.activation_row().state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert startup.events.count("start") == 1
    assert not startup.route.guest.calls and not startup.route.accounts.calls and not startup.route.local.calls
    assert "power" not in startup.events and "info" not in startup.events


@pytest.mark.parametrize("phase", ["task", "info"])
@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_startup_control_and_failed_cleanup_preserve_original(database, tmp_path, monkeypatch, phase, control_type):
    startup = Startup(database, monkeypatch)
    primary = control_type()
    startup.tasks = [primary] if phase == "task" else [task()]
    startup.infos = [primary]
    if phase == "info":
        original = OperationRepository.release_resolved
        monkeypatch.setattr(
            OperationRepository, "release_resolved", lambda *args: (_ for _ in ()).throw(OSError("cleanup"))
        )
    with pytest.raises(control_type) as caught, startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("interrupted startup admitted body")
    assert caught.value is primary and isinstance(primary.__cause__, NativeVMOperationControlFact)
    assert database.operations.inspect(_scope()) is not None
    startup.tasks = [task()]
    if phase == "info":
        monkeypatch.setattr(OperationRepository, "release_resolved", original)
    primary.__cause__.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None
    assert startup.events.count("start") == 1 and not startup.route.guest.calls


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("stage", ["register", "mark", "publish", "resolve"])
def test_activation_lost_commit_reply_through_workflow(database, tmp_path, monkeypatch, stage, committed):
    startup = Startup(database, monkeypatch)
    startup.tasks = [task()]
    primary = KeyboardInterrupt()
    names = {
        "register": "register_lifecycle_obligation",
        "mark": "mark_lifecycle_obligation_possible_effect",
        "publish": "publish_lifecycle_obligation_payload",
        "resolve": "resolve_lifecycle_obligation",
    }
    original = getattr(OperationRepository, names[stage])

    def interrupted(repository, *args, **kwargs):
        if committed:
            original(repository, *args, **kwargs)
        raise primary

    monkeypatch.setattr(OperationRepository, names[stage], interrupted)
    with pytest.raises(KeyboardInterrupt) as caught, startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("uncertain bookkeeping admitted body")
    assert caught.value is primary and not startup.route.guest.calls
    monkeypatch.setattr(OperationRepository, names[stage], original)
    claim = database.operations.inspect(_scope())
    if stage == "register" or (committed and stage in {"publish", "resolve"}):
        # Before commit, exact absence proves no POST; after commit, the unused row resolves.
        assert claim is None
        assert startup.events.count("start") == int(stage != "register")
    else:
        assert claim is not None and isinstance(primary.__cause__, NativeVMOperationControlFact)
        fact = primary.__cause__
        if stage == "mark":
            with pytest.raises(StateError):
                fact.retry_cleanup(Deadline.after(10))
        else:
            events = list(startup.events)
            fact.retry_cleanup(Deadline.after(10))
            assert database.operations.inspect(_scope()) is None
            assert startup.events.count("start") == 1
            assert startup.events[len(events) :] in ([], ["task"])
        assert not startup.route.guest.calls and not startup.route.accounts.calls


@pytest.mark.parametrize("after_guest", [False, True])
def test_changed_locator_after_start_suppresses_body(database, tmp_path, monkeypatch, after_guest):
    startup = Startup(database, monkeypatch)
    startup.tasks, startup.infos = [task()], [INFO]
    from agentworks.capabilities.vm_platform.base import ProviderLocator

    startup.route.locators = [ProviderLocator("initial")]
    if after_guest:
        startup.route.locators.append(ProviderLocator("initial"))
    startup.route.locators.append(ProviderLocator("changed"))
    with pytest.raises(StateError), startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("changed locator admitted body")
    assert startup.route.guest.calls == int(after_guest)
    assert not startup.route.accounts.calls and not startup.route.local.calls
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("phase", ["start", "task", "power", "info"])
def test_late_startup_reply_refuses_and_cleanup_never_resumes_preparation(database, tmp_path, monkeypatch, phase):
    startup = Startup(database, monkeypatch)
    startup.tasks, startup.infos = [task()], [INFO]
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    deadline = Deadline.after(10)
    name = {
        "start": "request_vm_start",
        "task": "request_task_status",
        "power": "request_power",
        "info": "request_guest_info",
    }[phase]
    original = getattr(_ProxmoxWire, name)

    def late(*args, **kwargs):
        response = original(*args, **kwargs)
        clock[0] += 20
        return response

    monkeypatch.setattr(_ProxmoxWire, name, late)
    with pytest.raises(StateError) as caught, startup.operation(tmp_path, deadline):
        pytest.fail("late startup admitted body")
    monkeypatch.setattr(_ProxmoxWire, name, original)
    if phase in {"start", "task"}:
        assert isinstance(caught.value.__cause__, NativeVMOperationControlFact)
        caught.value.__cause__.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None
    assert startup.events.count("start") == 1
    assert not startup.route.guest.calls and not startup.route.accounts.calls and not startup.route.local.calls


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"result": None},
        {"result": {}},
        {"result": {"version": 1, "supported_commands": []}},
        {"result": {"version": "9", "supported_commands": None}},
        {"result": {"version": "9", "supported_commands": [True]}},
    ],
)
def test_bad_guest_info_uses_only_passive_wait_until_deadline(database, tmp_path, monkeypatch, info):
    startup = Startup(database, monkeypatch)
    startup.tasks, startup.infos = [task()], [info]

    def expired(deadline):
        raise StateError("deadline expired")

    monkeypatch.setattr(native, "_pause", expired)
    with pytest.raises(StateError), startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("malformed info admitted helper")
    assert startup.events == ["start", "task", "power", "info"]
    assert database.operations.inspect(_scope()) is None
    assert not startup.route.guest.calls and not startup.route.accounts.calls


def test_fresh_power_must_be_running_before_guest_info(database, tmp_path, monkeypatch):
    startup = Startup(database, monkeypatch)
    startup.tasks, startup.power = [task()], {"status": "stopped"}
    with pytest.raises(StateError), startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("stopped power admitted helper")
    assert startup.events == ["start", "task", "power"]
    assert database.operations.inspect(_scope()) is None and not startup.route.guest.calls


def test_fresh_cleanup_observes_only_known_task_after_failed_read(database, tmp_path, monkeypatch):
    startup = Startup(database, monkeypatch)
    startup.tasks = [OSError("missing task log")]
    with pytest.raises(StateError) as caught, startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("failed read admitted helper")
    fact = caught.value.__cause__
    assert isinstance(fact, NativeVMOperationControlFact)
    events = list(startup.events)
    startup.tasks = [task(phase="running"), task()]
    fact.retry_cleanup(Deadline.after(10))
    assert startup.events[len(events) :] == ["task", "task"]
    assert startup.events.count("start") == 1
    assert startup.route.resolutions == 1 and not startup.route.guest.calls
    assert database.operations.inspect(_scope()) is None


def test_takeover_during_info_cannot_admit_guest_or_release_successor(database, tmp_path, monkeypatch):
    startup = Startup(database, monkeypatch)
    startup.tasks, startup.infos = [task()], [INFO]

    def takeover(*args, **kwargs):
        assert startup.owner is not None
        OperationOwner.recover(database.operations, startup.owner, "b" * 32)
        return INFO

    monkeypatch.setattr(_ProxmoxWire, "request_guest_info", takeover)
    with pytest.raises(StateError) as caught, startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("stale owner admitted guest")
    assert isinstance(caught.value.__cause__, NativeVMOperationControlFact)
    claim = database.operations.inspect(_scope())
    assert claim is not None and claim.ownership.generation_id == "b" * 32
    assert not startup.route.guest.calls and not startup.route.accounts.calls


def test_uncertain_fence_cannot_clear_absent_registration(database, tmp_path, monkeypatch):
    startup = Startup(database, monkeypatch)
    primary = KeyboardInterrupt()
    original = OperationRepository.list_pending_lifecycle_obligations

    def interrupted(*args, **kwargs):
        raise primary

    def unavailable(*args, **kwargs):
        raise OSError("ledger read unavailable")

    monkeypatch.setattr(OperationRepository, "register_lifecycle_obligation", interrupted)
    monkeypatch.setattr(OperationRepository, "list_pending_lifecycle_obligations", unavailable)
    with pytest.raises(KeyboardInterrupt) as caught, startup.operation(tmp_path, Deadline.after(10)):
        pytest.fail("lost registration admitted helper")
    assert caught.value is primary and isinstance(primary.__cause__, NativeVMOperationControlFact)
    assert database.operations.inspect(_scope()) is not None
    monkeypatch.setattr(OperationRepository, "list_pending_lifecycle_obligations", original)
    primary.__cause__.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None and not startup.events
