"""Local-only proof of the DOWNLOAD helper's guest effect fence."""

from __future__ import annotations

import multiprocessing
import os
import secrets
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution._file_download import FileDownloadStatus
from agentworks.execution._file_effect_gate import (
    FileEffectGateBinding,
    FileEffectGateError,
    advance_file_effect_gate,
    decode_file_effect_gate,
    encode_file_effect_gate,
    hold_file_effect_gate,
    initialize_file_effect_gate,
)
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_snapshot_exchange import (
    FileSnapshotObservationState,
    snapshot_begin,
    snapshot_cleanup,
    snapshot_reconcile,
)
from agentworks.execution._file_snapshot_protocol import FileSnapshotFailureCode
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from tests.execution.files._file_download_support import BytesSink
from tests.execution.files._file_snapshot_support import LocalCarrier, fixture_source, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the fixed snapshot helper requires Linux")

_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)


def _observe_guest() -> VMGuestIdentity:
    return _GUEST


def _plan() -> IdentityPlan:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)


def _fixture(monkeypatch: pytest.MonkeyPatch, scratch: Path, guest: VMGuestIdentity = _GUEST) -> None:
    scratch.mkdir(exist_ok=True)
    scratch.chmod(0o1777)
    install_fixture_bundle(
        monkeypatch,
        scratch,
        f"""
guest._identity=lambda: guest._fixture_guest
from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity
guest._fixture_guest=VMGuestIdentity(
    {guest.instance_marker!r}, {guest.boot_id!r}, {guest.init_start_ticks!r})
""",
    )


def _hold_until_released(binding: FileEffectGateBinding, entered: str, release: str, exited: str | None = None) -> None:
    with hold_file_effect_gate(binding, _observe_guest):
        Path(entered).touch()
        while not Path(release).exists():
            time.sleep(0.01)
    if exited is not None:
        Path(exited).touch()


def _gate(tmp_path: Path) -> FileEffectGateBinding:
    gate = tmp_path / "effect.db"
    return initialize_file_effect_gate(str(gate), _GUEST, os.geteuid(), "gate-vm")


def _call(binding: FileEffectGateBinding, root: Path) -> FileCallObligation:
    target = ManagedTargetIdentity(ManagedTargetKind.VM, "gate-vm", "v1:" + "b" * 64, _GUEST.boot_id)
    return FileCallObligation(
        FileCallFamily.DOWNLOAD,
        target,
        str(root),
        "source",
        _plan(),
        runtime_selection(sys.executable),
        token=secrets.token_bytes(16),
        effect_gate=binding,
    )


def _crash_controller_with_held_effect(
    database_path: str, binding: FileEffectGateBinding, entered: str, release: str, exited: str
) -> None:
    database = Database(Path(database_path))
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "gate-vm"), "download")
    obligation = owner.register_lifecycle_obligation(
        "file-call",
        payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
        payload=encode_file_call_obligation(_call(binding, Path(database_path).parent)),
        obligation_id="a" * 32,
    )
    obligation.mark_possible_effect()
    helper = multiprocessing.get_context("spawn").Process(
        target=_hold_until_released, args=(binding, entered, release, exited)
    )
    helper.start()
    until = time.monotonic() + 10
    while not Path(entered).exists() and time.monotonic() < until:
        time.sleep(0.01)
    os._exit(91 if Path(entered).exists() else 92)


def _crash_controller_with_fixed_snapshot(
    database_path: str,
    root_path: str,
    scratch_path: str,
    binding: FileEffectGateBinding,
    entered: str,
    release: str,
) -> None:
    from agentworks.execution import _file_snapshot_exchange

    _file_snapshot_exchange.FIXED_BUNDLE = fixture_source(
        Path(scratch_path),
        f"""
import time
from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity
guest._identity=lambda: VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {_GUEST.init_start_ticks!r})
_original_operate=guest._operate
def _held_operate(request, expires_at):
    result=_original_operate(request, expires_at)
    assert result is not None
    with open({binding.path!r}, 'rb') as same_inode_source:
        same_inode_source.read(1)
    bound_descriptors=[]
    for candidate in os.listdir('/proc/self/fd'):
        try:
            metadata=os.fstat(int(candidate))
        except OSError:
            continue
        if (metadata.st_dev, metadata.st_ino)==({binding.device!r}, {binding.inode!r}):
            bound_descriptors.append(candidate)
    assert len(bound_descriptors)==1
    open({entered!r}, 'wb').close()
    while not os.path.exists({release!r}):
        time.sleep(0.01)
    return result
guest._operate=_held_operate
""",
    )
    database = Database(Path(database_path))
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "gate-vm"), "download")
    operation = FileOperation(owner, _call(binding, Path(root_path)).target)

    def crash_after_helper_enters() -> None:
        until = time.monotonic() + 10
        while not Path(entered).exists() and time.monotonic() < until:
            time.sleep(0.01)
        os._exit(91 if Path(entered).exists() else 92)

    threading.Thread(target=crash_after_helper_enters, daemon=True).start()
    operation.download(
        LocalCarrier(),
        trusted_root_path=root_path,
        relative_path="effect.db",
        sink=BytesSink(),
        max_bytes=65_536,
        plan=_plan(),
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=binding,
    )
    os._exit(93)


def test_binding_codec_and_exact_file_call_row_survive_reopen(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    metadata = Path(binding.path).stat()
    assert (binding.device, binding.inode) == (metadata.st_dev, metadata.st_ino)
    call = _call(binding, tmp_path)
    database = Database(tmp_path / "state.db")
    try:
        owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "gate-vm"), "download"
        )
        owner.register_lifecycle_obligation(
            "file-call",
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=encode_file_call_obligation(call),
            obligation_id="a" * 32,
        )
        row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
        assert decode_file_call_obligation(row.payload).effect_gate == binding
        proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
        assert decode_file_effect_gate(encode_file_effect_gate(proposed)) == proposed
        with pytest.raises(ValidationError):
            FileOperation(owner, call.target).download(
                LocalCarrier(),
                trusted_root_path=str(tmp_path),
                relative_path="source",
                sink=BytesSink(),
                max_bytes=1024,
                plan=_plan(),
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                effect_gate=replace(binding, scope_name="another-vm"),
            )
    finally:
        database.close()


def test_spawned_controller_loss_retains_row_and_cannot_overtake_live_helper(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    database_path = tmp_path / "owner.db"
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    exited = tmp_path / "exited"
    controller = multiprocessing.get_context("spawn").Process(
        target=_crash_controller_with_held_effect,
        args=(str(database_path), binding, str(entered), str(release), str(exited)),
    )
    controller.start()
    try:
        controller.join(15)
        assert controller.exitcode == 91
        database = Database(database_path)
        try:
            scope = OperationScope(OperationResourceKind.VM, "gate-vm")
            predecessor = database.operations.inspect(scope)
            assert predecessor is not None
            row = database.operations.list_lifecycle_obligations(predecessor.ownership)[0]
            retained = decode_file_call_obligation(row.payload)
            assert retained.effect_gate == binding
            proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
            # The proposal is durable before the takeover advance attempt.
            owner = OperationOwner.recover(database.operations, predecessor.ownership, "b" * 32)
            bound = owner.rebind_lifecycle_obligation(
                row.obligation_id, "file-call", payload_version=row.payload_version, payload=row.payload
            )
            bound.publish_payload(
                expected_revision=row.payload_revision,
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=encode_file_call_obligation(replace(retained, effect_gate=proposed)),
            )
            while_held = database.operations.list_lifecycle_obligations(owner.ownership)[0]
            assert decode_file_call_obligation(while_held.payload).effect_gate == proposed
            with pytest.raises(FileEffectGateError):
                advance_file_effect_gate(proposed, _observe_guest)
            release.touch()
            until = time.monotonic() + 10
            while not exited.exists() and time.monotonic() < until:
                time.sleep(0.01)
            assert exited.exists()
            advanced = advance_file_effect_gate(proposed, _observe_guest)
            assert advance_file_effect_gate(proposed, _observe_guest) == advanced
            with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest):
                raise AssertionError("delayed old effect must not enter")
            with hold_file_effect_gate(advanced, _observe_guest):
                pass
        finally:
            database.close()
    finally:
        release.touch()
        controller.join(10)


def test_fixed_snapshot_survives_controller_loss_and_is_fenced_before_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _gate(tmp_path)
    root = tmp_path
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    database_path = tmp_path / "owner.db"
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    controller = multiprocessing.get_context("spawn").Process(
        target=_crash_controller_with_fixed_snapshot,
        args=(str(database_path), str(root), str(scratch), binding, str(entered), str(release)),
    )
    controller.start()
    try:
        controller.join(15)
        assert controller.exitcode == 91
        database = Database(database_path)
        try:
            predecessor = database.operations.inspect(OperationScope(OperationResourceKind.VM, "gate-vm"))
            assert predecessor is not None
            row = database.operations.list_lifecycle_obligations(predecessor.ownership)[0]
            call = decode_file_call_obligation(row.payload)
            assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
            assert call.effect_gate == binding and call.token is not None
            assert (call.root, call.relative_path) == (str(root), "effect.db")
            source_metadata = (root / "effect.db").stat()
            assert (source_metadata.st_dev, source_metadata.st_ino) == (binding.device, binding.inode)
            proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
            owner = OperationOwner.recover(database.operations, predecessor.ownership, "b" * 32)
            bound = owner.rebind_lifecycle_obligation(
                row.obligation_id, "file-call", payload_version=row.payload_version, payload=row.payload
            )
            bound.publish_payload(
                expected_revision=row.payload_revision,
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=encode_file_call_obligation(replace(call, effect_gate=proposed)),
            )
            with pytest.raises(FileEffectGateError):
                advance_file_effect_gate(proposed, _observe_guest)
            release.touch()
            until = time.monotonic() + 15
            advanced = None
            while advanced is None and time.monotonic() < until:
                try:
                    advanced = advance_file_effect_gate(proposed, _observe_guest)
                except FileEffectGateError:
                    time.sleep(0.05)
            assert advanced is not None
            # Simulate an acknowledgment lost after the guest committed.
            assert advance_file_effect_gate(proposed, _observe_guest) == advanced
            pending_row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
            confirmed = owner.rebind_lifecycle_obligation(
                pending_row.obligation_id,
                "file-call",
                payload_version=pending_row.payload_version,
                payload=pending_row.payload,
            )
            confirmed.publish_payload(
                expected_revision=pending_row.payload_revision,
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=encode_file_call_obligation(replace(call, effect_gate=advanced)),
            )
            final_row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
            assert decode_file_call_obligation(final_row.payload).effect_gate == advanced
            install_fixture_bundle(
                monkeypatch,
                scratch,
                f"""
from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity
guest._identity=lambda: VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {_GUEST.init_start_ticks!r})
""",
            )
            delayed = snapshot_begin(
                LocalCarrier(),
                trusted_root_path=str(root),
                relative_path="effect.db",
                max_bytes=65_536,
                token=call.token,
                plan=_plan(),
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                effect_gate=binding,
            )
            assert delayed.observation is not None and delayed.observation.failure is not None
            assert delayed.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
            reconciled = snapshot_reconcile(
                LocalCarrier(),
                token=call.token,
                plan=_plan(),
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                effect_gate=advanced,
            )
            assert reconciled.observation is not None
            assert reconciled.observation.state is FileSnapshotObservationState.RECOVERED
            debt = reconciled.observation.cleanup_debt
            assert debt is not None
            cleaned = snapshot_cleanup(
                LocalCarrier(),
                token=call.token,
                cleanup_debt=debt,
                plan=_plan(),
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                effect_gate=advanced,
            )
            assert cleaned.observation is not None and cleaned.observation.state is FileSnapshotObservationState.CLEANED
        finally:
            database.close()
    finally:
        release.touch()
        controller.join(10)


def test_real_download_persists_gate_before_dispatch_and_retains_missing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _gate(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "source").write_bytes(b"gated source")
    _fixture(monkeypatch, tmp_path / "scratch")
    database = Database(tmp_path / "owner.db")
    try:
        owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "gate-vm"), "download"
        )
        operation = FileOperation(owner, _call(binding, root).target)

        class InspectingCarrier(LocalCarrier):
            def execute(self, invocation, *, io, deadline):
                rows = tuple(
                    row
                    for row in database.operations.list_lifecycle_obligations(owner.ownership)
                    if row.state is LifecycleObligationState.POSSIBLE_EFFECT
                )
                assert len(rows) == 1
                assert decode_file_call_obligation(rows[0].payload).effect_gate == binding
                return super().execute(invocation, io=io, deadline=deadline)

        sink = BytesSink()
        completed = operation.download(
            InspectingCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            sink=sink,
            max_bytes=1024,
            plan=_plan(),
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            effect_gate=binding,
        )
        assert completed.status is FileDownloadStatus.COMPLETE
        Path(binding.path).rename(tmp_path / "missing-gate.db")
        refused = operation.download(
            InspectingCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            sink=BytesSink(),
            max_bytes=1024,
            plan=_plan(),
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            effect_gate=binding,
        )
        assert refused.status is FileDownloadStatus.UNCERTAIN
        assert refused.requires_owner_retention
        assert not Path(binding.path).exists()
    finally:
        database.close()


def test_active_helper_blocks_advance_and_delayed_old_generation_refuses(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    helper = multiprocessing.get_context("spawn").Process(
        target=_hold_until_released, args=(binding, str(entered), str(release))
    )
    helper.start()
    try:
        until = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert entered.exists()
        proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
        # The original flock descriptor prevents takeover during the effect.
        expires_at = time.monotonic() + 0.05
        with pytest.raises(FileEffectGateError):
            advance_file_effect_gate(proposed, _observe_guest, expires_at=expires_at)
        assert time.monotonic() >= expires_at
        expires_at = time.monotonic() + 0.05
        with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest, expires_at=expires_at):
            raise AssertionError("second helper must not enter before its deadline")
        release.touch()
        helper.join(10)
        assert helper.exitcode == 0
        advanced = advance_file_effect_gate(proposed, _observe_guest)
        with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest):
            raise AssertionError("old helper must not enter")
        with hold_file_effect_gate(advanced, _observe_guest):
            pass
    finally:
        release.touch()
        helper.join(10)


def test_lost_advance_reply_reconciles_but_stale_advance_cannot_overwrite(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    first = replace(binding, proposed_generation=secrets.token_bytes(16))
    advanced = advance_file_effect_gate(first, _observe_guest)
    assert advance_file_effect_gate(first, _observe_guest) == advanced
    second = replace(advanced, proposed_generation=secrets.token_bytes(16))
    newest = advance_file_effect_gate(second, _observe_guest)
    with pytest.raises(FileEffectGateError):
        advance_file_effect_gate(first, _observe_guest)
    with hold_file_effect_gate(newest, _observe_guest):
        pass


def test_missing_replaced_or_wrong_guest_state_refuses_in_same_epoch(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    wrong_guest = replace(_GUEST, init_start_ticks=11)
    with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, lambda: wrong_guest):
        pass
    original = tmp_path / "old-effect.db"
    Path(binding.path).rename(original)
    with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest):
        pass
    shutil.copyfile(original, binding.path)
    Path(binding.path).chmod(0o600)
    assert Path(binding.path).read_bytes() == original.read_bytes()
    assert Path(binding.path).stat().st_ino != binding.inode
    with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest):
        pass


def test_fixed_snapshot_helper_checks_independent_guest_and_fences_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _gate(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "source").write_bytes(b"file-effect-gate")
    scratch = tmp_path / "scratch"
    _fixture(monkeypatch, scratch)
    plan = _plan()
    token = secrets.token_bytes(16)

    def begin(gate: FileEffectGateBinding):
        return snapshot_begin(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            max_bytes=1024,
            token=token,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            effect_gate=gate,
        )

    begun = begin(binding)
    assert begun.observation is not None and begun.observation.state is FileSnapshotObservationState.READY
    assert begun.observation.snapshot is not None
    from agentworks.execution._scratch import _cleanup_debt

    cleanup_debt = _cleanup_debt(begun.observation.snapshot.ready)
    proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
    advanced = advance_file_effect_gate(proposed, _observe_guest)
    delayed = begin(binding)
    assert delayed.observation is not None
    assert delayed.observation.failure is not None
    assert delayed.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
    refused_cleanup = snapshot_cleanup(
        LocalCarrier(),
        token=token,
        cleanup_debt=cleanup_debt,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=binding,
    )
    assert refused_cleanup.observation is not None
    assert refused_cleanup.observation.failure is not None
    assert refused_cleanup.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
    assert tuple(scratch.iterdir())
    cleaned = snapshot_cleanup(
        LocalCarrier(),
        token=token,
        cleanup_debt=cleanup_debt,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=advanced,
    )
    assert cleaned.observation is not None and cleaned.observation.state is FileSnapshotObservationState.CLEANED
    assert not tuple(scratch.iterdir())

    install_fixture_bundle(
        monkeypatch,
        scratch,
        f"""
guest._identity=lambda: guest._fixture_guest
from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity
guest._fixture_guest=VMGuestIdentity(
    {_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {(_GUEST.init_start_ticks + 1)!r})
""",
    )
    wrong_epoch = begin(advanced)
    assert wrong_epoch.observation is not None
    assert wrong_epoch.observation.failure is not None
    assert wrong_epoch.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
