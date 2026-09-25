"""Portable SQLite proof of one held WSL2 DOWNLOAD normal path."""

from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, encode_vm_guest_identity_success
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution._wsl2_owned_download import WSL2DownloadStatus, WSL2OwnedDownload
from agentworks.execution.binding import NativeExecutionBinding
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
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
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


def _subject(
    database: Database, carrier: GuestThenFileCarrier, observer: FakeObserver, monkeypatch: pytest.MonkeyPatch
) -> WSL2OwnedDownload:
    subject, _ = _platform_subject(database, carrier, observer, monkeypatch)
    assert subject is not None
    return subject


def _platform_subject(
    database: Database,
    carrier: GuestThenFileCarrier,
    observer: FakeObserver | None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    locators: list[ProviderLocator | ProviderLocatorUnavailable] | None = None,
    connections: list[WSL2Connection] | None = None,
    runtimes: list[RuntimeSelection] | None = None,
    native: FakeNative | None = None,
    observed_routes: list[WSL2Connection] | None = None,
) -> tuple[WSL2OwnedDownload | None, Mock]:
    def execute(
        selected: WSL2Carrier, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline
    ) -> CarrierReport:
        if observed_routes is not None:
            observed_routes.append(selected.connection)
        return carrier.execute(invocation, io=io, deadline=deadline)

    monkeypatch.setattr(WSL2Carrier, "execute", execute)
    platform = Mock(spec=WSL2Platform)
    platform.site_name = "local"
    platform.observe_provider_locator.side_effect = locators or [ProviderLocator("wsl2:registration")] * 3
    routes = connections or [WSL2Connection("Ubuntu", "admin", "wsl.exe")] * 2
    selections = runtimes or [RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)] * len(routes)
    bindings = [
        NativeExecutionBinding(
            WSL2Carrier(route),
            route.user,
            runtime,
        )
        for route, runtime in zip(routes, selections, strict=True)
    ]
    platform.resolve_native_execution_binding.side_effect = bindings
    platform.test_bindings = bindings
    subject = WSL2OwnedDownload.from_platform(
        database.operations,
        _vm(),
        platform,
        cast(RunContext, object()),
        deadline=Deadline.after(30),
        native=FakeNative([]) if native is None else native,
        observer=observer,
    )
    return subject, platform


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
        subject = _subject(database, carrier, observer, monkeypatch)
        sink = BytesSink()
        assert _download(subject, root, sink) is WSL2DownloadStatus.COMPLETE
        assert bytes(sink.data) == b"held-wsl-download"
        assert carrier.calls > 1 and carrier.owner_id == subject.owner.ownership.operation_id
        assert observer.events == ["observe"]
        assert database.operations.inspect(subject.owner.ownership.scope) is None
        assert subject.file_operation is not None and not subject.file_operation.unfinished_downloads


def test_selected_platform_download_rechecks_registration_and_route(
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
        subject, platform = _platform_subject(database, carrier, FakeObserver([]), monkeypatch)
        assert subject is not None
        sink = BytesSink()
        assert _download(subject, root, sink) is WSL2DownloadStatus.COMPLETE
        assert bytes(sink.data) == b"held-wsl-download"
        assert platform.observe_provider_locator.call_count == 3
        assert platform.resolve_native_execution_binding.call_count == 2
        assert carrier.calls > 1
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_platform_carrier_mutation_cannot_redirect_file_dispatch(
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
        routes: list[WSL2Connection] = []
        subject, platform = _platform_subject(database, carrier, FakeObserver([]), monkeypatch, observed_routes=routes)
        assert subject is not None
        original = WSL2Connection("Ubuntu", "admin", "wsl.exe")
        initial_carrier = cast(WSL2Carrier, platform.test_bindings[0].carrier)
        initial_carrier._connection = WSL2Connection("other", "admin", "C:/other/wsl.exe")
        assert type(subject._carrier) is WSL2Carrier
        assert subject._carrier is not initial_carrier
        sink = BytesSink()
        assert _download(subject, root, sink) is WSL2DownloadStatus.COMPLETE
        assert bytes(sink.data) == b"held-wsl-download"
        assert len(routes) > 1 and all(route == original for route in routes)
        assert database.operations.inspect(subject.owner.ownership.scope) is None


@pytest.mark.parametrize("later", [ProviderLocator("wsl2:changed"), ProviderLocatorUnavailable()])
def test_selected_platform_changed_or_missing_locator_refuses_before_guest_or_file(
    tmp_path: Path, later: ProviderLocator | ProviderLocatorUnavailable, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        subject, platform = _platform_subject(
            database,
            carrier,
            FakeObserver([]),
            monkeypatch,
            locators=[ProviderLocator("wsl2:registration"), later],
        )
        assert subject is not None
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.REFUSED
        assert carrier.calls == 0
        platform.resolve_native_execution_binding.assert_called_once()
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_selected_platform_changed_route_refuses_before_file_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        subject, _ = _platform_subject(
            database,
            carrier,
            FakeObserver([]),
            monkeypatch,
            connections=[
                WSL2Connection("Ubuntu", "admin", "wsl.exe"),
                WSL2Connection("Ubuntu", "admin", "C:/other/wsl.exe"),
            ],
        )
        assert subject is not None
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.REFUSED
        # Selected preparation has already run its guest probe through the changed route.
        assert carrier.calls == 1
        assert subject.file_operation is None
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_selected_platform_changed_runtime_refuses_before_file_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        subject, _ = _platform_subject(
            database,
            carrier,
            FakeObserver([]),
            monkeypatch,
            runtimes=[
                RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
                RuntimeSelection(RuntimeTargetOS.LINUX, "/other/python"),
            ],
        )
        assert subject is not None
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.REFUSED
        assert carrier.calls == 1
        assert subject.file_operation is None
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_selected_platform_unavailable_locator_does_not_acquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        subject, platform = _platform_subject(
            database, carrier, FakeObserver([]), monkeypatch, locators=[ProviderLocatorUnavailable()]
        )
        assert subject is None
        platform.resolve_native_execution_binding.assert_not_called()
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is None


def test_selected_platform_invalid_binding_does_not_acquire(tmp_path: Path) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        platform.observe_provider_locator.return_value = ProviderLocator("wsl2:registration")
        platform.resolve_native_execution_binding.return_value = NativeExecutionBinding(
            WSL2Carrier(WSL2Connection("Ubuntu", "admin", "wsl.exe")),
            "different-account",
            RuntimeSelection(RuntimeTargetOS.LINUX),
        )
        with pytest.raises(ValidationError):
            WSL2OwnedDownload.from_platform(
                database.operations,
                _vm(),
                platform,
                cast(RunContext, object()),
                deadline=Deadline.after(30),
            )
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is None


def test_selected_platform_subclass_carrier_does_not_acquire(tmp_path: Path) -> None:
    class SubclassCarrier(WSL2Carrier):
        pass

    with closing(Database(tmp_path / "state.db")) as database:
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        platform.observe_provider_locator.return_value = ProviderLocator("wsl2:registration")
        platform.resolve_native_execution_binding.return_value = NativeExecutionBinding(
            SubclassCarrier(WSL2Connection("Ubuntu", "admin", "wsl.exe")),
            "admin",
            RuntimeSelection(RuntimeTargetOS.LINUX),
        )
        with pytest.raises(ValidationError):
            WSL2OwnedDownload.from_platform(
                database.operations,
                _vm(),
                platform,
                cast(RunContext, object()),
                deadline=Deadline.after(30),
            )
        assert database.operations.inspect(OperationScope(OperationResourceKind.VM, "box")) is None


def test_selected_platform_uncertain_guest_retains_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database, init_ticks=4097)
        subject, _ = _platform_subject(database, carrier, FakeObserver([], GuestAnchorPresence.UNKNOWN), monkeypatch)
        assert subject is not None
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.RETAINED
        assert carrier.calls == 1
        assert subject.file_operation is None
        assert database.operations.inspect(subject.owner.ownership.scope) is not None


def test_selected_platform_changed_route_retains_when_hold_absence_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        subject, _ = _platform_subject(
            database,
            carrier,
            FakeObserver([], GuestAnchorPresence.UNKNOWN),
            monkeypatch,
            connections=[
                WSL2Connection("Ubuntu", "admin", "wsl.exe"),
                WSL2Connection("Ubuntu", "admin", "C:/other/wsl.exe"),
            ],
        )
        assert subject is not None
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.RETAINED
        assert carrier.calls == 1
        assert subject.file_operation is None
        assert database.operations.inspect(subject.owner.ownership.scope) is not None


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
        subject = _subject(database, carrier, observer, monkeypatch)
        sink = BytesSink()
        assert _download(subject, root, sink) is WSL2DownloadStatus.REFUSED
        assert sink.data == b"" and carrier.calls > 1
        assert observer.events == ["observe"]
        assert subject.file_operation is not None and not subject.file_operation.unfinished_downloads
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_default_observer_rejection_does_not_acquire_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        scope = OperationScope(OperationResourceKind.VM, "box")
        with pytest.raises(ValidationError):
            _platform_subject(
                database,
                GuestThenFileCarrier(database),
                None,
                monkeypatch,
                connections=[WSL2Connection("Ubuntu", "admin", "C:/Windows/System32/wsl.exe")],
            )
        assert database.operations.inspect(scope) is None


def test_failed_inert_hold_construction_abandons_reserved_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        scope = OperationScope(OperationResourceKind.VM, "box")
        native = FakeNative([], snapshot_failure_at=1, snapshot_error=RuntimeError("snapshot failed"))
        with pytest.raises(RuntimeError, match="snapshot failed"):
            _platform_subject(
                database,
                GuestThenFileCarrier(database),
                FakeObserver([]),
                monkeypatch,
                native=native,
            )
        assert database.operations.inspect(scope) is None


def test_ready_epoch_mismatch_refuses_before_file_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database, init_ticks=4097)
        subject = _subject(database, carrier, FakeObserver([]), monkeypatch)
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.REFUSED
        assert carrier.calls == 1
        assert database.operations.inspect(subject.owner.ownership.scope) is None


def test_uncertain_guest_absence_retains_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database, init_ticks=4097)
        subject = _subject(database, carrier, FakeObserver([], GuestAnchorPresence.UNKNOWN), monkeypatch)
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
        subject = _subject(database, carrier, observer, monkeypatch)
        assert _download(subject, root, BytesSink()) is WSL2DownloadStatus.RETAINED
        assert carrier.calls > 1 and observer.events == []
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        rows = database.operations.list_lifecycle_obligations(subject.owner.ownership)
        assert any(row.state is LifecycleObligationState.POSSIBLE_EFFECT for row in rows)
