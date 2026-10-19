"""Real ordinary file helpers under a coordinated host owner, not host epoch proof."""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._file_download import FileDownloadStatus
from agentworks.execution._file_download_recovery import FileDownloadRecovery, _DownloadDrainEvidence
from agentworks.execution._file_effect_gate import FileEffectGateBinding
from agentworks.execution._file_gate_setup import FileEffectGateSetup
from agentworks.execution._file_gate_setup_recovery import FileGateSetupRecovery
from agentworks.execution._file_inventory_exchange import FileInventoryObservationState
from agentworks.execution._file_json import FileJsonStatus
from agentworks.execution._file_metadata_exchange import FileMetadataObservationState
from agentworks.execution._file_mutation_recovery import FileMutationFenceRecovery
from agentworks.execution._file_object_exchange import FileObjectObservationState
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_obligation import FileCallFamily, FileCallObligation, encode_file_call_obligation
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_publication import Create
from agentworks.execution._file_upload import FileUploadStatus
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from tests.execution.files._file_download_support import BytesSink
from tests.execution.files._file_publication_support import install_fixture_bundle as install_publication_bundle
from tests.execution.files._file_read_support import install_fixture_bundle as install_read_bundle
from tests.execution.files._file_snapshot_support import install_fixture_bundle as install_snapshot_bundle
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage_bundle
from tests.execution.files._file_upload_support import BytesSource, new_metadata
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files.test_file_operation_write import ObligationInspectingCarrier, _update, _upload

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="ordinary helper effects require Linux")

type HostFiles = tuple[Database, OperationOwner, FileOperation, Path, Path, IdentityPlan]
_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)


@pytest.fixture
def host_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HostFiles]:
    install_stage_bundle(monkeypatch)
    install_publication_bundle(monkeypatch)
    install_read_bundle(monkeypatch)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o1777)
    scratch.chmod(0o1777)
    install_snapshot_bundle(monkeypatch, scratch)
    root = tmp_path / "approved"
    root.mkdir()
    db = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        db.operations, OperationScope(OperationResourceKind.PLATFORM_HOST, "selected-host"), "ordinary-files"
    )
    plan = IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )
    operation = FileOperation(owner, owner.ownership.scope)
    try:
        yield db, owner, operation, root, scratch, plan
    finally:
        assert owner.close_local_delivery(Deadline.after(3))
        db.close()


def test_real_host_effects_share_actual_owner_scope_and_resolve_exact_ledger(host_files: HostFiles) -> None:
    db, owner, operation, root, scratch, plan = host_files
    carrier = ObligationInspectingCarrier(db, owner)
    uploaded = _upload(operation, root, plan, BytesSource(b'{"original":1}'), carrier=carrier)
    assert uploaded.status is FileUploadStatus.COMPLETE
    updated = _update(operation, root, plan, b'{"added":2}', "merge-overwrite", carrier=carrier)
    assert updated.status is FileJsonStatus.COMPLETE
    assert json.loads(root.joinpath("target").read_bytes()) == {"added": 2, "original": 1}
    metadata = new_metadata()
    changed = operation.set_metadata(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        trusted_owner=metadata.owner,
        trusted_group=metadata.group,
        mode=0o600,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert changed.result is not None and changed.result.observation is not None
    assert changed.result.observation.state is FileMetadataObservationState.CHANGED
    assert stat.S_IMODE(root.joinpath("target").stat().st_mode) == 0o600
    created = operation.ensure_directory(
        carrier,
        trusted_root_path=str(root),
        relative_path="created",
        trusted_owner=metadata.owner,
        trusted_group=metadata.group,
        mode=0o700,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert created.result is not None and created.result.observation is not None
    assert created.result.observation.state is FileMetadataObservationState.CHANGED
    inventory = operation.list_directory(
        carrier,
        trusted_root_path=str(root),
        relative_path="created",
        max_entries=4,
        max_depth=1,
        max_encoded_bytes=4096,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert inventory.result is not None and inventory.result.observation is not None
    assert inventory.result.observation.state is FileInventoryObservationState.PRESENT
    sink = BytesSink()
    downloaded = operation.download(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        sink=sink,
        max_bytes=64,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert downloaded.status is FileDownloadStatus.COMPLETE
    assert bytes(sink.data) == root.joinpath("target").read_bytes()
    assert list(scratch.iterdir()) == []
    present = operation.stat(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert present.result is not None and present.result.observation is not None
    revision = present.result.observation.revision
    assert revision is not None
    removed = operation.remove(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        expected_kind=FileKind.REGULAR,
        expected_revision=revision,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert removed.result is not None and removed.result.observation is not None
    assert removed.result.observation.state is FileObjectObservationState.CHANGED
    assert not root.joinpath("target").exists()
    assert carrier.payloads and all(call.target == owner.ownership.scope for call in carrier.payloads)
    assert all(call.payload_version == 7 and call.bootstrap is None for call in carrier.payloads)
    assert owner.list_pending_lifecycle_obligations() == ()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(owner.ownership.scope) is None


@pytest.mark.parametrize("fault", ["wrong-name", "vm-scope", "host-vm-identity", "guest", "bootstrap"])
def test_host_binding_refuses_wrong_scope_and_fabricated_guest_before_borrow(
    host_files: HostFiles, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    _, owner, _, root, _, plan = host_files

    def forbidden() -> None:
        raise AssertionError("invalid binding borrowed owner")

    monkeypatch.setattr(owner, "borrow", forbidden)
    scope = owner.ownership.scope
    with pytest.raises(ValidationError):
        if fault == "wrong-name":
            FileOperation(owner, replace(scope, resource_name="other"))
        elif fault == "vm-scope":
            FileOperation(owner, replace(scope, resource_kind=OperationResourceKind.VM))
        elif fault == "host-vm-identity":
            FileOperation(
                owner,
                ManagedTargetIdentity(
                    ManagedTargetKind.PLATFORM_HOST, scope.resource_name, "v1:" + "a" * 64, _GUEST.boot_id
                ),
            )
        elif fault == "guest":
            FileOperation(owner, scope, guest=_GUEST)
        else:
            root_plan = replace(plan, expected=IdentityExpectation(0, 0, (0,)))
            FileOperation(owner, scope, bootstrap=_NumericGuestBootstrap(root_plan, _GUEST))
    assert owner.list_pending_lifecycle_obligations() == ()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("gate", ["bound", "setup"])
def test_host_gate_refusal_precedes_borrow_registration_and_dispatch(
    host_files: HostFiles, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    db, owner, operation, root, _, plan = host_files
    carrier = ObligationInspectingCarrier(db, owner)
    target = ManagedTargetIdentity(ManagedTargetKind.VM, "guest", "v1:" + "a" * 64, _GUEST.boot_id)
    setup = FileEffectGateSetup.for_target(target, plan.expected.euid, _GUEST)
    binding = FileEffectGateBinding(setup.path, b"i" * 16, b"g" * 16, _GUEST, plan.expected.euid, target.name, 1, 2)

    def forbidden() -> None:
        raise AssertionError("host gate borrowed owner")

    monkeypatch.setattr(owner, "borrow", forbidden)
    with pytest.raises(ValidationError):
        operation.upload(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            source=BytesSource(b"data"),
            size=4,
            condition=Create(),
            create_metadata=new_metadata(),
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            effect_gate=binding if gate == "bound" else None,
            gate_setup=setup if gate == "setup" else None,
        )
    assert carrier.payloads == [] and owner.list_pending_lifecycle_obligations() == ()
    assert list(root.iterdir()) == []


def test_host_path_refusal_keeps_real_owner_usable_without_effect(host_files: HostFiles) -> None:
    db, owner, operation, root, _, plan = host_files
    carrier = ObligationInspectingCarrier(db, owner)
    with pytest.raises(ValidationError):
        _upload(operation, root, plan, BytesSource(b"data"), carrier=carrier, relative_path="../escape")
    assert carrier.payloads == [] and owner.list_pending_lifecycle_obligations() == ()
    assert not root.parent.joinpath("escape").exists()
    assert _upload(operation, root, plan, BytesSource(b"data"), carrier=carrier).status is FileUploadStatus.COMPLETE


@pytest.mark.parametrize("family", ["download", "setup", "mutation"])
def test_actual_host_owner_cannot_rebind_even_with_exact_synthetic_drain(
    host_files: HostFiles, monkeypatch: pytest.MonkeyPatch, family: str
) -> None:
    db, owner, _, root, _, plan = host_files
    call = FileCallObligation(
        FileCallFamily.UPLOAD if family == "mutation" else FileCallFamily.DOWNLOAD,
        owner.ownership.scope,
        str(root),
        "target",
        plan,
        runtime_selection(sys.executable),
        token=b"t" * 16,
    )
    handle = owner.register_lifecycle_obligation(
        "file-call", payload_version=7, payload=encode_file_call_obligation(call)
    )
    handle.mark_possible_effect()
    (row,) = owner.list_pending_lifecycle_obligations()
    recovered = OperationOwner.recover(db.operations, owner.ownership, "b" * 32)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("host recovery rebound its file-call")

    monkeypatch.setattr(recovered, "rebind_possible_effect_lifecycle_obligation", forbidden)
    target = ManagedTargetIdentity(ManagedTargetKind.PLATFORM_HOST, "selected-host", "v1:" + "a" * 64, _GUEST.boot_id)
    with pytest.raises(StateError):
        if family == "download":
            evidence = _DownloadDrainEvidence(recovered.ownership, row.obligation_id, row.payload_revision, row.payload)
            FileDownloadRecovery.open(recovered, target, row, evidence)
        elif family == "setup":
            FileGateSetupRecovery.open(recovered, target, row)
        else:
            FileMutationFenceRecovery.open(recovered, target, row)
    assert list(root.iterdir()) == []
