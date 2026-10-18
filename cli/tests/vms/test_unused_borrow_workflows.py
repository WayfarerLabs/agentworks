"""Native shutdown retires borrow replies lost before helper-owner publication."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database
from agentworks.errors import StateError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._wsl2_binding import _build_wsl2_native_binding
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from agentworks.execution.files import Create, JsonStrategy, NewMetadata
from agentworks.execution.models import Command, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.operations import OperationBorrow, OperationOwner
from agentworks.vms._native_operation import VMOperationControlFact, native_vm_operation
from tests.execution.test_managed_execution_access import Delivery
from tests.execution.test_wsl2_platform_hold import FakeObserver
from tests.vms.test_native_operation import _install_route, _root, _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="synthetic Linux native composition")


def interrupt_borrow(
    owner: OperationOwner,
    monkeypatch: pytest.MonkeyPatch,
    control: BaseException,
    orphans: list[OperationBorrow],
    *,
    before_raise: Callable[[], None] | None = None,
    selected_call: int = 1,
) -> None:
    original = owner.borrow
    calls = 0

    def lost_reply():
        nonlocal calls
        borrowed = original()
        calls += 1
        if calls != selected_call:
            return borrowed
        orphans.append(borrowed)
        assert owner._active_borrow is borrowed and not borrowed.closed
        assert owner._outstanding_attempt is None and not borrowed._attempt_started
        assert borrowed._dispatch_obligation_id is None and borrowed._supplied_dispatch is None
        if before_raise is not None:
            before_raise()
        raise control

    monkeypatch.setattr(owner, "borrow", lost_reply)


def finish_retry(database: Database, observer: FakeObserver, control: BaseException, retry: bool) -> None:
    if retry:
        fact = control.__cause__
        assert isinstance(fact, VMOperationControlFact)
        assert database.operations.inspect(_scope()) is not None
        with pytest.raises(StateError):
            fact._workflow.owner.borrow()
        observer.presence = GuestAnchorPresence.ABSENT_CONFIRMED
        fact.retry_cleanup(Deadline.after(10))
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("facade", ["direct", "stat", "read", "write", "upload-file", "download", "json", "directory"])
def test_prepared_facades_retire_only_the_unpublished_borrow(database, tmp_path, monkeypatch, facade, retry):
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    root = Path(_root(tmp_path))
    root.mkdir()
    source = root / "source"
    source.write_bytes(b"original")
    local_source = tmp_path / "upload-source"
    local_source.write_bytes(b"local-original")
    destination = tmp_path / "download-destination"
    marker = tmp_path / "command-marker"
    orphans: list[OperationBorrow] = []
    control = KeyboardInterrupt("borrow reply")
    metadata = NewMetadata("admin", "admin", 0o600)
    if retry:
        observer.presence = GuestAnchorPresence.UNKNOWN
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=PurePosixPath(root)
        ) as views,
    ):
        before = len(route.routes), route.local.calls
        pending = views.owner.list_pending_lifecycle_obligations()
        assert pending and "dispatch" in native.events
        interrupt_borrow(views.owner, monkeypatch, control, orphans)
        try:
            if facade == "direct":
                views.execution.run(Command(["/usr/bin/touch", str(marker)]), profile=Protection.DIRECT)
            elif facade == "stat":
                views.files.stat(PurePosixPath(source))
            elif facade == "read":
                views.files.read_file(PurePosixPath(source), max_bytes=64)
            elif facade == "write":
                views.files.write_file(
                    PurePosixPath(root / "new"), b"new", condition=Create(), create_metadata=metadata
                )
            elif facade == "upload-file":
                views.files.upload_file(
                    PurePosixPath(root / "new"), local_source, condition=Create(), create_metadata=metadata
                )
            elif facade == "download":
                views.files.download(PurePosixPath(source), destination, max_bytes=64)
            elif facade == "json":
                views.files.update_json(
                    PurePosixPath(root / "new"),
                    {"value": 1},
                    strategy=JsonStrategy.REPLACE,
                    create=True,
                    create_metadata=metadata,
                )
            else:
                views.files.create_directory(PurePosixPath(root / "new"), metadata=NewMetadata("admin", "admin", 0o700))
        finally:
            assert (len(route.routes), route.local.calls) == before
            assert views.owner.list_pending_lifecycle_obligations() == pending
    assert caught.value is control and len(orphans) == 1 and orphans[0].closed
    assert views.owner._active_borrow is None and views.owner._outstanding_attempt is None
    assert not marker.exists() and not destination.exists() and not (root / "new").exists()
    assert source.read_bytes() == b"original" and local_source.read_bytes() == b"local-original"
    assert not views.file_operation.has_unfinished_local_file_call
    assert not views.file_operation.has_unfinished_local_download_call
    finish_retry(database, observer, control, retry)
    assert views.owner._released and native.events.count("dispatch") == 1 and "eof" in native.events
    assert observer.events == (["observe", "observe"] if retry else ["observe"])


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("selected_call", [1, 2, 3])
def test_early_guest_and_account_preparation_retire_orphan_and_prior_hold(
    database, tmp_path, monkeypatch, selected_call, retry
):
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    database.insert_agent("worker", "box", "recorded-worker")
    route.accounts.identities["recorded-worker"] = IdentityExpectation(2401, 2402, (2402,))
    original = OperationOwner.borrow
    calls, orphans, at_interruption = [], [], []
    control = KeyboardInterrupt("preparation borrow reply")

    def borrow(owner):
        borrowed = original(owner)
        calls.append(borrowed)
        if len(calls) == selected_call:
            orphans.append(borrowed)
            at_interruption.append((len(route.routes), route.guest.calls, len(route.accounts.calls)))
            raise control
        return borrowed

    monkeypatch.setattr(OperationOwner, "borrow", borrow)
    if retry:
        observer.presence = GuestAnchorPresence.UNKNOWN
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database,
            "box",
            platform,
            RunContext(),
            deadline=Deadline.after(30),
            trusted_root=_root(tmp_path),
            agent_name="worker",
        ),
    ):
        pytest.fail("interrupted preparation yielded a native view")
    assert caught.value is control and len(orphans) == 1 and orphans[0].closed
    assert (len(route.routes), route.guest.calls, len(route.accounts.calls)) == at_interruption[0]
    assert route.local.calls == 0
    finish_retry(database, observer, control, retry)
    assert native.events.count("dispatch") == 1 and "eof" in native.events


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("phase", ["start", "shell", "observe", "dispose"])
def test_managed_facades_borrow_reply_loss_cleans_exact_native_workflow(database, tmp_path, monkeypatch, phase, retry):
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    binding = _build_wsl2_native_binding(
        WSL2Connection("Ubuntu", "admin", "wsl.exe"), RuntimeSelection(RuntimeTargetOS.LINUX)
    )
    monkeypatch.setattr(platform, "resolve_native_execution_binding", lambda *args, **kwargs: binding)
    managed = Delivery()

    def execute(selected, invocation, *, io, deadline, custody):
        try:
            managed.selected(io)
        except (AssertionError, AttributeError):
            return route.execute(selected, invocation, io=io, deadline=deadline, custody=custody)
        return managed.execute(invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(WSL2Carrier, "execute", execute)
    orphans: list[OperationBorrow] = []
    before = []
    control = KeyboardInterrupt("managed borrow reply")
    if retry:
        observer.presence = GuestAnchorPresence.UNKNOWN
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        native_vm_operation(
            database, "box", platform, RunContext(), deadline=Deadline.after(30), trusted_root=_root(tmp_path)
        ) as views,
    ):
        if phase in {"observe", "dispose"}:
            reference = views.execution.start(
                Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
            )
        interrupt_borrow(
            views.owner,
            monkeypatch,
            control,
            orphans,
            before_raise=lambda: before.append((len(route.routes), managed.clock.calls, managed.start.calls)),
            selected_call=2 if phase == "dispose" else 1,
        )
        if phase == "observe":
            views.execution.observe(reference)
        elif phase == "dispose":
            views.execution.dispose(reference)
        else:
            invocation = Script("true", Shell.USER_DEFAULT) if phase == "shell" else Command(["/bin/true"])
            views.execution.start(
                invocation, profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
            )
    assert caught.value is control and len(orphans) == 1 and orphans[0].closed
    assert (len(route.routes), managed.clock.calls, managed.start.calls) == before[0]
    assert managed.start.calls == int(phase in {"observe", "dispose"})
    assert managed.clock.calls == (0 if phase == "shell" else 1) and managed.dispose.calls == 0
    if phase == "dispose":
        (run,) = views.execution_operation.managed_runs
        assert run.disposal_attempted and run.cleanup_complete
    finish_retry(database, observer, control, retry)
    assert views.owner._released and native.events.count("dispatch") == 1 and "eof" in native.events
