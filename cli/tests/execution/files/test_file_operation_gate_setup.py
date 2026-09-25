"""One borrowed file call across guest gate setup and snapshot dispatch."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_effect_gate_exchange, _file_gate_setup, _file_operation
from agentworks.execution._file_download import FileDownloadOutcome
from agentworks.execution._file_effect_gate import inspect_file_effect_gate, setup_file_effect_gate
from agentworks.execution._file_effect_gate_bundle import _MODULE_NAMES, _PACKAGE
from agentworks.execution._file_effect_gate_exchange import GateControlMutationUncertain
from agentworks.execution._file_gate_setup import FileEffectGateSetup
from agentworks.execution._file_obligation import FileCallFamily, FileCallObligation, decode_file_call_obligation
from agentworks.execution._file_operation import FileOperation, _PendingDownloadSetup
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    PreparedInvocation,
)
from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner, _PreRegistrationClosingRefusal
from tests.execution.files._file_download_support import BytesSink
from tests.execution.files._file_snapshot_support import LocalCarrier, install_fixture_bundle
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file gate is Linux-only")

_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)


class _InspectingCarrier(LocalCarrier):
    def __init__(self, database: Database, owner: OperationOwner, *, fail_setup: str | None = None) -> None:
        super().__init__()
        self._database = database
        self._owner = owner
        self._fail_setup = fail_setup
        self.payloads: list[FileCallObligation] = []
        self.borrows: list[OperationBorrow | None] = []

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        rows = self._database.operations.list_lifecycle_obligations(self._owner.ownership)
        assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT
        self.payloads.append(decode_file_call_obligation(rows[0].payload))
        self.borrows.append(self._owner._active_borrow)  # noqa: SLF001
        if self._fail_setup == "not_sent":
            self.calls += 1
            return CarrierReport(Dispatch.NOT_SENT, failure=Failure.DISPATCH)
        result = super().execute(invocation, io=io, deadline=deadline)
        if self.calls == 1 and self._fail_setup == "lost":
            raise RuntimeError("lost setup reply")
        if self.calls == 1 and self._fail_setup == "exit1":
            return replace(result, completion=ExitStatus(code=1))
        if self.calls == 1 and self._fail_setup == "malformed":
            return replace(result, stdout=replace(result.stdout, complete=False))
        return result


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    gate_root = tmp_path / "gates"
    gate_root.mkdir()
    monkeypatch.setattr(_file_gate_setup, "_NAMESPACE", str(gate_root))
    guest_patch = (
        "from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity\n"
        f"guest._identity=lambda: VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, "
        f"{_GUEST.init_start_ticks!r})\n"
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    install_fixture_bundle(monkeypatch, scratch, guest_patch)
    gate_patch = guest_patch.replace("_agw_file_snapshot", "_agw_file_effect_gate")
    monkeypatch.setattr(
        _file_effect_gate_exchange,
        "FIXED_BUNDLE",
        fixture_file_bundle(_PACKAGE, _MODULE_NAMES, "_file_effect_gate_guest", gate_patch),
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"bound-download")
    return source, gate_root


def _call(
    operation: FileOperation,
    carrier: LocalCarrier,
    source: Path,
    setup: FileEffectGateSetup,
) -> FileDownloadOutcome:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    plan = IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)
    sink = BytesSink()
    result = operation.download(
        carrier,
        trusted_root_path=str(source),
        relative_path="file",
        sink=sink,
        max_bytes=64,
        plan=plan,
        deadline=Deadline.after(20),
        runtime_selection=runtime_selection(sys.executable),
        gate_setup=setup,
    )
    assert bytes(sink.data) == b"bound-download"
    return result


def _context(database: Database, gate_root: Path) -> tuple[OperationOwner, FileOperation, FileEffectGateSetup]:
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "core-file-vm"), "gated-download"
    )
    target = target_for_owner(owner)
    operation = FileOperation(owner, target)
    setup = FileEffectGateSetup.for_target(target, os.geteuid(), _GUEST)
    Path(setup.path).parent.mkdir(parents=True)
    assert str(gate_root) in setup.path
    return owner, operation, setup


def test_acknowledged_setup_promotes_one_row_and_borrow_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, gate_root = _fixture(monkeypatch, tmp_path)
    database = Database(tmp_path / "state.db")
    owner, operation, setup = _context(database, gate_root)
    carrier = _InspectingCarrier(database, owner)
    try:
        outcome = _call(operation, carrier, source, setup)
        assert outcome.binding.effect_gate is not None
        assert carrier.calls >= 2
        assert carrier.payloads[0].gate_setup == setup
        assert carrier.payloads[0].effect_gate is None
        assert all(payload.gate_setup is None for payload in carrier.payloads[1:])
        assert all(payload.effect_gate == outcome.binding.effect_gate for payload in carrier.payloads[1:])
        assert all(payload.token == outcome.token for payload in carrier.payloads)
        assert len({id(borrow) for borrow in carrier.borrows}) == 1
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1 and rows[0].state is LifecycleObligationState.RESOLVED
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_two_downloads_adopt_same_gate_at_current_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, gate_root = _fixture(monkeypatch, tmp_path)
    database = Database(tmp_path / "state.db")
    owner, operation, setup = _context(database, gate_root)
    try:
        first = _call(operation, LocalCarrier(), source, setup)
        current = inspect_file_effect_gate(setup.path, _GUEST, os.geteuid(), "core-file-vm", lambda: _GUEST)
        second = _call(operation, LocalCarrier(), source, setup)
        assert first.binding.effect_gate == current
        assert second.binding.effect_gate == current
        assert len(database.operations.list_lifecycle_obligations(owner.ownership)) == 2
        assert all(
            row.state is LifecycleObligationState.RESOLVED
            for row in database.operations.list_lifecycle_obligations(owner.ownership)
        )
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("existing", ["incomplete", "wrong_epoch", "wrong_scope"])
def test_existing_unsafe_gate_refuses_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str
) -> None:
    source, gate_root = _fixture(monkeypatch, tmp_path)
    database = Database(tmp_path / "state.db")
    owner, operation, setup = _context(database, gate_root)
    if existing == "incomplete":
        Path(setup.path).touch(mode=0o600)
    elif existing == "wrong_epoch":
        other = replace(_GUEST, init_start_ticks=11)
        setup_file_effect_gate(setup.path, other, os.geteuid(), "core-file-vm", lambda: other)
    else:
        setup_file_effect_gate(setup.path, _GUEST, os.geteuid(), "other-vm", lambda: _GUEST)
    before = Path(setup.path).stat()
    carrier = _InspectingCarrier(database, owner)
    try:
        with pytest.raises(GateControlMutationUncertain):
            _call(operation, carrier, source, setup)
        after = Path(setup.path).stat()
        assert (after.st_dev, after.st_ino, after.st_size) == (before.st_dev, before.st_ino, before.st_size)
        assert carrier.calls == 1
        assert len(operation.active_downloads) == 1
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()


def test_mismatched_setup_is_refused_before_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, gate_root = _fixture(monkeypatch, tmp_path)
    database = Database(tmp_path / "state.db")
    owner, operation, setup = _context(database, gate_root)
    carrier = _InspectingCarrier(database, owner)
    try:
        with pytest.raises(ValidationError):
            _call(operation, carrier, source, replace(setup, path=str(tmp_path / "unrelated.db")))
        assert carrier.calls == 0
        assert not operation.active_downloads
        assert not database.operations.list_lifecycle_obligations(owner.ownership)
        owner.close()
    finally:
        database.close()


def test_owner_close_before_setup_registration_releases_unused_borrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, gate_root = _fixture(monkeypatch, tmp_path)
    database = Database(tmp_path / "state.db")
    owner, operation, setup = _context(database, gate_root)
    carrier = _InspectingCarrier(database, owner)
    original_install = OperationBorrow.install_dispatch_obligation

    def close_before_install(self: OperationBorrow, *args, **kwargs):
        with pytest.raises(StateError):
            owner.close()
        return original_install(self, *args, **kwargs)

    monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", close_before_install)
    try:
        with pytest.raises(_PreRegistrationClosingRefusal):
            _call(operation, carrier, source, setup)
        assert carrier.calls == 0
        assert not operation.active_downloads
        assert not database.operations.list_lifecycle_obligations(owner.ownership)
        owner.close()
    finally:
        database.close()


def test_explicit_not_sent_setup_resolves_unused_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, gate_root = _fixture(monkeypatch, tmp_path)
    database = Database(tmp_path / "state.db")
    owner, operation, setup = _context(database, gate_root)
    carrier = _InspectingCarrier(database, owner, fail_setup="not_sent")
    try:
        with pytest.raises(StateError):
            _call(operation, carrier, source, setup)
        assert carrier.calls == 1
        assert not Path(setup.path).exists()
        assert not operation.active_downloads
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1 and rows[0].state is LifecycleObligationState.RESOLVED
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


@pytest.mark.parametrize("failure", ["registration", "lost", "exit1", "malformed", "publication", "promotion"])
def test_setup_failures_retain_one_exact_pending_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source, gate_root = _fixture(monkeypatch, tmp_path)
    database = Database(tmp_path / "state.db")
    owner, operation, setup = _context(database, gate_root)
    fail_setup = failure if failure in {"lost", "exit1", "malformed"} else None
    carrier = _InspectingCarrier(database, owner, fail_setup=fail_setup)
    if failure == "registration":
        original_install = OperationBorrow.install_dispatch_obligation

        def lost_registration(self: OperationBorrow, *args, **kwargs):
            original_install(self, *args, **kwargs)
            raise RuntimeError("lost registration reply")

        monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", lost_registration)
    if failure == "publication":
        original_publish = LifecycleObligation.publish_payload

        def lost_publication(self: LifecycleObligation, *args, **kwargs):
            original_publish(self, *args, **kwargs)
            raise RuntimeError("lost publication reply")

        monkeypatch.setattr(LifecycleObligation, "publish_payload", lost_publication)
    if failure == "promotion":

        def fail_promotion(*args, **kwargs):
            raise RuntimeError("promotion stopped")

        monkeypatch.setattr(_file_operation, "_prepare_download_from_binding", fail_promotion)
    try:
        expected_error = GateControlMutationUncertain if failure in {"exit1", "malformed"} else RuntimeError
        with pytest.raises(expected_error):
            _call(operation, carrier, source, setup)
        active = operation.active_downloads
        assert len(active) == 1
        pending = active[0].prepared
        assert isinstance(pending, _PendingDownloadSetup)
        assert pending.setup == setup
        assert len(pending.token) == 16
        assert active[0].borrow is pending.operation._borrow  # noqa: SLF001
        assert pending.operation.has_outstanding_attempt is (failure in {"lost", "exit1"})
        assert active[0].outcome is None
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1 and rows[0].state in {
            LifecycleObligationState.REGISTERED,
            LifecycleObligationState.POSSIBLE_EFFECT,
        }
        payload = decode_file_call_obligation(rows[0].payload)
        assert payload.family is FileCallFamily.DOWNLOAD and payload.token == pending.token
        assert payload.gate_setup == setup or payload.effect_gate is not None
        assert carrier.calls == (0 if failure == "registration" else 1)
        with pytest.raises(StateError):
            owner.close()
    finally:
        database.close()
