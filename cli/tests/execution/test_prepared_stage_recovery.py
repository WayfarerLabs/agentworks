"""Real SQLite and protected span actions with scripted stage protocols.

These tests do not prove Windows native endpoint or predecessor drain.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_stage_exchange import FileStageObservationState
from agentworks.execution._file_stage_protocol import (
    FileStageCleanupRequest,
    FileStageReconcileRequest,
    FileStageRequest,
    decode_file_stage_request,
    empty_file_stage_body,
    encode_file_stage_cleanup_result,
    encode_file_stage_reconcile_result,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution._prepared_helper_obligation import (
    PreparedHelperObligationError,
    decode_prepared_helper_obligation,
    encode_prepared_helper_obligation,
)
from agentworks.execution._prepared_helper_stage import PreparedHelperStage
from agentworks.execution._prepared_stage_recovery import PreparedHelperStageRecovery
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS, _NumericGuestBootstrap
from agentworks.execution._scratch import ScratchReference, _cleanup_debt
from agentworks.execution._scratch_receipt import ScratchHistoricalOwnership, ScratchOwnershipUncertainty
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.wsl2 import WSL2Carrier
from agentworks.operations import (
    OperationOwner,
)
from agentworks.vms.target_identity import compose_managed_vm_target_identity
from tests.execution.test_prepared_helper_stage import ROOT, Carrier
from tests.execution.test_wsl2_platform_hold import BOOT
from tests.vms import test_recovery_vm_span as span_fixtures
from tests.vms.test_target_preparation import _MARKER

pytestmark = pytest.mark.windows


class Helpers:
    def __init__(self, reference: ScratchReference) -> None:
        self.reference = reference
        self.requests: list[FileStageRequest] = []
        self.code: int | None = 0
        self.dispatch = Dispatch.SENT
        self.complete = True
        self.corrupt = False
        self.missing = False
        self.unsettled = False
        self.runtime = "ready"
        self.failure: Failure | None = None
        self.error: BaseException | None = None
        self.custody: LocalDeliveryCustody | None = None

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody,
    ) -> CarrierReport:
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        request = decode_file_stage_request(io.input.data[io.input.data.index(b"{") :])
        self.requests.append(request)
        assert request.root_path == "/tmp" and request.relative_path == "prepared-input"
        assert request.effect_gate is None
        self.custody = custody
        if self.unsettled:
            custody.begin_process()
        if self.error is not None:
            raise self.error
        if isinstance(request, FileStageReconcileRequest):
            body = encode_file_stage_reconcile_result(
                ScratchOwnershipUncertainty() if self.missing else ScratchHistoricalOwnership(self.reference._ownership)
            )
        else:
            assert isinstance(request, FileStageCleanupRequest)
            assert request.cleanup_debt == _cleanup_debt(self.reference)
            body = encode_file_stage_cleanup_result()
        if self.corrupt:
            body = b"invalid"
        payload = f"AGW_RUNTIME_1:{request.nonce}:{self.runtime}:0\n".encode() + b"".join(
            encode_file_record(request.nonce, FileRecord(index, kind, data))
            for index, (kind, data) in enumerate(
                ((FileRecordKind.RESULT, body), (FileRecordKind.FINISHED, empty_file_stage_body()))
            )
        )
        io.output.stdout.try_write(memoryview(payload))
        channel = CapturedOutput(complete=self.complete, retention=Retention.DELIVERED)
        return CarrierReport(
            self.dispatch,
            ExitStatus(self.code) if self.code is not None and self.dispatch is Dispatch.SENT else None,
            stdout=channel,
            stderr=channel,
            failure=self.failure,
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    original = OperationOwner.register_lifecycle_obligation
    stages = []
    carriers = []

    def register(owner, kind, **options):
        if kind != "file-call":
            return original(owner, kind, **options)
        database = owner._repository._controller_identity
        guest = VMGuestIdentity(_MARKER, BOOT, 4096)
        target = compose_managed_vm_target_identity(database.get_vm("box"), ProviderLocator("wsl2:registration"), guest)
        carrier = Carrier(owner)
        stage = PreparedHelperStage(
            owner,
            carrier,
            target,
            _NumericGuestBootstrap(ROOT, guest),
            RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
            Deadline.after(30),
            3,
        )
        stages.append(stage)
        carriers.append(carrier)
        assert stage.begin() and stage.append(b"abc")
        assert not stage.helper_uncertain
        return stage._obligation

    with monkeypatch.context() as patch:
        patch.setattr(OperationOwner, "register_lifecycle_obligation", register)
        fixture = span_fixtures.setup.__wrapped__(tmp_path, monkeypatch)  # type: ignore[attr-defined]
        values = next(fixture)
    database, owner, old_id, _, _, _, _, span, _ = values
    row = owner.inspect_lifecycle_obligation(old_id)
    assert owner.list_pending_lifecycle_obligations() == (row,)
    recovery = PreparedHelperStageRecovery.open(owner, row)
    helpers = Helpers(carriers[0].reference)

    def activate():
        prepared = span.open(Deadline.after(10))

        def execute(carrier, invocation, *, io, deadline, custody):
            return helpers.execute(invocation, io=io, deadline=deadline, custody=custody)

        monkeypatch.setattr(WSL2Carrier, "execute", execute)
        return prepared

    try:
        yield database, owner, row, span, recovery, helpers, activate, stages[0]
    finally:
        recovery.drain(Deadline.after(1))
        fixture.close()


def test_passive_sole_row_then_fresh_actions_reconcile_historical_debt_and_cleanup(setup):
    database, owner, row, span, recovery, helpers, activate, stage = setup
    assert helpers.requests == [] and recovery._span is None
    assert recovery.cleanup_debt is None and not recovery.cleaned
    assert stage.offset == 3 and stage.reference is not None
    activate()
    support = tuple(
        item for item in owner.list_pending_lifecycle_obligations() if item.obligation_id != row.obligation_id
    )
    with span.action(Deadline.after(2)) as context:
        result = recovery.reconcile(context, Deadline.after(1))
    assert result.observation.state is FileStageObservationState.RECOVERED
    assert result.observation.reference is None
    assert recovery.cleanup_debt is not None
    with span.action(Deadline.after(2)) as context:
        result = recovery.cleanup(context, Deadline.after(1))
    assert result.observation.state is FileStageObservationState.CLEANED
    assert recovery.cleaned and not recovery.resolution_pending
    assert owner.list_pending_lifecycle_obligations() == support
    assert owner.inspect_lifecycle_obligation(row.obligation_id).state is LifecycleObligationState.RESOLVED
    assert len(helpers.requests) == 2
    span.close(Deadline.after(2))
    assert owner.list_pending_lifecycle_obligations() == ()
    assert database.operations.inspect(owner.ownership.scope).ownership == owner.ownership


@pytest.mark.parametrize("kind", ["carrier-dispatch", "managed-start", "prepared-helper-stage"])
def test_any_other_predecessor_debt_refuses_passive_open(setup, kind):
    _, owner, row, _, _, helpers, _, _ = setup
    owner.admit_recovery_support_obligation(kind, payload_version=1, payload=b"", obligation_id="e" * 32)
    with pytest.raises(StateError):
        PreparedHelperStageRecovery.open(owner, row)
    assert helpers.requests == []


@pytest.mark.parametrize("phase", ["begin", "chunk", "activation"])
def test_actual_unknown_predecessor_dispatch_refuses_even_with_valid_stage_receipt(setup, phase):
    database, _, _, _, _, helpers, _, stage = setup
    # A separate real ordinary owner produces an unresolved dispatch before takeover.
    from agentworks.db import OperationResourceKind, OperationScope

    predecessor = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "other"), "test")
    record = decode_prepared_helper_obligation(stage._payload)
    carrier = Carrier(predecessor)
    pending = PreparedHelperStage(
        predecessor,
        carrier,
        replace(record.target, name="other"),
        record.bootstrap,
        record.runtime,
        Deadline.after(5),
        3,
    )
    if phase == "begin":
        carrier.code = None
        assert not pending.begin()
    else:
        assert pending.begin()
        if phase == "chunk":
            carrier.code = None
            assert not pending.append(b"abc")
        else:
            borrow = predecessor.borrow()
            managed = borrow.install_dispatch_obligation("f" * 32, "managed-start", payload_version=1, payload=b"run")
            managed.mark_possible_effect()
            borrow.begin_attempt()
    assert carrier.reference is not None
    owner = OperationOwner.recover(database.operations, predecessor.ownership, "d" * 32)
    row = owner.inspect_lifecycle_obligation(pending.obligation_id)
    with pytest.raises(StateError):
        PreparedHelperStageRecovery.open(owner, row)
    assert helpers.requests == []


@pytest.mark.parametrize("fault", ["kind", "version", "revision", "registered", "resolved", "id", "payload", "owner"])
def test_exact_passive_envelope_and_recovered_owner_required(setup, fault):
    _, owner, row, _, _, helpers, _, _ = setup
    changes = {
        "kind": {"obligation_kind": "other"},
        "version": {"payload_version": 2},
        "revision": {"payload_revision": 1},
        "registered": {"state": LifecycleObligationState.REGISTERED},
        "resolved": {"state": LifecycleObligationState.RESOLVED},
        "id": {"obligation_id": "f" * 32},
        "payload": {"payload": b"{}"},
    }
    if fault == "owner":
        owner = OperationOwner(owner._repository, owner.ownership)
    else:
        row = replace(row, **changes[fault])
    with pytest.raises((StateError, ValidationError, PreparedHelperObligationError)):
        PreparedHelperStageRecovery.open(owner, row)
    assert helpers.requests == []


@pytest.mark.parametrize(
    "fault", ["escaped", "expired", "budget", "guest", "groups", "target", "runtime", "copied", "owner"]
)
def test_fresh_action_identity_and_budget_refuse_before_effect(setup, fault):
    database, owner, _, span, recovery, helpers, activate, _ = setup
    prepared = activate()
    deadline = Deadline.after(1)
    if fault == "escaped":
        with pytest.raises(StateError):
            recovery.reconcile(prepared, deadline)
    else:
        with span.action(deadline) as context:
            if fault == "expired":
                object.__setattr__(deadline, "expires_at", 0.0)
            elif fault == "budget":
                deadline = Deadline.after(10)
            elif fault == "guest":
                context = replace(context, guest=replace(context.guest, init_start_ticks=1))
            elif fault == "groups":
                context = replace(
                    context,
                    root_plan=replace(context.root_plan, expected=replace(context.root_plan.expected, groups=(0, 7))),
                )
            elif fault == "target":
                context = replace(context, target=replace(context.target, name="other"))
            elif fault == "runtime":
                context = replace(context, runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX))
            elif fault == "copied":
                recovery._owner = OperationOwner(owner._repository, owner.ownership)
            else:
                OperationOwner.recover(database.operations, owner.ownership, "e" * 32)
            with pytest.raises((StateError, ValidationError)):
                recovery.reconcile(context, deadline)
    assert helpers.requests == [] and recovery.dispatch is None


def test_fresh_launcher_mode_may_change_with_exact_numeric_authority(setup):
    _, owner, row, span, _, helpers, activate, _ = setup
    record = decode_prepared_helper_obligation(row.payload)
    historical = replace(
        record, bootstrap=replace(record.bootstrap, root_entry=replace(ROOT, mode=IdentityMode.DIRECT))
    )
    bound = owner.rebind_lifecycle_obligation(
        row.obligation_id, row.obligation_kind, payload_version=1, payload=row.payload
    )
    # Persist the alternate valid historical launcher before opening another adapter.
    from agentworks.db.operations import OperationRepository

    assert isinstance(owner._repository, OperationRepository)
    database = owner._repository._controller_identity
    payload = encode_prepared_helper_obligation(historical)
    database._conn.execute(
        "UPDATE lifecycle_obligations SET payload = ? WHERE obligation_id = ?", (payload, bound.obligation_id)
    )
    database._conn.commit()
    recovery = PreparedHelperStageRecovery.open(owner, owner.inspect_lifecycle_obligation(row.obligation_id))
    activate()
    with span.action(Deadline.after(2)) as context:
        assert context.root_plan.mode is not IdentityMode.DIRECT
        candidate = recovery.reconcile(context, Deadline.after(1))
        assert candidate.observation is not None
        assert candidate.observation.state is FileStageObservationState.RECOVERED
    assert len(helpers.requests) == 1


@pytest.mark.parametrize("fault", ["incarnation", "guest", "groups", "gid", "runtime"])
def test_genuine_action_must_match_full_persisted_authority(setup, fault):
    database, owner, row, span, _, helpers, activate, _ = setup
    record = decode_prepared_helper_obligation(row.payload)
    if fault == "incarnation":
        record = replace(record, target=replace(record.target, incarnation="v1:" + "f" * 64))
    elif fault == "guest":
        guest = replace(record.bootstrap.guest, init_start_ticks=1)
        record = replace(
            record,
            bootstrap=replace(record.bootstrap, guest=guest),
            target=replace(record.target, boot_id=vm_guest_boot_id(guest)),
        )
    elif fault in {"groups", "gid"}:
        expected = replace(ROOT.expected, groups=(0, 7), egid=7 if fault == "gid" else 0)
        record = replace(record, bootstrap=replace(record.bootstrap, root_entry=replace(ROOT, expected=expected)))
    else:
        record = replace(record, runtime=RuntimeSelection(RuntimeTargetOS.LINUX))
    database._conn.execute(
        "UPDATE lifecycle_obligations SET payload = ? WHERE obligation_id = ?",
        (encode_prepared_helper_obligation(record), row.obligation_id),
    )
    database._conn.commit()
    recovery = PreparedHelperStageRecovery.open(owner, owner.inspect_lifecycle_obligation(row.obligation_id))
    activate()
    with span.action(Deadline.after(2)) as context, pytest.raises(StateError):
        recovery.reconcile(context, Deadline.after(1))
    assert helpers.requests == [] and recovery.dispatch is None


@pytest.mark.parametrize("fault", ["stale", "active", "support"])
def test_passive_open_refuses_unsealed_stale_active_or_already_open_span(setup, fault):
    database, owner, row, _, recovery, helpers, activate, _ = setup
    if fault == "stale":
        OperationOwner.recover(database.operations, owner.ownership, "e" * 32)
    elif fault == "support":
        activate()
    else:
        recovery._opening = True
        recovery.dispatch = recovery._bound.open_dispatch()
    with pytest.raises(StateError):
        PreparedHelperStageRecovery.open(owner, row)
    assert helpers.requests == []


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_missing_or_corrupt_receipt_never_means_absence_or_cleanup_permission(setup, fault):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    setattr(helpers, fault, True)
    with span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
        with pytest.raises(StateError):
            recovery.cleanup(context, Deadline.after(1))
    assert recovery.cleanup_debt is None and not recovery.cleaned
    current = owner.inspect_lifecycle_obligation(row.obligation_id)
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT and current.payload == row.payload
    assert current.payload_version == 1 and current.payload_revision == 2
    assert len(helpers.requests) == 1
