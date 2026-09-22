"""Core-owned lifetime checks for concrete private file calls."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Event, Thread

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_download
from agentworks.execution._file_download import (
    FileDownloadControlFact,
    FileDownloadFailure,
    FileDownloadStatus,
    _WorkingState,
)
from agentworks.execution._file_operation import FileOperation, UnfinishedFileDownload
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import CarrierIO, CarrierReport, ChannelFeatures, Deadline, PreparedInvocation
from agentworks.operations import OperationBorrow, OperationOwner
from tests.execution.files._file_download_support import BytesSink, LostCallStdoutCarrier
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private snapshot helper requires Linux")


class CaptureStop(Exception):
    pass


class InterruptingCarrier:
    def __init__(self, control: BaseException) -> None:
        self.control = control
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, io, deadline
        self.calls += 1
        raise self.control


class ReentrantCarrier:
    def __init__(
        self,
        operation: FileOperation,
        root: Path,
        plan: IdentityPlan,
    ) -> None:
        self.operation = operation
        self.root = root
        self.plan = plan
        self.inner = LocalCarrier()
        self.calls = 0
        self.rejected = False

    @property
    def features(self) -> ChannelFeatures:
        return self.inner.features

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        if self.calls == 0:
            with pytest.raises(StateError):
                self.operation.download(
                    LocalCarrier(),
                    trusted_root_path=str(self.root),
                    relative_path="source",
                    sink=BytesSink(),
                    max_bytes=64,
                    plan=self.plan,
                    deadline=deadline,
                    runtime_selection=runtime_selection(sys.executable),
                )
            self.rejected = True
        self.calls += 1
        return self.inner.execute(invocation, io=io, deadline=deadline)


class BlockingCarrier:
    def __init__(self, entered: Event, release: Event) -> None:
        self.entered = entered
        self.release = release
        self.inner = LocalCarrier()
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return self.inner.features

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        if self.calls == 0:
            self.entered.set()
            if not self.release.wait(timeout=10):
                raise RuntimeError("timed out waiting to release the attached call")
        self.calls += 1
        return self.inner.execute(invocation, io=io, deadline=deadline)


@pytest.fixture
def plan() -> IdentityPlan:
    gid = os.getegid()
    groups = tuple(sorted(set(os.getgroups()) | {gid}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), gid, groups), IdentityMode.DIRECT)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    source = tmp_path / "source-root"
    source.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    return source, scratch


def _owner(database: Database) -> OperationOwner:
    return OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "core-file-vm"),
        "core-file",
    )


def test_local_refusal_closes_predispatch_borrow_without_retained_state(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    carrier = LocalCarrier()
    try:
        with pytest.raises(ValidationError):
            operation.download(
                carrier,
                trusted_root_path="/approved",
                relative_path="source",
                sink=BytesSink(),
                max_bytes=0,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert carrier.calls == 0
        assert not operation.active_downloads
        assert operation.unfinished_downloads == ()
        owner.close()
        assert database.operations.inspect(owner.ownership.scope) is None
    finally:
        database.close()


def test_shared_operation_rejects_overlapping_view_call(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    carrier = ReentrantCarrier(operation, source, plan)
    sink = BytesSink()
    try:
        outcome = operation.download(
            carrier,
            trusted_root_path=str(source),
            relative_path="source",
            sink=sink,
            max_bytes=64,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )

        assert outcome.status is FileDownloadStatus.COMPLETE
        assert bytes(sink.data) == b"payload" and carrier.rejected
        assert operation.active_downloads == ()
        assert operation.unfinished_downloads == ()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_completed_call_forgets_only_its_record_when_next_call_attaches(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    entered = Event()
    release = Event()
    second_carrier = BlockingCarrier(entered, release)
    second_outcomes = []
    second_failures: list[BaseException] = []
    second_thread: Thread | None = None
    started_second = False
    close = OperationBorrow.close

    def run_second() -> None:
        try:
            second_outcomes.append(
                operation.download(
                    second_carrier,
                    trusted_root_path=str(source),
                    relative_path="source",
                    sink=BytesSink(),
                    max_bytes=64,
                    plan=plan,
                    deadline=Deadline.after(30),
                    runtime_selection=runtime_selection(sys.executable),
                )
            )
        except BaseException as failure:
            second_failures.append(failure)

    def interleave_after_close(borrow: OperationBorrow) -> None:
        nonlocal second_thread, started_second
        close(borrow)
        if started_second:
            return
        started_second = True
        second_thread = Thread(target=run_second)
        second_thread.start()
        assert entered.wait(timeout=10)

    monkeypatch.setattr(OperationBorrow, "close", interleave_after_close)
    try:
        first = operation.download(
            LocalCarrier(),
            trusted_root_path=str(source),
            relative_path="source",
            sink=BytesSink(),
            max_bytes=64,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )

        assert first.status is FileDownloadStatus.COMPLETE
        assert len(operation.active_downloads) == 1
        assert operation.active_downloads[0].carrier is second_carrier
        release.set()
        assert second_thread is not None
        second_thread.join(timeout=10)
        assert not second_thread.is_alive()
        assert second_failures == []
        assert len(second_outcomes) == 1
        assert second_outcomes[0].status is FileDownloadStatus.COMPLETE
        assert not operation.active_downloads
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        release.set()
        if second_thread is not None:
            second_thread.join(timeout=10)
        database.close()


def test_successive_inert_cleanup_debts_remain_distinct(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    carriers = (LostCallStdoutCarrier(3), LostCallStdoutCarrier(3))
    try:
        outcomes = tuple(
            operation.download(
                carrier,
                trusted_root_path=str(source),
                relative_path="source",
                sink=BytesSink(),
                max_bytes=64,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )
            for carrier in carriers
        )

        assert all(outcome.failure is FileDownloadFailure.CLEANUP for outcome in outcomes)
        assert all(outcome.cleanup_debt is not None for outcome in outcomes)
        assert all(not outcome.pending_remote_effects for outcome in outcomes)
        retained = operation.unfinished_downloads
        assert len(retained) == 2
        assert tuple(item.carrier for item in retained) == carriers
        assert all(item.binding is outcome.binding for item, outcome in zip(retained, outcomes, strict=True))
        assert all(item.outcome is outcome for item, outcome in zip(retained, outcomes, strict=True))
        assert retained[0] is not retained[1]
        assert operation.active_downloads == ()
    finally:
        database.close()


def test_exceptional_outcome_is_retained_before_borrow_handoff(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    control = KeyboardInterrupt("control-canary")
    carrier = InterruptingCarrier(control)
    handoff_observations: list[UnfinishedFileDownload] = []
    handoff = OperationBorrow.handoff_unresolved

    def observe_handoff(borrow: OperationBorrow) -> None:
        retained = operation.unfinished_downloads
        assert operation.active_downloads
        assert len(retained) == 1
        assert retained[0].outcome is operation.active_downloads[0].outcome
        handoff_observations.append(retained[0])
        handoff(borrow)

    monkeypatch.setattr(OperationBorrow, "handoff_unresolved", observe_handoff)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            operation.download(
                carrier,
                trusted_root_path="/approved",
                relative_path="source",
                sink=BytesSink(),
                max_bytes=64,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert raised.value is control
        fact = raised.value.__cause__
        assert isinstance(fact, FileDownloadControlFact)
        assert handoff_observations[0].outcome is fact.outcome
        assert handoff_observations[0].carrier is carrier
        assert operation.active_downloads == ()
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()


def test_failed_unfinished_capture_keeps_attached_working_state_and_borrow(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    captured: list[UnfinishedFileDownload] = []

    def fail_capture(self: FileOperation, download: UnfinishedFileDownload) -> None:
        del self
        captured.append(download)
        raise CaptureStop("capture-canary")

    monkeypatch.setattr(FileOperation, "_retain_unfinished", fail_capture)
    try:
        with pytest.raises(CaptureStop):
            operation.download(
                LostCallStdoutCarrier(3),
                trusted_root_path=str(source),
                relative_path="source",
                sink=BytesSink(),
                max_bytes=64,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        active = operation.active_downloads[0] if operation.active_downloads else None
        assert active is not None and active.outcome is captured[0].outcome
        assert active.prepared.state is not None
        assert active.prepared.workflow._sink is None  # noqa: SLF001
        assert operation.unfinished_downloads == ()
        with pytest.raises(StateError):
            owner.borrow()
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()


def test_failed_exceptional_capture_preserves_control_identity_and_fact(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    control = KeyboardInterrupt("control-canary")
    carrier = InterruptingCarrier(control)

    def fail_capture(self: FileOperation, download: UnfinishedFileDownload) -> None:
        del self, download
        raise CaptureStop("capture-canary")

    monkeypatch.setattr(FileOperation, "_retain_unfinished", fail_capture)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            operation.download(
                carrier,
                trusted_root_path="/approved",
                relative_path="source",
                sink=BytesSink(),
                max_bytes=64,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert raised.value is control
        fact = raised.value.__cause__
        assert isinstance(fact, FileDownloadControlFact)
        active = operation.active_downloads[0] if operation.active_downloads else None
        assert active is not None and active.outcome is fact.outcome
        assert operation.unfinished_downloads == ()
        with pytest.raises(StateError):
            owner.borrow()
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()


def test_outcome_allocation_failure_keeps_previously_attached_state_and_borrow(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    control = CaptureStop("outcome-allocation-canary")

    def fail_finish(self: _WorkingState) -> None:
        del self
        raise control

    monkeypatch.setattr(_WorkingState, "finish", fail_finish)
    try:
        with pytest.raises(CaptureStop) as raised:
            operation.download(
                LocalCarrier(),
                trusted_root_path=str(source),
                relative_path="source",
                sink=BytesSink(),
                max_bytes=64,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert raised.value is control
        active = operation.active_downloads[0] if operation.active_downloads else None
        assert active is not None and active.outcome is None
        assert active.prepared.state.binding is active.binding
        assert operation.unfinished_downloads == ()
        with pytest.raises(StateError):
            owner.borrow()
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("failure_point", ["outcome", "fact"])
def test_exceptional_fact_failure_preserves_control_without_reusing_prior_cause(
    tmp_path: Path,
    roots: tuple[Path, Path],
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    source, _ = roots
    source.joinpath("source").write_bytes(b"payload")
    database = Database(tmp_path / "state.db")
    owner = _owner(database)
    operation = FileOperation(owner)
    try:
        previous = operation.download(
            LocalCarrier(),
            trusted_root_path=str(source),
            relative_path="source",
            sink=BytesSink(),
            max_bytes=64,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
        )
        control = KeyboardInterrupt("control-canary")
        prior_fact = FileDownloadControlFact(previous)
        control.__cause__ = prior_fact
        carrier = LocalCarrier()

        class InterruptingSink:
            def try_write(self, data: memoryview) -> int:
                raise control

        allocation_causes: list[BaseException | None] = []

        def fail_allocation(*args: object) -> None:
            allocation_causes.append(control.__cause__)
            raise MemoryError("allocation-canary")

        if failure_point == "outcome":
            monkeypatch.setattr(_WorkingState, "finish", fail_allocation)
        else:
            monkeypatch.setattr(_file_download, "FileDownloadControlFact", fail_allocation)

        with pytest.raises(KeyboardInterrupt) as raised:
            operation.download(
                carrier,
                trusted_root_path=str(source),
                relative_path="source",
                sink=InterruptingSink(),
                max_bytes=64,
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
            )

        assert allocation_causes == [prior_fact]
        assert raised.value is control and raised.value.__cause__ is None
        assert operation.unfinished_downloads == ()
        assert len(operation.active_downloads) == 1
        active = operation.active_downloads[0]
        assert active.carrier is carrier and active.outcome is None
        assert active.prepared.state.token != previous.token
        with pytest.raises(StateError):
            owner.borrow()
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()
