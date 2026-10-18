"""Generation fences for metadata, conditional removal and JSON children."""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import AgentworksError, ValidationError
from agentworks.execution import _file_effect_gate, _file_metadata_exchange, _file_object_exchange
from agentworks.execution._file_effect_gate_exchange import exchange_file_effect_gate
from agentworks.execution._file_effect_gate_protocol import GateControlOperation
from agentworks.execution._file_gate_control import advance_file_effect_gate
from agentworks.execution._file_gate_setup import FileEffectGateSetup
from agentworks.execution._file_gate_setup_recovery import FileGateSetupRecovery
from agentworks.execution._file_json import FileJsonOutcome, FileJsonStatus
from agentworks.execution._file_metadata_bundle import _MODULE_NAMES as METADATA_MODULES
from agentworks.execution._file_metadata_bundle import _PACKAGE as METADATA_PACKAGE
from agentworks.execution._file_metadata_exchange import (
    FileMetadataCandidateResult,
    FileMetadataObservationState,
    set_file_metadata,
)
from agentworks.execution._file_metadata_protocol import (
    FileMetadataFailureCode,
    FileMetadataOperation,
    FileMetadataRequest,
    decode_file_metadata_request,
    encode_file_metadata_request,
)
from agentworks.execution._file_object_bundle import _MODULE_NAMES as OBJECT_MODULES
from agentworks.execution._file_object_bundle import _PACKAGE as OBJECT_PACKAGE
from agentworks.execution._file_object_exchange import FileObjectCandidateResult, remove_file, stat_file
from agentworks.execution._file_object_protocol import (
    FileObjectFailureCode,
    FileObjectOperation,
    FileObjectRequest,
    decode_file_object_request,
    encode_file_object_request,
)
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_obligation import decode_file_call_obligation, encode_file_call_obligation
from agentworks.execution._file_result import reduce_file_metadata, reduce_file_remove
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import FileFailureReason
from agentworks.operations import OperationOwner
from tests.execution._bound_carrier_support import bind_carrier as bind_carrier
from tests.execution._bound_carrier_support import hold_operation_owner as hold_operation_owner
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._file_read_support import install_fixture_bundle as install_read_bundle
from tests.execution.files._file_upload_support import LostCallStdoutCarrier, new_metadata
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files.test_file_operation_upload_gate import _GUEST, _RowCheckingCarrier
from tests.execution.files.test_file_operation_upload_gate import context as context

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution._file_effect_gate import FileEffectGateBinding
    from agentworks.execution._file_operation import FileOperation
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import Carrier


def _install(
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    modules: tuple[str, ...],
    guest: str,
    exchange: ModuleType,
    extra: str = "",
) -> None:
    patch = (
        f"gate=sys.modules[{(package + '._file_effect_gate')!r}]\n"
        f"gate._GATE_NAMESPACE={_file_effect_gate._GATE_NAMESPACE!r}\n"
        "gate._ROOT_UID=os.geteuid()\n"
        f"guest._identity=lambda: sys.modules[{(package + '._vm_guest_identity_protocol')!r}]."
        f"VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {_GUEST.init_start_ticks!r})\n"
    )
    monkeypatch.setattr(
        exchange, "FIXED_BUNDLE", fixture_file_bundle(package, modules, guest, "import os\n" + patch + extra)
    )


@pytest.fixture
def effects(context, monkeypatch):
    _install(monkeypatch, METADATA_PACKAGE, METADATA_MODULES, "_file_metadata_guest", _file_metadata_exchange)
    _install(monkeypatch, OBJECT_PACKAGE, OBJECT_MODULES, "_file_object_guest", _file_object_exchange)
    install_read_bundle(monkeypatch)
    return context


def test_metadata_wire_carries_exact_gate(context) -> None:
    _, _, _, root, plan, gate = context
    request = FileMetadataRequest(
        "a" * 32,
        FileMetadataOperation.SET_METADATA,
        str(root),
        "file",
        0,
        0,
        0o600,
        None,
        plan.expected,
        effect_gate=gate,
    )
    assert decode_file_metadata_request(encode_file_metadata_request(request)) == request
    with pytest.raises(ValueError):
        encode_file_metadata_request(replace(request, effect_gate=replace(gate, proposed_generation=b"x" * 16)))


@pytest.mark.parametrize("case", ["set_metadata", "ensure_directory", "create_directory", "remove", "json"])
@pytest.mark.parametrize("setup", [False, True])
@pytest.mark.parametrize("runtime", [sys.executable, "/usr/bin/python3.11"])
def test_exact_gate_is_durable_before_every_dispatch_and_stale_effect_refuses(
    effects, case, setup, runtime, bind_carrier
):
    database, owner, operation, root, plan, gate = effects
    path = root / "target"
    if case in {"set_metadata", "remove"}:
        path.write_bytes(b"original")
    carrier = _RowCheckingCarrier(database, owner, gate)
    if setup:

        class SetupCarrier(_RowCheckingCarrier):
            def execute(self, invocation, *, io, deadline, custody=None):
                rows = database.operations.list_pending_lifecycle_obligations(owner.ownership)
                call = decode_file_call_obligation(rows[0].payload)
                if call.gate_setup is not None:
                    assert call.effect_gate is None
                    assert self.calls == 0
                    return LocalCarrier.execute(self, invocation, io=io, deadline=deadline, custody=custody)
                return super().execute(invocation, io=io, deadline=deadline, custody=custody)

        carrier = SetupCarrier(database, owner, gate)
    common = dict(
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(20),
        runtime_selection=runtime_selection(runtime),
    )
    binding = dict(gate_setup=FileEffectGateSetup(gate.path, gate.guest)) if setup else dict(effect_gate=gate)
    if case == "json":
        outcome = operation.update_json(
            carrier,
            **common,
            **binding,
            source=b'{"x":1}',
            strategy="replace",
            create=True,
            create_metadata=new_metadata(),
            max_bytes=1024,
            max_depth=4,
        )
        assert outcome.status is FileJsonStatus.COMPLETE
        assert outcome.binding.effect_gate == gate
        assert outcome.upload_outcome.binding.effect_gate == gate
        assert json.loads(path.read_bytes()) == {"x": 1}
    elif case == "remove":
        observed = stat_file(bind_carrier(LocalCarrier()), **common).observation
        assert observed is not None
        revision = observed.revision
        outcome = operation.remove(
            carrier, **common, **binding, expected_kind=FileKind.REGULAR, expected_revision=revision
        )
        assert not path.exists()
    else:
        metadata = new_metadata()
        outcome = getattr(operation, case)(
            carrier, **common, **binding, trusted_owner=metadata.owner, trusted_group=metadata.group, mode=0o700
        )
        assert outcome.result.observation.state is not FileMetadataObservationState.REFUSED
        assert path.exists()
    assert carrier.seen
    assert not database.operations.list_pending_lifecycle_obligations(owner.ownership)
    advance_file_effect_gate(replace(gate, proposed_generation=b"z" * 16), lambda: _GUEST)
    common["relative_path"] = "stale"
    refused = set_file_metadata(
        bind_carrier(LocalCarrier()), **common, effect_gate=gate, uid=os.geteuid(), gid=os.getegid(), mode=0o600
    )
    assert refused.observation is not None and refused.observation.failure is not None
    assert refused.observation.failure.code is FileMetadataFailureCode.EFFECT_GATE_REFUSED
    assert not (root / "stale").exists()


def test_stat_cannot_carry_gate_and_remove_wire_preserves_gate(effects, bind_carrier):
    _, _, _, root, plan, gate = effects
    (root / "file").write_bytes(b"x")
    observed = stat_file(
        bind_carrier(LocalCarrier()),
        trusted_root_path=str(root),
        relative_path="file",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
    ).observation
    assert observed is not None
    request = FileObjectRequest(
        "a" * 32,
        FileObjectOperation.REMOVE,
        str(root),
        "file",
        None,
        plan.expected,
        FileKind.REGULAR,
        observed.revision,
        gate,
    )
    assert decode_file_object_request(encode_file_object_request(request)) == request
    with pytest.raises(ValueError):
        encode_file_object_request(replace(request, operation=FileObjectOperation.STAT))
    advance_file_effect_gate(replace(gate, proposed_generation=b"z" * 16), lambda: _GUEST)
    result = remove_file(
        bind_carrier(LocalCarrier()),
        trusted_root_path=str(root),
        relative_path="file",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
        expected_kind=FileKind.REGULAR,
        expected_revision=observed.revision,
        effect_gate=gate,
    )
    assert result.observation is not None and result.observation.failure is not None
    assert result.observation.failure.code is FileObjectFailureCode.EFFECT_GATE_REFUSED
    assert (root / "file").read_bytes() == b"x"


@pytest.mark.parametrize(
    "target", ["/run", "/run/agentworks", "/run/agentworks/file-gates-v1", "/run/agentworks/file-gates-v1/1001/gate"]
)
def test_reserved_metadata_targets_refuse_before_dispatch(effects, target, bind_carrier):
    _, _, operation, _, plan, _ = effects
    carrier = LocalCarrier()
    target = target.replace("/run/agentworks/file-gates-v1", _file_effect_gate._GATE_NAMESPACE)
    with pytest.raises(ValidationError):
        operation.set_metadata(
            carrier,
            trusted_root_path="/",
            relative_path=target[1:],
            trusted_owner=new_metadata().owner,
            trusted_group=new_metadata().group,
            mode=0o700,
            plan=plan,
            deadline=Deadline.after(10),
            runtime_selection=runtime_selection(sys.executable),
        )
    assert carrier.calls == 0


def _json_call(
    operation: FileOperation,
    carrier: Carrier,
    root: Path,
    plan: IdentityPlan,
    *,
    effect_gate: FileEffectGateBinding | None = None,
    gate_setup: FileEffectGateSetup | None = None,
) -> FileJsonOutcome:
    return operation.update_json(
        carrier,
        trusted_root_path=str(root),
        relative_path="target",
        source=b'{"source":{"private":true}}',
        strategy="merge-overwrite",
        create=True,
        create_metadata=new_metadata(),
        max_bytes=4096,
        max_depth=8,
        plan=plan,
        deadline=Deadline.after(20),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=effect_gate,
        gate_setup=gate_setup,
    )


def test_json_conflict_retries_publish_each_exact_child_gate_before_dispatch(effects):
    database, owner, operation, root, plan, gate = effects
    target = root / "target"
    target.write_bytes(b'{"initial":true}')
    tokens = {}

    class RacingCarrier(_RowCheckingCarrier):
        def execute(self, invocation, *, io, deadline, custody=None):
            (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
            call = decode_file_call_obligation(row.payload)
            assert call.effect_gate == gate
            assert b'"private"' not in row.payload
            if call.token is not None:
                assert call.attempt is not None
                tokens[call.attempt] = call.token
            result = super().execute(invocation, io=io, deadline=deadline, custody=custody)
            if self.calls == 1:
                target.write_bytes(b'{"concurrent":true}')
            return result

    outcome = _json_call(operation, RacingCarrier(database, owner, gate), root, plan, effect_gate=gate)
    assert outcome.status is FileJsonStatus.COMPLETE
    assert outcome.publication_attempts == 2
    assert len(tokens) == 2 and len(set(tokens.values())) == 2
    assert json.loads(target.read_bytes()) == {"concurrent": True, "source": {"private": True}}


def test_json_uncertain_child_capture_keeps_exact_gate_token_and_cleanup_debt(effects):
    database, owner, operation, root, plan, gate = effects
    (root / "target").write_bytes(b'{"initial":true}')
    outcome = _json_call(operation, LostCallStdoutCarrier(4), root, plan, effect_gate=gate)
    assert outcome.status is FileJsonStatus.UNCERTAIN
    assert outcome.upload_outcome is not None and outcome.upload_outcome.scratch_cleanup_debt is not None
    (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    retained = decode_file_call_obligation(row.payload)
    assert retained.effect_gate == gate
    assert retained.token == outcome.upload_outcome.token and retained.attempt == 1
    assert retained.scratch_cleanup_debt == outcome.upload_outcome.scratch_cleanup_debt
    assert b'"private"' not in row.payload


@pytest.mark.parametrize("case", ["metadata", "remove", "json"])
def test_lost_setup_recovery_inspects_only_setup_and_stale_publisher_cannot_dispatch(
    effects, case, hold_operation_owner
):
    database, owner, operation, root, plan, gate = effects
    target = root / "target"
    target.write_bytes(b'{"initial":true}')

    class LostSetup(LocalCarrier):
        def execute(self, invocation, *, io, deadline, custody=None):
            super().execute(invocation, io=io, deadline=deadline, custody=custody)
            assert self.calls == 1
            raise RuntimeError("lost setup reply")

    carrier = LostSetup()
    setup = FileEffectGateSetup(gate.path, gate.guest)
    common = dict(
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(20),
        runtime_selection=runtime_selection(sys.executable),
        gate_setup=setup,
    )
    with pytest.raises(RuntimeError, match="lost setup reply"):
        if case == "json":
            _json_call(operation, carrier, root, plan, gate_setup=setup)
        elif case == "remove":
            # The retained revision is deliberately synthetic: SETUP must be the only dispatch.
            from tests.execution.files.test_file_publication_protocol import _revision

            operation.remove(carrier, **common, expected_kind=FileKind.REGULAR, expected_revision=_revision())
        else:
            operation.set_metadata(
                carrier, **common, trusted_owner=new_metadata().owner, trusted_group=new_metadata().group, mode=0o600
            )
    (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    call = decode_file_call_obligation(row.payload)
    assert call.gate_setup == setup and call.effect_gate is None and call.token is None
    assert target.read_bytes() == b'{"initial":true}' and carrier.calls == 1
    active = (
        operation.active_json_updates
        if case == "json"
        else operation.active_removals
        if case == "remove"
        else operation.active_metadata
    )[0]
    old_obligation = active.obligation
    assert old_obligation is not None
    recovered = hold_operation_owner(OperationOwner.recover(database.operations, owner.ownership, "c" * 32))
    (current,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    result = FileGateSetupRecovery.open(recovered, call.target, current).inspect(
        LocalCarrier(), deadline=Deadline.after(20)
    )
    assert result.observation is not None and result.observation.binding == gate
    receipt = recovered.inspect_lifecycle_obligation(current.obligation_id)
    assert receipt is not None and receipt.state is LifecycleObligationState.RESOLVED
    with pytest.raises(AgentworksError):
        old_obligation.publish_payload(
            expected_revision=row.payload_revision,
            payload_version=row.payload_version,
            payload=encode_file_call_obligation(replace(call, gate_setup=None, effect_gate=gate)),
        )
    assert target.read_bytes() == b'{"initial":true}' and carrier.calls == 1


@pytest.mark.parametrize("case", ["metadata", "remove"])
@pytest.mark.parametrize("runtime", [sys.executable, "/usr/bin/python3.11"])
def test_actual_packed_effect_holds_gate_until_cleanup_and_delayed_request_refuses(
    effects, monkeypatch, bind_carrier, case, runtime
):
    _, _, _, root, plan, gate = effects
    target = root / "target"
    target.write_bytes(b"original")
    entered, release = root / "entered", root / "release"
    extra = f"""
import time
_original=guest._operate
def held(request, expires_at):
 if request.operation.value == "stat":
  return _original(request, expires_at)
 result=_original(request, expires_at)
 open({str(entered)!r}, 'wb').close()
 while not os.path.exists({str(release)!r}):
  time.sleep(0.01)
 return result
guest._operate=held
"""
    if case == "metadata":
        _install(
            monkeypatch, METADATA_PACKAGE, METADATA_MODULES, "_file_metadata_guest", _file_metadata_exchange, extra
        )
    else:
        _install(monkeypatch, OBJECT_PACKAGE, OBJECT_MODULES, "_file_object_guest", _file_object_exchange, extra)
    common = dict(
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(20),
        runtime_selection=runtime_selection(runtime),
        effect_gate=gate,
    )
    if case == "metadata":

        def effect() -> FileMetadataCandidateResult | FileObjectCandidateResult:
            return set_file_metadata(
                bind_carrier(LocalCarrier()), **common, uid=os.geteuid(), gid=os.getegid(), mode=0o600
            )
    else:
        observed = stat_file(
            bind_carrier(LocalCarrier()), **{k: v for k, v in common.items() if k != "effect_gate"}
        ).observation
        assert observed is not None and observed.revision is not None
        revision = observed.revision

        def effect() -> FileMetadataCandidateResult | FileObjectCandidateResult:
            return remove_file(
                bind_carrier(LocalCarrier()), **common, expected_kind=FileKind.REGULAR, expected_revision=revision
            )

    proposed = replace(gate, proposed_generation=b"p" * 16)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(effect)
        try:
            until = time.monotonic() + 10
            while not entered.exists() and time.monotonic() < until and not pending.done():
                time.sleep(0.01)
            assert entered.exists()
            refused = exchange_file_effect_gate(
                bind_carrier(LocalCarrier()),
                operation=GateControlOperation.ADVANCE,
                path=gate.path,
                guest=gate.guest,
                scope_name=gate.scope_name,
                binding=proposed,
                plan=plan,
                deadline=Deadline.after(0.15),
                runtime_selection=runtime_selection(runtime),
            )
            assert refused.observation is None or refused.observation.binding is None
            assert not pending.done()
        finally:
            release.touch()
            result = pending.result()
    assert result.carrier_completion is not None and result.carrier_completion.code == 0
    confirmed = exchange_file_effect_gate(
        bind_carrier(LocalCarrier()),
        operation=GateControlOperation.ADVANCE,
        path=gate.path,
        guest=gate.guest,
        scope_name=gate.scope_name,
        binding=proposed,
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(runtime),
    )
    assert confirmed.observation is not None and confirmed.observation.binding == replace(gate, generation=b"p" * 16)
    stale = effect()
    assert stale.observation is not None and stale.observation.failure is not None
    assert stale.observation.failure.code.value == "effect_gate_refused"


@pytest.mark.parametrize("case", ["metadata", "remove"])
def test_stale_gate_reduces_to_public_refusal(effects, bind_carrier, case):
    _, _, operation, root, plan, gate = effects
    target = root / "target"
    target.write_bytes(b"original")
    observed = stat_file(
        bind_carrier(LocalCarrier()),
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
    ).observation
    assert observed is not None and observed.revision is not None
    advance_file_effect_gate(replace(gate, proposed_generation=b"z" * 16), lambda: _GUEST)
    common = dict(
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=gate,
    )
    if case == "metadata":
        outcome = operation.set_metadata(
            LocalCarrier(), **common, trusted_owner=new_metadata().owner, trusted_group=new_metadata().group, mode=0o600
        )
    else:
        outcome = operation.remove(
            LocalCarrier(), **common, expected_kind=FileKind.REGULAR, expected_revision=observed.revision
        )
    with pytest.raises(AgentworksError) as raised:
        if case == "metadata":
            reduce_file_metadata(outcome, entity_kind="vm", entity_name=gate.scope_name)
        else:
            reduce_file_remove(outcome, entity_kind="vm", entity_name=gate.scope_name)
    assert raised.value.details is not None and raised.value.details.reason is FileFailureReason.REFUSED
    assert target.read_bytes() == b"original"


@pytest.mark.parametrize("bad", ["proposal", "euid", "guest", "darwin"])
@pytest.mark.parametrize("case", ["metadata", "remove", "json"])
def test_invalid_gate_refuses_before_any_dispatch(effects, case, bad):
    database, owner, operation, root, plan, gate = effects
    if bad == "proposal":
        gate = replace(gate, proposed_generation=b"p" * 16)
    elif bad == "euid":
        gate = replace(gate, euid=gate.euid + 1)
    elif bad == "guest":
        gate = replace(gate, guest=replace(gate.guest, init_start_ticks=gate.guest.init_start_ticks + 1))
    carrier = LocalCarrier()
    selected = (
        RuntimeSelection(RuntimeTargetOS.DARWIN, sys.executable)
        if bad == "darwin"
        else runtime_selection(sys.executable)
    )
    common = dict(
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=selected,
        effect_gate=gate,
    )
    with pytest.raises(ValidationError):
        if case == "metadata":
            operation.set_metadata(
                carrier, **common, trusted_owner=new_metadata().owner, trusted_group=new_metadata().group, mode=0o600
            )
        elif case == "remove":
            from tests.execution.files.test_file_publication_protocol import _revision

            operation.remove(carrier, **common, expected_kind=FileKind.REGULAR, expected_revision=_revision())
        else:
            operation.update_json(
                carrier,
                **common,
                source=b'{"x":1}',
                strategy="replace",
                create=True,
                create_metadata=new_metadata(),
                max_bytes=1024,
                max_depth=8,
            )
    assert carrier.calls == 0
    assert not database.operations.list_pending_lifecycle_obligations(owner.ownership)


@pytest.mark.parametrize("case", ["metadata", "remove"])
def test_actual_guest_mismatch_refuses_before_effects(effects, monkeypatch, bind_carrier, case):
    _, _, _, root, plan, gate = effects
    target = root / "target"
    target.write_bytes(b"original")
    common = dict(
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
    )
    observed = stat_file(bind_carrier(LocalCarrier()), **common).observation
    assert observed is not None and observed.revision is not None
    package = METADATA_PACKAGE if case == "metadata" else OBJECT_PACKAGE
    wrong = (
        f"guest._identity=lambda: sys.modules[{package + '._vm_guest_identity_protocol'!r}]."
        f"VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, 11)\n"
    )
    result: FileMetadataCandidateResult | FileObjectCandidateResult
    if case == "metadata":
        _install(
            monkeypatch, METADATA_PACKAGE, METADATA_MODULES, "_file_metadata_guest", _file_metadata_exchange, wrong
        )
        result = set_file_metadata(
            bind_carrier(LocalCarrier()), **common, effect_gate=gate, uid=os.geteuid(), gid=os.getegid(), mode=0o600
        )
    else:
        _install(monkeypatch, OBJECT_PACKAGE, OBJECT_MODULES, "_file_object_guest", _file_object_exchange, wrong)
        result = remove_file(
            bind_carrier(LocalCarrier()),
            **common,
            effect_gate=gate,
            expected_kind=FileKind.REGULAR,
            expected_revision=observed.revision,
        )
    assert result.observation is not None and result.observation.failure is not None
    assert result.observation.failure.code.value == "effect_gate_refused"
    assert target.read_bytes() == b"original"


@pytest.mark.parametrize("target", ["/run", "/run/agentworks", "namespace"])
def test_metadata_guest_independently_refuses_reserved_convergence(effects, monkeypatch, bind_carrier, target):
    _, _, _, root, plan, _ = effects
    target = _file_effect_gate._GATE_NAMESPACE if target == "namespace" else target
    extra = f"""
from dataclasses import replace
decode=guest.decode_file_metadata_request
guest.decode_file_metadata_request=lambda data: replace(decode(data), root_path="/", relative_path={target[1:]!r})
def forbidden_parent(request):
 raise AssertionError("reserved convergence reached filesystem")
guest._parent=forbidden_parent
"""
    _install(monkeypatch, METADATA_PACKAGE, METADATA_MODULES, "_file_metadata_guest", _file_metadata_exchange, extra)
    result = set_file_metadata(
        bind_carrier(LocalCarrier()),
        trusted_root_path=str(root),
        relative_path="benign",
        uid=os.geteuid(),
        gid=os.getegid(),
        mode=0o600,
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert result.observation is not None and result.observation.failure is not None
    assert result.observation.failure.code is FileMetadataFailureCode.EFFECT_GATE_REFUSED


def test_json_escaping_control_capture_keeps_child_gate(effects):
    database, owner, operation, root, plan, gate = effects
    (root / "target").write_bytes(b'{"initial":true}')

    class Interrupted(LocalCarrier):
        def execute(self, invocation, *, io, deadline, custody=None):
            result = super().execute(invocation, io=io, deadline=deadline, custody=custody)
            if self.calls == 4:
                raise KeyboardInterrupt
            return result

    carrier = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        _json_call(operation, carrier, root, plan, effect_gate=gate)
    (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    retained = decode_file_call_obligation(row.payload)
    assert retained.effect_gate == gate and retained.token is not None and retained.attempt == 1
    assert retained.scratch_cleanup_debt is not None
    assert carrier.calls == 4
    assert b'"private"' not in row.payload


def test_remove_control_namespace_refuses_at_host_and_guest(effects, monkeypatch, bind_carrier):
    from tests.execution.files.test_file_publication_protocol import _revision

    _, _, operation, root, plan, _ = effects
    namespace = _file_effect_gate._GATE_NAMESPACE
    carrier = LocalCarrier()
    common = dict(
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
        expected_kind=FileKind.REGULAR,
        expected_revision=_revision(),
    )
    with pytest.raises(ValidationError):
        operation.remove(carrier, trusted_root_path="/", relative_path=namespace[1:], **common)
    assert carrier.calls == 0
    extra = f"""
from dataclasses import replace
decode=guest.decode_file_object_request
guest.decode_file_object_request=lambda data: replace(decode(data), root_path="/", relative_path={namespace[1:]!r})
def forbidden_parent(request):
 raise AssertionError("reserved removal reached filesystem")
guest._parent=forbidden_parent
"""
    _install(monkeypatch, OBJECT_PACKAGE, OBJECT_MODULES, "_file_object_guest", _file_object_exchange, extra)
    result = remove_file(bind_carrier(LocalCarrier()), trusted_root_path=str(root), relative_path="benign", **common)
    assert result.observation is not None and result.observation.failure is not None
    assert result.observation.failure.code is FileObjectFailureCode.EFFECT_GATE_REFUSED


def test_stat_namespace_never_opens_gate_or_observes_guest_epoch(effects, monkeypatch, bind_carrier):
    _, _, _, _, plan, gate = effects
    extra = """
def forbidden(*args, **kwargs):
 raise AssertionError("STAT reached gate admission")
guest._identity=forbidden
guest.hold_file_effect_gate=forbidden
"""
    _install(monkeypatch, OBJECT_PACKAGE, OBJECT_MODULES, "_file_object_guest", _file_object_exchange, extra)
    before = os.stat(gate.path)
    result = stat_file(
        bind_carrier(LocalCarrier()),
        trusted_root_path=_file_effect_gate._GATE_NAMESPACE.rsplit("/", 1)[0],
        relative_path="file-gates-v1",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
    )
    assert result.observation is not None and result.observation.revision is not None
    after = os.stat(gate.path)
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)


def test_nongated_darwin_entry_needs_no_guest_epoch_or_gate(effects, monkeypatch, bind_carrier):
    _, _, _, root, plan, _ = effects
    extra = f"""
from {METADATA_PACKAGE}._file_stat import FileRevision, FileStat
def forbidden(*args, **kwargs):
 raise AssertionError("nongated Darwin request reached gate admission")
guest._identity=forbidden
guest.hold_file_effect_gate=forbidden
def unchanged(request, expires_at):
 assert request.effect_gate is None
 return guest.FileMetadataResultControl(guest.FileMetadataResultKind.UNCHANGED,
  FileRevision(FileStat(1, 2, 0o100600, 1, request.uid, request.gid, 0, 1, 1)))
guest._operate=unchanged
sys.platform="darwin"
"""
    _install(monkeypatch, METADATA_PACKAGE, METADATA_MODULES, "_file_metadata_guest", _file_metadata_exchange, extra)
    result = set_file_metadata(
        bind_carrier(LocalCarrier()),
        trusted_root_path=str(root),
        relative_path="benign",
        uid=os.geteuid(),
        gid=os.getegid(),
        mode=0o600,
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.DARWIN, sys.executable),
    )
    assert result.observation is not None and result.observation.state is FileMetadataObservationState.UNCHANGED
    assert not (root / "benign").exists()
