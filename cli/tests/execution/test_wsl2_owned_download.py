"""Portable SQLite proof of one held WSL2 DOWNLOAD normal path."""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, encode_vm_guest_identity_success
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution._wsl2_owned_download import WSL2DownloadStatus, WSL2OwnedDownload
from agentworks.execution.carrier import (
    CapturedOutput,
    Carrier,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.wsl2 import WSL2Connection
from tests.execution.files._file_download_support import BytesSink, LostCallStdoutCarrier
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle
from tests.execution.test_wsl2_platform_hold import BOOT, FakeNative, FakeObserver
from tests.vms.test_target_preparation import _MARKER, _vm

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private file fixture requires Linux")


class GuestThenFileCarrier:
    def __init__(self, database: Database, *, init_ticks: int = 4096, file_carrier: Carrier | None = None) -> None:
        self.database = database
        self.init_ticks = init_ticks
        self.file_carrier = file_carrier or LocalCarrier()
        self.owner_id: str | None = None
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.file_carrier.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        claim = self.database.operations.inspect(OperationScope(OperationResourceKind.VM, "box"))
        assert claim is not None
        current = claim.ownership.operation_id
        if self.owner_id is None:
            self.owner_id = current
        assert current == self.owner_id
        if self.calls != 1:
            return self.file_carrier.execute(invocation, io=io, deadline=deadline)
        assert isinstance(io.output, SinkOutput)
        assert "agentworks-runtime-prerequisite" in invocation.argv
        nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
        guest = VMGuestIdentity(_MARKER, BOOT, self.init_ticks)
        response = encode_vm_guest_identity_success(nonce, guest)
        io.output.stdout.try_write(memoryview(f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode() + response))
        return CarrierReport(
            Dispatch.SENT,
            ExitStatus(code=0),
            stdout=CapturedOutput(complete=True, retention=Retention.DELIVERED),
            stderr=CapturedOutput(complete=True, retention=Retention.DELIVERED),
        )


def _plan() -> IdentityPlan:
    gid = os.getegid()
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), gid, tuple(sorted(set(os.getgroups()) | {gid}))), IdentityMode.DIRECT
    )


def _subject(database: Database, carrier: GuestThenFileCarrier, observer: FakeObserver) -> WSL2OwnedDownload:
    return WSL2OwnedDownload(
        database.operations,
        _vm(),
        ProviderLocator("wsl2:opaque-test-registration"),
        WSL2Connection("Ubuntu", "admin", "wsl.exe"),
        native=FakeNative([]),
        observer=observer,
        carrier=carrier,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
    )


def _download(subject: WSL2OwnedDownload, root: Path, sink: BytesSink) -> WSL2DownloadStatus:
    return subject.download(
        trusted_root_path=str(root),
        relative_path="source",
        sink=sink,
        max_bytes=64,
        plan=_plan(),
        deadline=Deadline.after(30),
    )


def test_complete_download_uses_one_owner_and_releases_after_exact_guest_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"held-wsl-download")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        observer = FakeObserver([])
        subject = _subject(database, carrier, observer)
        sink = BytesSink()
        assert _download(subject, root, sink) is WSL2DownloadStatus.COMPLETE
        assert bytes(sink.data) == b"held-wsl-download"
        assert carrier.calls > 1 and carrier.owner_id == subject.owner.ownership.operation_id
        assert observer.events == ["observe"]
        assert database.operations.inspect(subject.owner.ownership.scope) is None
        assert subject.file_operation is not None and not subject.file_operation.unfinished_downloads


def test_missing_source_resolves_file_and_hold_obligations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        observer = FakeObserver([])
        subject = _subject(database, carrier, observer)
        sink = BytesSink()
        assert _download(subject, root, sink) is WSL2DownloadStatus.REFUSED
        assert sink.data == b"" and carrier.calls > 1
        assert observer.events == ["observe"]
        assert subject.file_operation is not None and not subject.file_operation.unfinished_downloads
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_default_observer_rejection_does_not_acquire_claim(tmp_path: Path) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        scope = OperationScope(OperationResourceKind.VM, "box")
        with pytest.raises(ValidationError):
            WSL2OwnedDownload(
                database.operations,
                _vm(),
                ProviderLocator("wsl2:opaque-test-registration"),
                WSL2Connection("Ubuntu", "admin", "C:/Windows/System32/wsl.exe"),
                native=FakeNative([]),
            )
        assert database.operations.inspect(scope) is None


def test_failed_inert_hold_construction_abandons_reserved_claim(tmp_path: Path) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        scope = OperationScope(OperationResourceKind.VM, "box")
        native = FakeNative([], snapshot_failure_at=1, snapshot_error=RuntimeError("snapshot failed"))
        with pytest.raises(RuntimeError, match="snapshot failed"):
            WSL2OwnedDownload(
                database.operations,
                _vm(),
                ProviderLocator("wsl2:opaque-test-registration"),
                WSL2Connection("Ubuntu", "admin", "wsl.exe"),
                native=native,
                observer=FakeObserver([]),
            )
        assert database.operations.inspect(scope) is None


def test_ready_epoch_mismatch_refuses_before_file_dispatch(tmp_path: Path) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database, init_ticks=4097)
        subject = _subject(database, carrier, FakeObserver([]))
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.REFUSED
        assert carrier.calls == 1
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_uncertain_guest_absence_retains_claim(tmp_path: Path) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database, init_ticks=4097)
        subject = _subject(database, carrier, FakeObserver([], GuestAnchorPresence.UNKNOWN))
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.RETAINED
        assert carrier.calls == 1
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        rows = database.operations.list_lifecycle_obligations(subject.owner.ownership)
        assert any(row.state is LifecycleObligationState.POSSIBLE_EFFECT for row in rows)


def test_unresolved_file_exchange_retains_claim_and_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"held-wsl-download")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch)
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        carrier.file_carrier = LostCallStdoutCarrier(3)
        observer = FakeObserver([])
        subject = _subject(database, carrier, observer)
        assert _download(subject, root, BytesSink()) is WSL2DownloadStatus.RETAINED
        assert carrier.calls > 1 and observer.events == []
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        rows = database.operations.list_lifecycle_obligations(subject.owner.ownership)
        assert any(row.state is LifecycleObligationState.POSSIBLE_EFFECT for row in rows)
