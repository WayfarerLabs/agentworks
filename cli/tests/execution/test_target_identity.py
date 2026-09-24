"""Owned composition checks for private fixed-helper identity plans."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._account_protocol import (
    AccountFailure,
    encode_account_failure,
    encode_account_identity,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._target_identity import (
    TargetIdentityControlFact,
    TargetIdentityFailure,
    TargetIdentityStatus,
    prepare_target_identity,
)
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers._subprocess import run_process
from agentworks.operations import OperationAttempt, OperationOwner

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.windows

_ROOT = IdentityExpectation(0, 0, (0,))
_WORKER = IdentityExpectation(1001, 1002, (1002, 1003))
_OTHER = IdentityExpectation(2001, 2002, (2002,))


def _runtime(path: str = "/usr/bin/python3") -> RuntimeSelection:
    return RuntimeSelection(RuntimeTargetOS.LINUX, path)


def _nonce(invocation: PreparedInvocation) -> str:
    marker = invocation.argv.index("agentworks-runtime-prerequisite")
    return invocation.argv[marker + 1]


@dataclass
class SyntheticCarrier:
    identities: dict[str, IdentityExpectation] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    malformed: set[str] = field(default_factory=set)
    reflected: set[str] = field(default_factory=set)
    runtime_missing: set[str] = field(default_factory=set)
    incomplete: set[str] = field(default_factory=set)
    dispatch: Dispatch = Dispatch.SENT
    completion: ExitStatus | None = ExitStatus(code=0)
    uncertain_on_account: str | None = None
    expire_on_return: bool = False
    expire_on_account: str | None = None
    calls: list[str] = field(default_factory=list)
    deadlines: list[Deadline] = field(default_factory=list)

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        assert isinstance(io.input, FiniteInput)
        assert isinstance(io.output, SinkOutput)
        request = json.loads(io.input.data)
        account = request["account"]
        nonce = _nonce(invocation)
        self.calls.append(account)
        self.deadlines.append(deadline)
        if self.dispatch is Dispatch.SENT:
            if account in self.runtime_missing:
                payload = f"AGW_RUNTIME_1:{nonce}:missing:-\n".encode("ascii")
            else:
                prefix = f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode("ascii")
                if account in self.missing:
                    response = encode_account_failure(nonce, AccountFailure.MISSING)
                elif account in self.malformed:
                    response = b'{"invalid":true}'
                elif account in self.reflected:
                    response = json.dumps(
                        {
                            "account": account,
                            "identity": {"egid": 1002, "euid": 1001, "groups": [1002, 1003]},
                            "nonce": nonce,
                            "status": "identity",
                            "version": 1,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("ascii")
                else:
                    response = encode_account_identity(nonce, self.identities[account])
                payload = prefix + response
            io.output.stdout.try_write(memoryview(payload))
        if self.expire_on_return or account == self.expire_on_account:
            object.__setattr__(deadline, "expires_at", 0.0)
        streams = CapturedOutput(
            complete=account not in self.incomplete,
            retention=Retention.DELIVERED,
        )
        return CarrierReport(
            self.dispatch,
            None if account == self.uncertain_on_account else self.completion,
            stdout=streams,
            stderr=CapturedOutput(complete=True, retention=Retention.DELIVERED),
        )


class LocalCarrier:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        result = run_process(list(invocation.argv), io=io, deadline=deadline)
        completion = None
        if result.exit_status is not None:
            completion = (
                ExitStatus(signal=-result.exit_status)
                if result.exit_status < 0
                else ExitStatus(code=result.exit_status)
            )
        return CarrierReport(
            Dispatch.SENT if result.started else Dispatch.NOT_SENT,
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )


class ControlStop(BaseException):
    pass


class RaisingCarrier(SyntheticCarrier):
    def __init__(self, control: BaseException) -> None:
        super().__init__()
        self.control = control

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, io, deadline
        raise self.control


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[tuple[Database, OperationOwner]]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "identity-vm"),
        "target-identity",
    )
    try:
        yield database, owner
    finally:
        database.close()


def _prepare(
    owner: OperationOwner,
    carrier,
    *,
    delivery: str = "worker",
    workload: str = "worker",
    include_elevated: bool = False,
    deadline: Deadline | None = None,
    runtime_path: str = "/usr/bin/python3",
):
    return prepare_target_identity(
        carrier,
        delivery_account=delivery,
        workload_account=workload,
        include_elevated=include_elevated,
        runtime_selection=_runtime(runtime_path),
        deadline=deadline or Deadline.after(15),
        owner=owner,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="the account helper requires Linux")
def test_real_fixed_helper_composes_current_direct_identity_under_one_borrow(
    owned: tuple[Database, OperationOwner],
) -> None:
    import pwd

    _, owner = owned
    account = pwd.getpwuid(os.geteuid()).pw_name
    carrier = LocalCarrier()

    result = _prepare(owner, carrier, delivery=account, workload=account, runtime_path=sys.executable)

    assert result.status is TargetIdentityStatus.PREPARED
    assert result.ordinary_plan is not None and result.ordinary_plan.mode is IdentityMode.DIRECT
    assert result.ordinary_plan.expected.euid == os.geteuid()
    assert result.elevated_plan is None
    assert result.delivery_result is result.workload_result
    assert carrier.calls == 1
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    ("carrier", "delivery", "workload", "include_elevated", "ordinary_mode", "elevated_mode", "calls"),
    [
        (
            SyntheticCarrier({"delivery": _WORKER, "workload": _WORKER}),
            "delivery",
            "workload",
            False,
            IdentityMode.DIRECT,
            None,
            2,
        ),
        (
            SyntheticCarrier({"delivery": _ROOT, "workload": _WORKER}),
            "delivery",
            "workload",
            False,
            IdentityMode.DEMOTE,
            None,
            2,
        ),
        (SyntheticCarrier({"root": _ROOT}), "root", "root", True, IdentityMode.DIRECT, IdentityMode.DIRECT, 1),
        (
            SyntheticCarrier({"delivery": _ROOT, "workload": _WORKER}),
            "delivery",
            "workload",
            True,
            IdentityMode.DEMOTE,
            IdentityMode.DIRECT,
            2,
        ),
        (
            SyntheticCarrier({"worker": _WORKER, "root": _ROOT}),
            "worker",
            "worker",
            True,
            IdentityMode.DIRECT,
            IdentityMode.SUDO_ROOT,
            2,
        ),
    ],
    ids=["direct-alias", "demote", "same-root", "root-demote-and-direct", "sudo-root"],
)
def test_synthetic_identity_selection(
    owned: tuple[Database, OperationOwner],
    carrier: SyntheticCarrier,
    delivery: str,
    workload: str,
    include_elevated: bool,
    ordinary_mode: IdentityMode,
    elevated_mode: IdentityMode | None,
    calls: int,
) -> None:
    _, owner = owned

    result = _prepare(owner, carrier, delivery=delivery, workload=workload, include_elevated=include_elevated)

    assert result.status is TargetIdentityStatus.PREPARED
    assert result.ordinary_plan is not None and result.ordinary_plan.mode is ordinary_mode
    assert (result.elevated_plan.mode if result.elevated_plan else None) is elevated_mode
    if result.elevated_plan is not None and elevated_mode is IdentityMode.SUDO_ROOT:
        assert result.elevated_plan.expected == _ROOT
        assert result.root_result is not None
    else:
        assert result.root_result is None
    assert len(carrier.calls) == calls
    assert len({id(deadline) for deadline in carrier.deadlines}) == 1
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_duplicate_name_reuses_one_observation_only_within_the_call(
    owned: tuple[Database, OperationOwner],
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier({"worker": _WORKER})

    first = _prepare(owner, carrier)
    second = _prepare(owner, carrier)

    assert first.delivery_result is first.workload_result
    assert second.delivery_result is second.workload_result
    assert carrier.calls == ["worker", "worker"]
    assert not first.requires_owner_retention
    assert not second.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    ("carrier", "failure", "retained"),
    [
        (SyntheticCarrier({"worker": _WORKER}, missing={"root"}), TargetIdentityFailure.ACCOUNT, False),
        (SyntheticCarrier({"worker": _WORKER, "root": _OTHER}), TargetIdentityFailure.IDENTITY_PATH, False),
        (
            SyntheticCarrier({"worker": _WORKER, "root": _ROOT}, uncertain_on_account="root"),
            TargetIdentityFailure.TERMINATION,
            True,
        ),
        (
            SyntheticCarrier({"worker": _WORKER, "root": _ROOT}, expire_on_account="root"),
            TargetIdentityFailure.DEADLINE,
            False,
        ),
    ],
    ids=["missing-root", "non-root-observation", "uncertain-root", "expired-root"],
)
def test_elevated_lookup_failure_keeps_ordinary_evidence_without_prepared_status(
    owned: tuple[Database, OperationOwner],
    carrier: SyntheticCarrier,
    failure: TargetIdentityFailure,
    retained: bool,
) -> None:
    _, owner = owned

    result = _prepare(owner, carrier, include_elevated=True)

    assert result.status is (TargetIdentityStatus.UNCERTAIN if retained else TargetIdentityStatus.FAILED)
    assert result.failure is failure
    assert result.elevated_plan is None
    assert carrier.calls == ["worker", "root"]
    assert len({id(deadline) for deadline in carrier.deadlines}) == 1
    assert result.requires_owner_retention is retained
    if failure is TargetIdentityFailure.DEADLINE:
        assert result.ordinary_plan is None and result.deadline_exceeded
    else:
        assert result.ordinary_plan is not None and result.ordinary_plan.mode is IdentityMode.DIRECT
    if not retained:
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


@pytest.mark.parametrize("include_elevated", [None, 0, "yes", object()])
def test_untyped_elevation_choice_is_refused_before_borrow(
    owned: tuple[Database, OperationOwner],
    include_elevated: object,
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier({"worker": _WORKER})

    with pytest.raises(ValidationError):
        _prepare(owner, carrier, include_elevated=include_elevated)  # type: ignore[arg-type]

    assert carrier.calls == []
    borrow = owner.borrow()
    borrow.close()
    owner.close()


@pytest.mark.parametrize(
    ("delivery_identity", "workload_identity", "include_elevated"),
    [
        (_WORKER, _OTHER, False),
        (_WORKER, _ROOT, False),
        (_WORKER, _OTHER, True),
    ],
)
def test_unproved_cross_identity_paths_are_refused_without_root_lookup(
    owned: tuple[Database, OperationOwner],
    delivery_identity: IdentityExpectation,
    workload_identity: IdentityExpectation,
    include_elevated: bool,
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier({"delivery": delivery_identity, "workload": workload_identity})

    result = _prepare(owner, carrier, delivery="delivery", workload="workload", include_elevated=include_elevated)

    assert result.status is TargetIdentityStatus.FAILED
    assert result.failure is TargetIdentityFailure.IDENTITY_PATH
    assert result.ordinary_plan is None and result.elevated_plan is None
    assert carrier.calls == ["delivery", "workload"]
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    "bad_name",
    ["", "bad\0name", "\ud800", "x" * 32_768],
    ids=["empty", "embedded-nul", "lone-surrogate", "overlong"],
)
def test_all_input_names_are_validated_before_dispatch(
    owned: tuple[Database, OperationOwner],
    bad_name: str,
) -> None:
    database, owner = owned
    carrier = SyntheticCarrier({"worker": _WORKER})

    with pytest.raises(ValidationError) as raised:
        _prepare(owner, carrier, delivery="worker", workload=bad_name)

    assert carrier.calls == []
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.RESERVED
    owner.close()


def test_expired_deadline_refuses_before_borrow_or_dispatch(
    owned: tuple[Database, OperationOwner],
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier({"worker": _WORKER})

    result = _prepare(owner, carrier, deadline=Deadline(0.0))

    assert result.failure is TargetIdentityFailure.DEADLINE
    assert result.deadline_exceeded and carrier.calls == []
    borrow = owner.borrow()
    borrow.close()
    owner.close()


def test_late_normal_completion_settles_attempt_but_cannot_produce_plan(
    owned: tuple[Database, OperationOwner],
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier({"worker": _WORKER}, expire_on_return=True)

    result = _prepare(owner, carrier, deadline=Deadline.after(15))

    assert result.status is TargetIdentityStatus.FAILED
    assert result.failure is TargetIdentityFailure.DEADLINE and result.deadline_exceeded
    assert result.ordinary_plan is None and result.elevated_plan is None
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    ("carrier", "failure"),
    [
        (SyntheticCarrier({"delivery": _WORKER}, missing={"delivery"}), TargetIdentityFailure.ACCOUNT),
        (
            SyntheticCarrier({"delivery": _WORKER}, runtime_missing={"delivery"}),
            TargetIdentityFailure.RUNTIME_PREREQUISITE,
        ),
        (SyntheticCarrier({"delivery": _WORKER}, reflected={"delivery"}), TargetIdentityFailure.OBSERVATION),
        (SyntheticCarrier({"delivery": _WORKER}, malformed={"delivery"}), TargetIdentityFailure.OBSERVATION),
        (SyntheticCarrier({"delivery": _WORKER}, incomplete={"delivery"}), TargetIdentityFailure.OBSERVATION),
    ],
    ids=["missing-account", "runtime", "reflected", "malformed", "incomplete"],
)
def test_closed_lookup_failures_stop_before_the_next_account(
    owned: tuple[Database, OperationOwner],
    carrier: SyntheticCarrier,
    failure: TargetIdentityFailure,
) -> None:
    _, owner = owned

    result = _prepare(owner, carrier, delivery="delivery", workload="workload")

    assert result.status is TargetIdentityStatus.FAILED
    assert result.failure is failure and result.ordinary_plan is None and result.elevated_plan is None
    assert carrier.calls == ["delivery"] and result.workload_result is None
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    ("dispatch", "completion", "failure", "retained"),
    [
        (Dispatch.SENT, ExitStatus(code=7), TargetIdentityFailure.TERMINATION, True),
        (Dispatch.SENT, None, TargetIdentityFailure.TERMINATION, True),
        (Dispatch.UNKNOWN, None, TargetIdentityFailure.TERMINATION, True),
        (Dispatch.NOT_SENT, None, TargetIdentityFailure.DISPATCH, False),
    ],
)
def test_abnormal_and_no_send_completion_facts_are_conservative(
    owned: tuple[Database, OperationOwner],
    dispatch: Dispatch,
    completion: ExitStatus | None,
    failure: TargetIdentityFailure,
    retained: bool,
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier({"worker": _WORKER}, dispatch=dispatch, completion=completion)

    result = _prepare(owner, carrier)

    assert result.failure is failure and result.ordinary_plan is None and result.elevated_plan is None
    assert result.requires_owner_retention is retained
    assert result.pending_remote_effects is retained
    if not retained:
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


@pytest.mark.parametrize(
    ("dispatch", "completion", "failure", "retained"),
    [
        (Dispatch.SENT, ExitStatus(code=7), TargetIdentityFailure.TERMINATION, True),
        (Dispatch.SENT, None, TargetIdentityFailure.TERMINATION, True),
        (Dispatch.UNKNOWN, None, TargetIdentityFailure.TERMINATION, True),
        (Dispatch.NOT_SENT, None, TargetIdentityFailure.DISPATCH, False),
    ],
    ids=["nonzero", "missing-completion", "unknown-dispatch", "not-sent"],
)
def test_expiry_crossed_during_abnormal_exchange_preserves_primary_failure(
    owned: tuple[Database, OperationOwner],
    dispatch: Dispatch,
    completion: ExitStatus | None,
    failure: TargetIdentityFailure,
    retained: bool,
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier(
        {"worker": _WORKER},
        dispatch=dispatch,
        completion=completion,
        expire_on_return=True,
    )

    result = _prepare(owner, carrier, deadline=Deadline.after(15))

    assert result.failure is failure and result.deadline_exceeded
    assert result.ordinary_plan is None and result.elevated_plan is None
    assert result.requires_owner_retention is retained
    assert result.pending_remote_effects is retained
    if not retained:
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


@pytest.mark.parametrize("control", [RuntimeError("ordinary-canary"), ControlStop("base-canary")])
def test_escaping_failures_attach_safe_retained_facts(
    owned: tuple[Database, OperationOwner],
    control: BaseException,
) -> None:
    _, owner = owned
    carrier = RaisingCarrier(control)

    with pytest.raises(type(control)) as raised:
        _prepare(owner, carrier)

    assert raised.value is control
    fact = raised.value.__cause__
    assert isinstance(fact, TargetIdentityControlFact)
    assert fact.preparation.status is TargetIdentityStatus.UNCERTAIN
    assert fact.preparation.pending_remote_effects
    assert fact.preparation.requires_owner_retention
    assert "ordinary-canary" not in repr(fact) and "base-canary" not in repr(fact)


@pytest.mark.parametrize("transition_committed", [False, True], ids=["before-commit", "after-commit"])
def test_database_arm_failure_prevents_dispatch_and_exports_coordination_uncertainty(
    owned: tuple[Database, OperationOwner],
    monkeypatch: pytest.MonkeyPatch,
    transition_committed: bool,
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier({"worker": _WORKER})
    original = owner._repository.mark_lifecycle_obligation_possible_effect  # noqa: SLF001

    def fail_arm(ownership, obligation_id) -> None:
        if transition_committed:
            original(ownership, obligation_id)
        raise RuntimeError("database-arm-canary")

    monkeypatch.setattr(owner._repository, "mark_lifecycle_obligation_possible_effect", fail_arm)  # noqa: SLF001

    with pytest.raises(RuntimeError, match="database-arm-canary") as raised:
        _prepare(owner, carrier)

    fact = raised.value.__cause__
    assert isinstance(fact, TargetIdentityControlFact)
    assert fact.preparation.coordination_uncertain
    assert fact.preparation.requires_owner_retention
    assert carrier.calls == []


def test_attempt_settlement_failure_retains_recorded_account_result(
    owned: tuple[Database, OperationOwner],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner = owned
    carrier = SyntheticCarrier({"worker": _WORKER})

    def fail_settle(_attempt: OperationAttempt) -> None:
        raise RuntimeError("settlement-canary")

    monkeypatch.setattr(OperationAttempt, "settle", fail_settle)

    with pytest.raises(RuntimeError, match="settlement-canary") as raised:
        _prepare(owner, carrier)

    fact = raised.value.__cause__
    assert isinstance(fact, TargetIdentityControlFact)
    assert fact.preparation.delivery_result is not None
    assert fact.preparation.coordination_uncertain
    assert fact.preparation.requires_owner_retention


def test_safe_failure_relinquishes_borrow_while_uncertain_attempt_retains_claim(
    owned: tuple[Database, OperationOwner],
) -> None:
    _, owner = owned
    safe = SyntheticCarrier({"delivery": _WORKER}, missing={"delivery"})

    result = _prepare(owner, safe, delivery="delivery", workload="workload")

    assert not result.requires_owner_retention
    borrow = owner.borrow()
    borrow.close()

    uncertain = SyntheticCarrier({"worker": _WORKER}, completion=None)
    retained = _prepare(owner, uncertain)
    assert retained.requires_owner_retention
    with pytest.raises(StateError):
        owner.borrow()


def test_default_representations_do_not_disclose_account_names(
    owned: tuple[Database, OperationOwner],
) -> None:
    _, owner = owned
    delivery = "delivery-account-canary"
    workload = "workload-account-canary"
    carrier = SyntheticCarrier({delivery: _WORKER}, missing={delivery})

    result = _prepare(owner, carrier, delivery=delivery, workload=workload)

    assert delivery not in repr(result) and workload not in repr(result)
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
