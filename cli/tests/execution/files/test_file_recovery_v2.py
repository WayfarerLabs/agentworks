"""SQLite/span integration with synthetic native custody and helper transcripts.

The retained WSL2 hold and fixed preparation are real composition objects.
File responses and DOWNLOAD drain evidence are fixture-only: these tests do
not prove native privilege transitions, guest effects or predecessor drain.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.execution import _file_effect_gate_bundle as gate_bundle
from agentworks.execution import _file_snapshot_bundle as snapshot_bundle
from agentworks.execution._file_download_recovery import FileDownloadRecovery, _DownloadDrainEvidence
from agentworks.execution._file_effect_gate import FileEffectGateBinding
from agentworks.execution._file_effect_gate_protocol import (
    GateControlRequest,
    decode_gate_control_request,
    encode_gate_control_result,
)
from agentworks.execution._file_gate_setup import FileEffectGateSetup, file_effect_gate_path
from agentworks.execution._file_gate_setup_recovery import FileGateSetupRecovery
from agentworks.execution._file_obligation import (
    FileCallFamily,
    FileCallObligation,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_package_recovery import FilePackageFenceRecovery
from agentworks.execution._file_snapshot_protocol import (
    FileSnapshotCleanupRequest,
    decode_file_snapshot_request,
    encode_file_snapshot_cleanup_result,
    encode_file_snapshot_reconcile_result,
    snapshot_context,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution._runtime_prerequisite import (
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
    build_root_guest_bootstrap_argv,
)
from agentworks.execution._scratch_receipt import ScratchHistoricalOwnership
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import CarrierIO, Deadline, FiniteInput, PreparedInvocation, SinkOutput
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.vms._recovery_vm_span import RecoveryVMSpan
from tests.execution.files._runtime_support import runtime_nonce
from tests.execution.files.test_file_snapshot_protocol import _reference
from tests.execution.files.test_numeric_guest_helper_adoption import _CapturedCarrier
from tests.execution.test_wsl2_platform_hold import FakeNative, FakeObserver
from tests.vms.test_recovery_vm_span import setup as setup

pytestmark = pytest.mark.windows


class FileWire(_CapturedCarrier):
    """Feed sequenced protocol evidence through the actual guarded carrier."""

    def __init__(self, context) -> None:
        super().__init__()
        self.context = context
        self.requests: list[Any] = []
        self.control: BaseException | None = None
        self.status = 0

    def _run(self, invocation: PreparedInvocation, io: CarrierIO) -> int:
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        is_gate = io.input.data.startswith(gate_bundle.ROOT_PROGRAM.prefix)
        bundle = gate_bundle if is_gate else snapshot_bundle
        decode = decode_gate_control_request if is_gate else decode_file_snapshot_request
        request = decode(io.input.data[len(bundle.ROOT_PROGRAM.prefix) :])
        self.requests.append(request)
        root = self.context.root_plan
        argv, _, _ = build_root_guest_bootstrap_argv(
            root,
            request.identity,
            selection=self.context.runtime_selection,
            program=bundle.ROOT_PROGRAM,
            nonce=runtime_nonce(invocation),
            expected_guest=self.context.guest,
        )
        assert invocation.argv == argv
        if self.control is not None:
            raise self.control
        if isinstance(request, GateControlRequest):
            binding = request.binding
            if binding is None:
                binding = FileEffectGateBinding(
                    request.path, b"i" * 16, b"g" * 16, request.guest, request.euid, request.scope_name, 1, 2
                )
            else:
                binding = replace(binding, generation=binding.proposed_generation, proposed_generation=None)
            body = encode_gate_control_result(binding)
        elif isinstance(request, FileSnapshotCleanupRequest):
            body = encode_file_snapshot_cleanup_result()
        else:
            reference = _reference(token=request.token, context=snapshot_context(request.identity))
            body = encode_file_snapshot_reconcile_result(ScratchHistoricalOwnership(reference._ownership))
        for sequence, kind, value in ((0, FileRecordKind.RESULT, body), (1, FileRecordKind.FINISHED, b"{}")):
            record = encode_file_record(request.nonce, FileRecord(sequence, kind, value))
            assert io.output.stdout.try_write(memoryview(record)) == len(record)
        return self.status


def _install(setup, context, family: str, *, version: int = 2, root_body: bool = False):
    database, owner, old_id, *_ = setup
    plan = context.root_plan if root_body else context.ordinary_plan
    root_mode = IdentityMode.DIRECT if context.root_plan.mode is IdentityMode.SUDO_ROOT else IdentityMode.SUDO_ROOT
    historical = replace(plan, mode=root_mode if plan.expected.euid == 0 else IdentityMode.DEMOTE)
    bootstrap = _NumericGuestBootstrap(replace(context.root_plan, mode=root_mode), context.guest)
    path = file_effect_gate_path(context.target, plan.expected.euid, context.guest)
    gate = FileEffectGateBinding(path, b"i" * 16, b"g" * 16, context.guest, plan.expected.euid, "box", 1, 2)
    call = FileCallObligation(
        family=FileCallFamily.PACKAGE_UPLOAD if family == "package" else FileCallFamily.DOWNLOAD,
        target=context.target,
        root="/srv/data",
        relative_path="payload",
        identity_plan=historical,
        runtime_selection=context.runtime_selection,
        token=b"t" * 16,
        batch_index=0 if family == "package" else None,
        effect_gate=gate if family == "package" else None,
        gate_setup=FileEffectGateSetup(path, context.guest) if family == "setup" else None,
        bootstrap=bootstrap if version == 2 else None,
    )
    database.operations.publish_lifecycle_obligation_payload(
        owner.ownership, old_id, expected_revision=0, payload_version=version, payload=encode_file_call_obligation(call)
    )
    row = next(row for row in owner.list_lifecycle_obligations() if row.obligation_id == old_id)
    return call, row


def _open(owner, row, context, family: str):
    target = decode_file_call_obligation(row.payload).target
    if family == "download":
        # Synthetic adapter-journal proof, independent of fresh span readiness.
        evidence = _DownloadDrainEvidence(owner.ownership, row.obligation_id, row.payload_revision, row.payload)
        return FileDownloadRecovery.open(owner, target, row, evidence, context=context)
    cls = FileGateSetupRecovery if family == "setup" else FilePackageFenceRecovery
    return cls.open(owner, target, row, context=context)


def _run(adapter, family: str, deadline: Deadline, *carrier):
    method = {"download": "reconcile", "setup": "inspect", "package": "advance"}[family]
    return getattr(adapter, method)(*carrier, deadline=deadline)


def _wire(monkeypatch, context) -> FileWire:
    wire = FileWire(context)

    def execute(self, invocation, *, io, deadline):
        return wire.execute(invocation, io=io, deadline=deadline)

    monkeypatch.setattr(WSL2Carrier, "execute", execute)
    return wire


@pytest.mark.parametrize("family", ["download", "setup", "package"])
@pytest.mark.parametrize("root_body", [False, True])
@pytest.mark.parametrize("root_delivery", [False, True])
def test_v2_uses_fresh_launcher_preserves_historical_plans_and_actual_hold(
    setup, monkeypatch, family, root_body, root_delivery
):
    database, owner, old_id, platform, _, native, _, span, _ = setup
    if root_delivery:
        database._conn.execute("UPDATE vms SET admin_username = 'root' WHERE name = 'box'")
        database._conn.commit()
        span._workload_account = "root"
        platform.resolve_native_execution_binding.return_value = NativeExecutionBinding(
            WSL2Carrier(WSL2Connection("Ubuntu", "root", "wsl.exe")),
            "root",
            RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
        )
    span.open(Deadline.after(10))
    deadline = Deadline.after(10)
    with span.action(deadline) as context:
        call, row = _install(setup, context, family, root_body=root_body)
        adapter = _open(owner, row, context, family)
        wire = _wire(monkeypatch, context)
        result = _run(adapter, family, deadline)
        assert result.observation is not None and result.observation.state.value in {"recovered", "resolved"}
        if family == "download":
            adapter.cleanup(deadline=deadline)
        current = next(row for row in owner.list_lifecycle_obligations() if row.obligation_id == old_id)
        retained = decode_file_call_obligation(current.payload)
        assert current.payload_version == retained.payload_version == 2
        assert retained.identity_plan == call.identity_plan
        assert retained.bootstrap == call.bootstrap
        assert all(request.identity == call.identity_plan.expected for request in wire.requests)
        assert "eof" not in native.events
        if family == "setup":
            assert current.payload == row.payload and current.state is LifecycleObligationState.RESOLVED
        else:
            assert current.state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        _run(adapter, family, Deadline.after(10))


@pytest.mark.parametrize("family", ["download", "setup", "package"])
@pytest.mark.parametrize("fault", ["missing", "swapped", "escaped", "wrong_owner", "facts", "fake_span", "version"])
def test_v2_context_refuses_before_rebind_or_publication(setup, family, fault):
    _, owner, _, _, carrier, _, _, span, _ = setup
    span.open(Deadline.after(10))
    deadline = Deadline.after(10)
    with span.action(deadline) as context:
        _, row = _install(setup, context, family)
        supplied, selected_owner = context, owner
        if fault == "missing":
            supplied = None
        elif fault == "swapped":
            supplied = replace(context, carrier=Mock())
        elif fault == "wrong_owner":
            selected_owner = OperationOwner(owner._repository, owner.ownership)
        elif fault == "facts":
            supplied = replace(context, root_plan=replace(context.root_plan, mode=IdentityMode.DIRECT))
        elif fault == "fake_span":
            supplied = replace(context, _span=Mock())
        elif fault == "version":
            row = replace(row, payload_version=1)
        if fault != "escaped":
            with pytest.raises(StateError):
                _open(selected_owner, row, supplied, family)
    if fault == "escaped":
        with pytest.raises(StateError):
            _open(owner, row, supplied, family)
    current = next(value for value in owner.list_lifecycle_obligations() if value.obligation_id == row.obligation_id)
    assert current.payload == row.payload and current.payload_revision == row.payload_revision
    assert carrier.calls == ["guest", "admin", "root"]


@pytest.mark.parametrize("family", ["download", "setup", "package"])
@pytest.mark.parametrize(
    "fault",
    [
        "target",
        "guest_marker",
        "guest_boot",
        "guest_init",
        "root_gid",
        "root_groups",
        "body_uid",
        "body_gid",
        "body_groups",
        "runtime",
    ],
)
def test_v2_complete_identity_mismatch_refuses_before_dispatch(setup, family, fault):
    database, owner, old_id, _, carrier, _, _, span, _ = setup
    span.open(Deadline.after(10))
    with span.action(Deadline.after(10)) as context:
        call, row = _install(setup, context, family)
        if fault == "target":
            call = replace(call, target=replace(call.target, incarnation="v1:" + "f" * 64))
        elif fault.startswith("guest"):
            field, value = {
                "guest_marker": ("instance_marker", "f" * 32),
                "guest_boot": ("boot_id", "11111111-1111-4111-8111-111111111111"),
                "guest_init": ("init_start_ticks", 8192),
            }[fault]
            guest = replace(call.bootstrap.guest, **{field: value})
            # Full init mismatch can keep the exact derived target boot.
            from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id

            target = replace(call.target, boot_id=vm_guest_boot_id(guest))
            path = file_effect_gate_path(target, call.identity_plan.expected.euid, guest)
            call = replace(
                call,
                target=target,
                bootstrap=replace(call.bootstrap, guest=guest),
                effect_gate=replace(call.effect_gate, guest=guest, path=path) if call.effect_gate else None,
                gate_setup=FileEffectGateSetup(path, guest) if call.gate_setup else None,
            )
        elif fault.startswith("root"):
            expected = (
                replace(call.bootstrap.root_entry.expected, egid=1, groups=(0, 1))
                if fault == "root_gid"
                else replace(call.bootstrap.root_entry.expected, groups=(0, 1))
            )
            call = replace(
                call,
                bootstrap=replace(call.bootstrap, root_entry=replace(call.bootstrap.root_entry, expected=expected)),
            )
        elif fault.startswith("body"):
            expected = call.identity_plan.expected
            if fault == "body_uid":
                expected = replace(expected, euid=2001)
            elif fault == "body_gid":
                expected = replace(expected, egid=1003)
            else:
                expected = replace(expected, groups=(1002,))
            path = file_effect_gate_path(call.target, expected.euid, context.guest)
            call = replace(
                call,
                identity_plan=replace(call.identity_plan, expected=expected),
                effect_gate=replace(call.effect_gate, euid=expected.euid, path=path) if call.effect_gate else None,
                gate_setup=FileEffectGateSetup(path, context.guest) if call.gate_setup else None,
            )
        else:
            call = replace(call, runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX))
        database.operations.publish_lifecycle_obligation_payload(
            owner.ownership,
            old_id,
            expected_revision=row.payload_revision,
            payload_version=2,
            payload=encode_file_call_obligation(call),
        )
        row = next(value for value in owner.list_lifecycle_obligations() if value.obligation_id == old_id)
        with pytest.raises(StateError):
            _open(owner, row, context, family)
        assert carrier.calls == ["guest", "admin", "root"]


@pytest.mark.parametrize("family", ["download", "setup", "package"])
def test_escaped_after_open_and_free_carrier_refuse_before_pending_changes(setup, family):
    _, owner, old_id, _, carrier, _, _, span, _ = setup
    span.open(Deadline.after(10))
    with span.action(Deadline.after(10)) as context:
        _, row = _install(setup, context, family)
        adapter = _open(owner, row, context, family)
        with pytest.raises(StateError):
            _run(adapter, family, Deadline.after(1), context.carrier)
    with pytest.raises(StateError):
        _run(adapter, family, Deadline.after(1))
    with span.action(Deadline.after(10)), pytest.raises(StateError):
        _run(adapter, family, Deadline.after(1))
    current = next(value for value in owner.list_lifecycle_obligations() if value.obligation_id == old_id)
    assert current.payload == row.payload and current.payload_revision == row.payload_revision
    assert carrier.calls == ["guest", "admin", "root"]


@pytest.mark.parametrize("family", ["download", "setup", "package"])
def test_v1_rejects_context_and_encoded_version_mismatch_without_upgrade(setup, family):
    _, owner, old_id, _, _, _, _, span, _ = setup
    span.open(Deadline.after(10))
    with span.action(Deadline.after(10)) as context:
        _, row = _install(setup, context, family, version=1)
        with pytest.raises(StateError):
            _open(owner, row, context, family)
        with pytest.raises(StateError):
            _open(owner, replace(row, payload_version=2), None, family)
        _open(owner, row, None, family)
    current = next(value for value in owner.list_lifecycle_obligations() if value.obligation_id == old_id)
    assert current.payload == row.payload and current.payload_version == 1


@pytest.mark.parametrize("family", ["download", "package"])
def test_v2_cas_lost_reply_preserves_version_and_reopens_exact_latest_view(setup, monkeypatch, family):
    _, owner, old_id, _, _, _, _, span, _ = setup
    span.open(Deadline.after(10))
    original = LifecycleObligation.publish_payload
    interruption = KeyboardInterrupt()

    def lost(self, **kwargs):
        original(self, **kwargs)
        raise interruption

    with span.action(Deadline.after(10)) as context:
        call, row = _install(setup, context, family)
        adapter = _open(owner, row, context, family)
        wire = _wire(monkeypatch, context)
        with monkeypatch.context() as patch:
            patch.setattr(LifecycleObligation, "publish_payload", lost)
            with pytest.raises(KeyboardInterrupt) as caught:
                _run(adapter, family, Deadline.after(1))
            assert caught.value is interruption
        current = next(value for value in owner.list_lifecycle_obligations() if value.obligation_id == old_id)
        retained = decode_file_call_obligation(current.payload)
        assert current.payload_version == 2 and current.payload_revision == row.payload_revision + 1
        assert retained.bootstrap == call.bootstrap and retained.identity_plan == call.identity_plan
        assert wire.calls == (1 if family == "download" else 0)
    with span.action(Deadline.after(10)) as context:
        adapter = _open(owner, current, context, family)
        wire = _wire(monkeypatch, context)
        if family == "download":
            adapter.cleanup(deadline=Deadline.after(1))
        else:
            adapter.advance(deadline=Deadline.after(1))
        assert wire.calls == 1


@pytest.mark.parametrize("family", ["download", "setup", "package"])
@pytest.mark.parametrize("failure", ["control", "incomplete"])
def test_v2_unknown_helper_preserves_row_and_span(setup, monkeypatch, family, failure):
    _, owner, old_id, _, _, native, _, span, _ = setup
    span.open(Deadline.after(10))
    with span.action(Deadline.after(10)) as context:
        _, row = _install(setup, context, family)
        adapter = _open(owner, row, context, family)
        wire = _wire(monkeypatch, context)
        if failure == "control":
            wire.control = KeyboardInterrupt()
            with pytest.raises(KeyboardInterrupt):
                _run(adapter, family, Deadline.after(1))
        else:
            wire.status = 1
            if family == "download":
                # An observed debt cannot be published while its concrete
                # attempt remains uncertain; retain the original durable row.
                with pytest.raises(StateError):
                    _run(adapter, family, Deadline.after(1))
            else:
                result = _run(adapter, family, Deadline.after(1))
                assert result.carrier_completion.code == 1
    current = next(value for value in owner.list_lifecycle_obligations() if value.obligation_id == old_id)
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT and current.payload_version == 2
    with pytest.raises(StateError):
        span.close(Deadline.after(1))
    assert "eof" not in native.events and span.requires_owner_retention


@pytest.mark.parametrize("family", ["download", "setup", "package"])
def test_recovery_of_recovery_requires_new_owner_actual_span_and_exact_record(setup, monkeypatch, family):
    database, owner, old_id, platform, _, native, _, span, _ = setup
    fixed_execute = WSL2Carrier.execute
    span.open(Deadline.after(10))
    with span.action(Deadline.after(10)) as context:
        call, row = _install(setup, context, family)
        adapter = _open(owner, row, context, family)
        wire = _wire(monkeypatch, context)
        wire.control = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            _run(adapter, family, Deadline.after(1))
    retained = next(value for value in owner.list_lifecycle_obligations() if value.obligation_id == old_id)
    recovered = OperationOwner.recover(database.operations, owner.ownership, "e" * 32)
    current = next(value for value in recovered.list_lifecycle_obligations() if value.obligation_id == old_id)
    assert current.payload == retained.payload and current.payload_version == 2
    with pytest.raises(StateError):
        _open(recovered, current, context, family)
    monkeypatch.setattr(WSL2Carrier, "execute", fixed_execute)
    next_native = FakeNative([])
    next_span = RecoveryVMSpan(
        database,
        "box",
        platform,
        span._ctx,
        owner=recovered,
        hold_id="1" * 32,
        preparation_id="2" * 32,
        workload_account="admin",
        native=next_native,
        observer=FakeObserver([]),
    )
    next_span.open(Deadline.after(10))
    with next_span.action(Deadline.after(10)) as fresh:
        rebound = _open(recovered, current, fresh, family)
        wire = _wire(monkeypatch, fresh)
        result = _run(rebound, family, Deadline.after(1))
        assert result.observation is not None and result.observation.state.value in {"recovered", "resolved"}
        assert wire.calls == 1
    latest = next(value for value in recovered.list_lifecycle_obligations() if value.obligation_id == old_id)
    recorded = decode_file_call_obligation(latest.payload)
    assert recorded.bootstrap == call.bootstrap and recorded.identity_plan == call.identity_plan
    assert latest.payload_version == recorded.payload_version == 2
    with pytest.raises(StateError):
        next_span.close(Deadline.after(1))
    assert "eof" not in native.events and "eof" not in next_native.events


def test_download_fresh_readiness_never_substitutes_for_exact_drain_evidence(setup):
    _, owner, _, _, carrier, _, _, span, _ = setup
    span.open(Deadline.after(10))
    with span.action(Deadline.after(10)) as context:
        call, row = _install(setup, context, "download")
        evidence = _DownloadDrainEvidence(owner.ownership, row.obligation_id, row.payload_revision + 1, row.payload)
        with pytest.raises(StateError):
            FileDownloadRecovery.open(owner, call.target, row, evidence, context=context)
    assert carrier.calls == ["guest", "admin", "root"]


def test_v2_lost_fence_confirmation_is_adopted_without_another_dispatch(setup, monkeypatch):
    _, owner, old_id, _, _, _, _, span, _ = setup
    span.open(Deadline.after(10))
    original = LifecycleObligation.publish_payload
    publications = 0

    def lost(self, **kwargs):
        nonlocal publications
        row = original(self, **kwargs)
        publications += 1
        if publications == 2:
            raise KeyboardInterrupt()
        return row

    with span.action(Deadline.after(10)) as context:
        call, row = _install(setup, context, "package")
        adapter = _open(owner, row, context, "package")
        wire = _wire(monkeypatch, context)
        monkeypatch.setattr(LifecycleObligation, "publish_payload", lost)
        with pytest.raises(KeyboardInterrupt):
            adapter.advance(deadline=Deadline.after(1))
        with pytest.raises(StateError):
            adapter.advance(deadline=Deadline.after(1))
        assert wire.calls == 1
        latest = next(value for value in owner.list_lifecycle_obligations() if value.obligation_id == old_id)
        recorded = decode_file_call_obligation(latest.payload)
        assert latest.payload_revision == row.payload_revision + 2
        assert latest.payload_version == recorded.payload_version == 2
        assert recorded.bootstrap == call.bootstrap and recorded.identity_plan == call.identity_plan
