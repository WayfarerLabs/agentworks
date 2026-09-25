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
from agentworks.execution import _file_effect_gate_exchange, _file_gate_setup
from agentworks.execution._file_effect_gate_bundle import _MODULE_NAMES, _PACKAGE
from agentworks.execution._file_gate_setup import FileEffectGateSetup
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
from agentworks.vms.target_identity import vm_guest_boot_id
from tests.execution.files._file_download_support import BytesSink, LostCallStdoutCarrier
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
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
    connection: WSL2Connection | None = None,
    native: FakeNative | None = None,
    observed_routes: list[WSL2Connection] | None = None,
    observed_carriers: list[WSL2Carrier] | None = None,
) -> tuple[WSL2OwnedDownload | None, Mock]:
    def execute(
        selected: WSL2Carrier, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline
    ) -> CarrierReport:
        if observed_routes is not None:
            observed_routes.append(selected.connection)
        if observed_carriers is not None:
            observed_carriers.append(selected)
        return carrier.execute(invocation, io=io, deadline=deadline)

    monkeypatch.setattr(WSL2Carrier, "execute", execute)
    platform = Mock(spec=WSL2Platform)
    platform.site_name = "local"
    platform.observe_provider_locator.side_effect = locators or [ProviderLocator("wsl2:registration")] * 3
    route = connection or WSL2Connection("Ubuntu", "admin", "wsl.exe")
    binding = NativeExecutionBinding(
        WSL2Carrier(route),
        route.user,
        RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
    )
    platform.resolve_native_execution_binding.return_value = binding
    platform.test_binding = binding
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


def _install_file_fixtures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scratch: Path) -> Path:
    gate_root = tmp_path / "gates"
    gate_root.joinpath(str(os.geteuid())).mkdir(parents=True)
    monkeypatch.setattr(_file_gate_setup, "_NAMESPACE", str(gate_root))
    guest_patch = (
        "from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity\n"
        f"guest._identity=lambda: VMGuestIdentity({_MARKER!r}, {BOOT!r}, 4096)\n"
    )
    install_fixture_bundle(monkeypatch, scratch, guest_patch)
    gate_patch = guest_patch.replace("_agw_file_snapshot", "_agw_file_effect_gate")
    monkeypatch.setattr(
        _file_effect_gate_exchange,
        "FIXED_BUNDLE",
        fixture_file_bundle(_PACKAGE, _MODULE_NAMES, "_file_effect_gate_guest", gate_patch),
    )
    return gate_root


def test_complete_download_uses_one_owner_and_releases_after_exact_guest_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"held-wsl-download")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    gate_root = _install_file_fixtures(monkeypatch, tmp_path, scratch)
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
        assert subject.preparation is not None and subject.preparation.target is not None
        assert subject.preparation.guest_result is not None
        observation = subject.preparation.guest_result.observation
        assert observation is not None and observation.identity is not None
        assert subject.outcome is not None and subject.outcome.binding.effect_gate is not None
        gate = subject.outcome.binding.effect_gate
        assert gate.guest == observation.identity
        assert gate.guest.instance_marker == _MARKER
        assert gate.euid == _plan().expected.euid
        assert gate.scope_name == subject.preparation.target.name
        assert subject.preparation.target.boot_id == vm_guest_boot_id(gate.guest)
        assert (
            gate.path
            == FileEffectGateSetup.for_target(
                subject.preparation.target, _plan().expected.euid, observation.identity
            ).path
        )
        assert gate.path.startswith(str(gate_root)) and Path(gate.path).is_file()


def test_selected_platform_download_rechecks_registration_with_owned_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    root.joinpath("source").write_bytes(b"held-wsl-download")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    _install_file_fixtures(monkeypatch, tmp_path, scratch)
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        routes: list[WSL2Connection] = []
        selected_route = WSL2Connection("Debian", "admin", "wsl.exe")
        subject, platform = _platform_subject(
            database, carrier, FakeObserver([]), monkeypatch, connection=selected_route, observed_routes=routes
        )
        assert subject is not None
        sink = BytesSink()
        assert _download(subject, root, sink) is WSL2DownloadStatus.COMPLETE
        assert bytes(sink.data) == b"held-wsl-download"
        assert platform.observe_provider_locator.call_count == 3
        assert platform.resolve_native_execution_binding.call_count == 1
        assert carrier.calls > 1
        assert routes and all(route == selected_route for route in routes)
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
    _install_file_fixtures(monkeypatch, tmp_path, scratch)
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        routes: list[WSL2Connection] = []
        carriers: list[WSL2Carrier] = []
        subject, platform = _platform_subject(
            database, carrier, FakeObserver([]), monkeypatch, observed_routes=routes, observed_carriers=carriers
        )
        assert subject is not None
        original = WSL2Connection("Ubuntu", "admin", "wsl.exe")
        initial_carrier = cast(WSL2Carrier, platform.test_binding.carrier)
        initial_carrier._connection = WSL2Connection("other", "admin", "C:/other/wsl.exe")
        assert type(subject._carrier) is WSL2Carrier
        assert subject._carrier is not initial_carrier
        sink = BytesSink()
        assert _download(subject, root, sink) is WSL2DownloadStatus.COMPLETE
        assert bytes(sink.data) == b"held-wsl-download"
        assert len(routes) > 1 and all(route == original for route in routes)
        assert len(carriers) > 1 and all(selected is subject._carrier for selected in carriers)
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


def test_registration_replacement_during_binding_resolution_refuses_before_guest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate_root = tmp_path / "gates"
    monkeypatch.setattr(_file_gate_setup, "_NAMESPACE", str(gate_root))
    setup = Mock(side_effect=AssertionError("gate setup before target preparation"))
    monkeypatch.setattr(FileEffectGateSetup, "for_target", setup)
    with closing(Database(tmp_path / "state.db")) as database:
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        current = ["wsl2:registration"]
        platform.observe_provider_locator.side_effect = lambda *args, **kwargs: ProviderLocator(current[0])

        def resolve(*args: object, **kwargs: object) -> NativeExecutionBinding:
            del args, kwargs
            current[0] = "wsl2:replacement"
            connection = WSL2Connection("Ubuntu", "admin", "wsl.exe")
            return NativeExecutionBinding(
                WSL2Carrier(connection),
                connection.user,
                RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
            )

        platform.resolve_native_execution_binding.side_effect = resolve
        subject = WSL2OwnedDownload.from_platform(
            database.operations,
            _vm(),
            platform,
            cast(RunContext, object()),
            deadline=Deadline.after(30),
            native=FakeNative([]),
            observer=FakeObserver([]),
        )
        assert subject is not None
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.REFUSED
        assert subject.preparation is not None
        assert subject.preparation.guest_result is None
        assert subject.file_operation is None
        setup.assert_not_called()
        assert not gate_root.exists()
        assert platform.observe_provider_locator.call_count == 2
        platform.resolve_native_execution_binding.assert_called_once()
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


def test_selected_platform_invalid_locator_does_not_acquire(tmp_path: Path) -> None:
    with closing(Database(tmp_path / "state.db")) as database:
        platform = Mock(spec=WSL2Platform)
        platform.site_name = "local"
        locator = ProviderLocator("wsl2:registration")
        object.__setattr__(locator, "token", "")
        platform.observe_provider_locator.return_value = locator
        with pytest.raises(ValidationError):
            WSL2OwnedDownload.from_platform(
                database.operations,
                _vm(),
                platform,
                cast(RunContext, object()),
                deadline=Deadline.after(30),
            )
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


def test_settled_file_with_unknown_hold_absence_retains_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    _install_file_fixtures(monkeypatch, tmp_path, scratch)
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        observer = FakeObserver([], GuestAnchorPresence.UNKNOWN)
        subject, _ = _platform_subject(database, carrier, observer, monkeypatch)
        assert subject is not None
        assert _download(subject, tmp_path, BytesSink()) is WSL2DownloadStatus.RETAINED
        assert carrier.calls > 1
        assert observer.events == ["observe"]
        assert subject.file_operation is not None
        assert database.operations.inspect(subject.owner.ownership.scope) is not None


def test_missing_source_resolves_file_and_hold_obligations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source-root"
    root.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    _install_file_fixtures(monkeypatch, tmp_path, scratch)
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
                connection=WSL2Connection("Ubuntu", "admin", "C:/Windows/System32/wsl.exe"),
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
    _install_file_fixtures(monkeypatch, tmp_path, scratch)
    with closing(Database(tmp_path / "state.db")) as database:
        carrier = GuestThenFileCarrier(database)
        carrier.file_carrier = LostCallStdoutCarrier(1)
        observer = FakeObserver([])
        subject = _subject(database, carrier, observer, monkeypatch)
        with pytest.raises(_file_effect_gate_exchange.GateControlMutationUncertain):
            _download(subject, root, BytesSink())
        assert carrier.calls > 1 and observer.events == []
        assert database.operations.inspect(subject.owner.ownership.scope) is not None
        rows = database.operations.list_lifecycle_obligations(subject.owner.ownership)
        assert any(row.state is LifecycleObligationState.POSSIBLE_EFFECT for row in rows)
