"""Core upload custody with an exact existing Linux file-effect gate."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import (
    _file_effect_gate,
    _file_effect_gate_exchange,
    _file_gate_setup,
    _file_operation,
    _file_publication_exchange,
    _file_stage_exchange,
)
from agentworks.execution._file_effect_gate_bundle import _MODULE_NAMES as GATE_MODULES
from agentworks.execution._file_effect_gate_bundle import _PACKAGE as GATE_PACKAGE
from agentworks.execution._file_effect_gate_exchange import GateControlMutationUncertain
from agentworks.execution._file_gate_control import advance_file_effect_gate, setup_file_effect_gate
from agentworks.execution._file_gate_setup import FileEffectGateSetup, file_effect_gate_path
from agentworks.execution._file_gate_setup_recovery import FileGateSetupRecovery
from agentworks.execution._file_obligation import (
    FileCallFamily,
    FileCallObligationCodecError,
    decode_file_call_obligation,
)
from agentworks.execution._file_operation import FileOperation, PackageUploadMember
from agentworks.execution._file_publication import Create
from agentworks.execution._file_publication_bundle import _MODULE_NAMES as PUBLICATION_MODULES
from agentworks.execution._file_publication_bundle import _PACKAGE as PUBLICATION_PACKAGE
from agentworks.execution._file_stage_bundle import _MODULE_NAMES as STAGE_MODULES
from agentworks.execution._file_stage_bundle import _PACKAGE as STAGE_PACKAGE
from agentworks.execution._file_upload import FileUploadStatus
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._scratch_receipt import scratch_name
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, PreparedInvocation
from agentworks.operations import LifecycleObligation, OperationOwner
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._file_upload_support import BytesSource, new_metadata
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file gate requires Linux")
_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)


class _RowCheckingCarrier(LocalCarrier):
    def __init__(self, database: Database, owner: OperationOwner, gate) -> None:
        super().__init__()
        self._database = database
        self._owner = owner
        self._gate = gate
        self.expected_index: int | None = None
        self.seen: list[tuple[FileCallFamily, int | None]] = []

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        (row,) = (
            candidate
            for candidate in self._database.operations.list_lifecycle_obligations(self._owner.ownership)
            if candidate.state is LifecycleObligationState.POSSIBLE_EFFECT
        )
        call = decode_file_call_obligation(row.payload)
        assert call.effect_gate == self._gate
        assert call.batch_index == self.expected_index
        self.seen.append((call.family, call.batch_index))
        return super().execute(invocation, io=io, deadline=deadline)


def _install_bundle(monkeypatch: pytest.MonkeyPatch, package: str, modules: tuple[str, ...], guest: str) -> None:
    patch = (
        "import os\n"
        f"gate=sys.modules[{(package + '._file_effect_gate')!r}]\n"
        f"gate._GATE_NAMESPACE={_file_effect_gate._GATE_NAMESPACE!r}\n"
        "gate._ROOT_UID=os.geteuid()\n"
        f"guest._identity=lambda: sys.modules[{(package + '._vm_guest_identity_protocol')!r}]."
        f"VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {_GUEST.init_start_ticks!r})\n"
    )
    bundle = fixture_file_bundle(package, modules, guest, patch)
    exchange = (
        _file_stage_exchange
        if package == STAGE_PACKAGE
        else _file_publication_exchange
        if package == PUBLICATION_PACKAGE
        else _file_effect_gate_exchange
    )
    monkeypatch.setattr(exchange, "FIXED_BUNDLE", bundle)


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    namespace = tmp_path / "run" / "agentworks" / "file-gates-v1"
    namespace.mkdir(parents=True)
    monkeypatch.setattr(_file_gate_setup, "_NAMESPACE", str(namespace))
    monkeypatch.setattr(_file_effect_gate, "_GATE_NAMESPACE", str(namespace))
    monkeypatch.setattr(_file_effect_gate, "_ROOT_UID", os.geteuid())
    _install_bundle(monkeypatch, STAGE_PACKAGE, STAGE_MODULES, "_file_stage_guest")
    _install_bundle(monkeypatch, PUBLICATION_PACKAGE, PUBLICATION_MODULES, "_file_publication_guest")
    _install_bundle(monkeypatch, GATE_PACKAGE, GATE_MODULES, "_file_effect_gate_guest")
    database = Database(tmp_path / "owner.db")
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "upload-vm"), "gated-upload"
    )
    target = replace(target_for_owner(owner), boot_id=vm_guest_boot_id(_GUEST))
    operation = FileOperation(owner, target)
    gate_path = file_effect_gate_path(target, os.geteuid(), _GUEST)
    Path(gate_path).parent.mkdir(mode=0o700)
    gate = setup_file_effect_gate(gate_path, _GUEST, os.geteuid(), target.name, lambda: _GUEST)
    root = tmp_path / "root"
    root.mkdir()
    plan = IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )
    try:
        yield database, owner, operation, root, plan, gate
    finally:
        database.close()


def test_single_upload_persists_gate_before_dispatch_and_stale_generation_refuses(context) -> None:
    database, owner, operation, root, plan, gate = context
    carrier = _RowCheckingCarrier(database, owner, gate)
    common = dict(
        trusted_root_path=str(root),
        size=1,
        condition=Create(),
        create_metadata=new_metadata(),
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=gate,
    )
    first = operation.upload(carrier, relative_path="first", source=BytesSource(b"a"), **common)
    assert first.status is FileUploadStatus.COMPLETE
    assert (root / "first").read_bytes() == b"a"
    assert carrier.seen and all(family is FileCallFamily.UPLOAD for family, _ in carrier.seen)
    (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
    forged = json.loads(row.payload)
    forged["target"]["boot_id"] = target_for_owner(owner).boot_id
    with pytest.raises(FileCallObligationCodecError):
        decode_file_call_obligation(json.dumps(forged, sort_keys=True, separators=(",", ":")).encode("ascii"))

    advance_file_effect_gate(replace(gate, proposed_generation=b"d" * 16), lambda: _GUEST)
    second = operation.upload(carrier, relative_path="second", source=BytesSource(b"b"), **common)
    assert second.status is FileUploadStatus.FAILED
    assert not (root / "second").exists()
    assert second.binding.effect_gate == gate


def test_upload_setup_promotes_one_row_before_source_read(context) -> None:
    database, owner, operation, root, plan, gate = context
    setup = FileEffectGateSetup(gate.path, _GUEST)
    source = BytesSource(b"x")

    class CheckingCarrier(LocalCarrier):
        def __init__(self) -> None:
            super().__init__()
            self.rows: list[tuple[int, FileCallFamily, bytes, bool]] = []

        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
            call = decode_file_call_obligation(row.payload)
            self.rows.append((row.payload_revision, call.family, call.token or b"", call.gate_setup is not None))
            if self.calls == 0:
                assert call.gate_setup == setup and call.effect_gate is None
                assert source.calls == 0
            else:
                assert call.gate_setup is None and call.effect_gate == gate
            return super().execute(invocation, io=io, deadline=deadline)

    carrier = CheckingCarrier()
    result = operation.upload(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        source=source,
        size=1,
        condition=Create(),
        create_metadata=new_metadata(),
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        gate_setup=setup,
    )
    assert result.status is FileUploadStatus.COMPLETE
    assert (root / "target").read_bytes() == b"x"
    assert carrier.rows[0][3] and not any(row[3] for row in carrier.rows[1:])
    assert {row[1] for row in carrier.rows} == {FileCallFamily.UPLOAD}
    assert {row[2] for row in carrier.rows} == {result.token}
    assert len({row[0] for row in carrier.rows}) == 2
    assert len(database.operations.list_lifecycle_obligations(owner.ownership)) == 1


def test_upload_setup_publication_failure_retains_source_and_bound_row(
    context, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, operation, root, plan, gate = context
    source = BytesSource(b"x")
    original = LifecycleObligation.publish_payload

    def lost_reply(self: LifecycleObligation, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("lost publication reply")

    monkeypatch.setattr(LifecycleObligation, "publish_payload", lost_reply)
    with pytest.raises(RuntimeError, match="lost publication reply"):
        operation.upload(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="target",
            source=source,
            size=1,
            condition=Create(),
            create_metadata=new_metadata(),
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            gate_setup=FileEffectGateSetup(gate.path, _GUEST),
        )
    assert source.calls == 0
    assert len(operation.active_uploads) == 1
    (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
    assert decode_file_call_obligation(row.payload).effect_gate == gate
    assert not (root / "target").exists()


@pytest.mark.parametrize("control", [RuntimeError, KeyboardInterrupt])
def test_recovered_setup_only_upload_inspects_without_source_replay(context, control: type[BaseException]) -> None:
    database, owner, operation, root, plan, gate = context
    source = BytesSource(b"x")

    class LostSetupCarrier(LocalCarrier):
        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            super().execute(invocation, io=io, deadline=deadline)
            raise control("lost setup reply")

    with pytest.raises(control, match="lost setup reply"):
        operation.upload(
            LostSetupCarrier(),
            trusted_root_path=str(root),
            relative_path="target",
            source=source,
            size=1,
            condition=Create(),
            create_metadata=new_metadata(),
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            gate_setup=FileEffectGateSetup(gate.path, _GUEST),
        )
    assert source.calls == 0
    recovered = OperationOwner.recover(database.operations, owner.ownership, "c" * 32)
    (row,) = database.operations.list_lifecycle_obligations(recovered.ownership)
    assert decode_file_call_obligation(row.payload).gate_setup is not None
    result = FileGateSetupRecovery.open(
        recovered, replace(target_for_owner(recovered), boot_id=vm_guest_boot_id(_GUEST)), row
    ).inspect(LocalCarrier(), deadline=Deadline.after(30))
    assert result.observation is not None and result.observation.binding == gate
    assert (
        database.operations.list_lifecycle_obligations(recovered.ownership)[0].state
        is LifecycleObligationState.RESOLVED
    )
    assert source.calls == 0
    assert not (root / "target").exists()


@pytest.mark.parametrize("failure", ["promotion", "unsafe_gate"])
def test_upload_setup_failure_keeps_custody_without_source_read(
    context, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    database, owner, operation, root, plan, gate = context
    source = BytesSource(b"x")
    if failure == "promotion":

        def stop_promotion(*args, **kwargs):
            raise RuntimeError("promotion stopped")

        monkeypatch.setattr(_file_operation, "_prepare_upload_from_binding", stop_promotion)
    else:
        Path(gate.path).unlink()
        Path(gate.path).touch(mode=0o600)
    expected = RuntimeError if failure == "promotion" else GateControlMutationUncertain
    with pytest.raises(expected):
        operation.upload(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="target",
            source=source,
            size=1,
            condition=Create(),
            create_metadata=new_metadata(),
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            gate_setup=FileEffectGateSetup(gate.path, _GUEST),
        )
    assert source.calls == 0
    assert not (root / "target").exists()
    assert len(operation.active_uploads) == 1
    (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
    call = decode_file_call_obligation(row.payload)
    if failure == "promotion":
        assert call.effect_gate == gate
    else:
        assert call.gate_setup == FileEffectGateSetup(gate.path, _GUEST)
    with pytest.raises(StateError):
        owner.close()


def test_package_persists_exact_gate_for_each_child_before_dispatch(context) -> None:
    database, owner, operation, root, plan, gate = context
    carrier = _RowCheckingCarrier(database, owner, gate)
    carrier.expected_index = 0
    completed: list[int] = []

    def checkpoint(index: int, outcome) -> None:
        assert outcome.status is FileUploadStatus.COMPLETE
        completed.append(index)
        carrier.expected_index = index + 1

    outcomes = operation.upload_package(
        carrier,
        trusted_root_path=str(root),
        members=(
            PackageUploadMember("first", BytesSource(b"a"), 1, Create(), new_metadata()),
            PackageUploadMember("second", BytesSource(b"b"), 1, Create(), new_metadata()),
        ),
        checkpoint=checkpoint,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=gate,
    )
    assert len(outcomes) == 2 and completed == [0, 1]
    assert {index for _, index in carrier.seen} == {0, 1}
    assert all(family is FileCallFamily.PACKAGE_UPLOAD for family, _ in carrier.seen)
    assert (root / "first").read_bytes() == b"a"
    assert (root / "second").read_bytes() == b"b"


def test_package_setup_binds_index_zero_before_any_child_side_effect(context) -> None:
    database, owner, operation, root, plan, gate = context
    sources = (BytesSource(b"a"), BytesSource(b"b"))
    setup = FileEffectGateSetup(gate.path, _GUEST)

    class CheckingCarrier(LocalCarrier):
        def __init__(self) -> None:
            super().__init__()
            self.rows: list[tuple[int, int, bytes, bool]] = []

        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
            call = decode_file_call_obligation(row.payload)
            assert call.family is FileCallFamily.PACKAGE_UPLOAD
            assert call.batch_index in {0, 1}
            self.rows.append((row.payload_revision, call.batch_index, call.token or b"", call.gate_setup is not None))
            if self.calls == 0:
                assert call.gate_setup == setup and call.effect_gate is None
                assert all(source.calls == 0 for source in sources)
            else:
                assert call.gate_setup is None and call.effect_gate == gate
            return super().execute(invocation, io=io, deadline=deadline)

    carrier = CheckingCarrier()
    checkpoints: list[int] = []
    outcomes = operation.upload_package(
        carrier,
        trusted_root_path=str(root),
        members=(
            PackageUploadMember("first", sources[0], 1, Create(), new_metadata()),
            PackageUploadMember("second", sources[1], 1, Create(), new_metadata()),
        ),
        checkpoint=lambda index, outcome: checkpoints.append(index),
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        gate_setup=setup,
    )
    assert checkpoints == [0, 1]
    assert len(outcomes) == 2
    assert carrier.rows[0][1:] == (0, outcomes[0].token, True)
    assert all(not setup_only for _, _, _, setup_only in carrier.rows[1:])
    assert {index for _, index, _, _ in carrier.rows} == {0, 1}
    assert len(database.operations.list_lifecycle_obligations(owner.ownership)) == 1
    assert (root / "first").read_bytes() == b"a"
    assert (root / "second").read_bytes() == b"b"


def test_package_setup_lost_reply_recovers_only_setup(context) -> None:
    database, owner, operation, root, plan, gate = context
    source = BytesSource(b"a")

    class LostReplyCarrier(LocalCarrier):
        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            super().execute(invocation, io=io, deadline=deadline)
            raise RuntimeError("lost setup reply")

    with pytest.raises(RuntimeError, match="lost setup reply"):
        operation.upload_package(
            LostReplyCarrier(),
            trusted_root_path=str(root),
            members=(PackageUploadMember("first", source, 1, Create(), new_metadata()),),
            checkpoint=lambda index, outcome: None,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            gate_setup=FileEffectGateSetup(gate.path, _GUEST),
        )
    assert source.calls == 0
    recovered = OperationOwner.recover(database.operations, owner.ownership, "c" * 32)
    (row,) = database.operations.list_lifecycle_obligations(recovered.ownership)
    call = decode_file_call_obligation(row.payload)
    assert call.family is FileCallFamily.PACKAGE_UPLOAD and call.batch_index == 0
    assert call.gate_setup is not None
    result = FileGateSetupRecovery.open(
        recovered, replace(target_for_owner(recovered), boot_id=vm_guest_boot_id(_GUEST)), row
    ).inspect(LocalCarrier(), deadline=Deadline.after(30))
    assert result.observation is not None and result.observation.binding == gate
    assert (
        database.operations.list_lifecycle_obligations(recovered.ownership)[0].state
        is LifecycleObligationState.RESOLVED
    )
    assert not (root / "first").exists()


def test_package_setup_publication_failure_keeps_bound_index_zero(context, monkeypatch: pytest.MonkeyPatch) -> None:
    database, owner, operation, root, plan, gate = context
    source = BytesSource(b"a")
    original = LifecycleObligation.publish_payload

    def lost_reply(self: LifecycleObligation, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("lost publication reply")

    monkeypatch.setattr(LifecycleObligation, "publish_payload", lost_reply)
    with pytest.raises(RuntimeError, match="lost publication reply"):
        operation.upload_package(
            LocalCarrier(),
            trusted_root_path=str(root),
            members=(PackageUploadMember("first", source, 1, Create(), new_metadata()),),
            checkpoint=lambda index, outcome: None,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            gate_setup=FileEffectGateSetup(gate.path, _GUEST),
        )
    assert source.calls == 0
    with pytest.raises(StateError):
        owner.close()
    (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
    call = decode_file_call_obligation(row.payload)
    assert call.effect_gate == gate and call.batch_index == 0
    assert not (root / "first").exists()


def test_package_setup_rejects_wrong_guest_before_dispatch(context) -> None:
    database, owner, operation, root, plan, gate = context
    source = BytesSource(b"a")
    wrong = replace(_GUEST, init_start_ticks=_GUEST.init_start_ticks + 1)
    with pytest.raises(ValidationError):
        operation.upload_package(
            LocalCarrier(),
            trusted_root_path=str(root),
            members=(PackageUploadMember("first", source, 1, Create(), new_metadata()),),
            checkpoint=lambda index, outcome: None,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            gate_setup=FileEffectGateSetup(gate.path, wrong),
        )
    assert source.calls == 0
    assert not database.operations.list_lifecycle_obligations(owner.ownership)


def test_stale_gate_also_fences_failure_cleanup(context) -> None:
    database, owner, operation, root, plan, gate = context
    carrier = _RowCheckingCarrier(database, owner, gate)

    class AdvancingSource(BytesSource):
        def try_read(self, limit: int) -> bytes:
            if self.calls == 0:
                advance_file_effect_gate(replace(gate, proposed_generation=b"d" * 16), lambda: _GUEST)
            return super().try_read(limit)

    outcome = operation.upload(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        source=AdvancingSource(b"x"),
        size=1,
        condition=Create(),
        create_metadata=new_metadata(),
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=gate,
    )
    assert outcome.status is FileUploadStatus.FAILED
    assert outcome.scratch_cleanup_debt is not None
    assert (root / scratch_name(outcome.token)).exists()
    assert not (root / "target").exists()


@pytest.mark.parametrize("mismatch", ["scope", "path", "euid", "advance", "runtime"])
def test_mismatched_gate_refuses_before_dispatch(context, mismatch: str) -> None:
    database, owner, operation, root, plan, gate = context
    carrier = LocalCarrier()
    selection = runtime_selection(sys.executable)
    if mismatch == "scope":
        gate = replace(gate, scope_name="other")
    elif mismatch == "path":
        gate = replace(gate, path=gate.path[:-67] + "0" * 64 + ".db")
    elif mismatch == "euid":
        gate = replace(gate, euid=gate.euid + 1)
    elif mismatch == "advance":
        gate = replace(gate, proposed_generation=b"p" * 16)
    else:
        selection = RuntimeSelection(RuntimeTargetOS.DARWIN, sys.executable)
    with pytest.raises(ValidationError):
        operation.upload(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            source=BytesSource(b"x"),
            size=1,
            condition=Create(),
            create_metadata=new_metadata(),
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=selection,
            effect_gate=gate,
        )
    assert carrier.calls == 0
    assert database.operations.list_lifecycle_obligations(owner.ownership) == ()


def test_managed_target_boot_mismatch_refuses_before_dispatch(context) -> None:
    database, owner, _, root, plan, gate = context
    operation = FileOperation(owner, target_for_owner(owner))
    carrier = LocalCarrier()
    with pytest.raises(ValidationError):
        operation.upload(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            source=BytesSource(b"x"),
            size=1,
            condition=Create(),
            create_metadata=new_metadata(),
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            effect_gate=gate,
        )
    assert carrier.calls == 0
    assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
