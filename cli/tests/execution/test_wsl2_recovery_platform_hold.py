"""SQLite custody boundaries for renewed WSL2 recovery availability."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._wsl2_guest_observer import WSL2GuestObserver
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence
from agentworks.execution._wsl2_platform_hold import OBLIGATION_KIND, WSL2PlatformHold, decode_hold_payload
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection
from agentworks.operations import OperationOwner, RecoveryAttempt, RecoveryDispatch
from tests.execution._bound_carrier_support import obligation_receipt
from tests.execution.test_wsl2_platform_hold import (
    GUEST,
    FakeNative,
    FakeObserver,
    ObservedTransitionLock,
    PausedNative,
    QueryFactory,
)

pytestmark = pytest.mark.windows
SUPPORT_ID = "d" * 32
CONNECTION = WSL2Connection("Ubuntu", "body-user", "wsl.exe")


@pytest.mark.parametrize("recovery", [False, True])
def test_true_active_cap_refuses_hold_before_native_launch_without_phantom_custody(
    db: Database, recovery: bool
) -> None:
    owner = OperationOwner.acquire(db.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "capacity")
    debts = [owner.register_lifecycle_obligation("pending", payload_version=1, payload=b"owned") for _ in range(128)]
    if recovery:
        owner = OperationOwner.recover(db.operations, owner.ownership, "b" * 32)
    native = FakeNative([])
    subject = hold(owner, native)
    with pytest.raises(StateError):
        if recovery:
            subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
        else:
            subject.start(Deadline.after(1))
    assert not subject.registration_uncertain and subject.obligation is None
    assert native.events == ["controller"]
    subject.release(Deadline.after(1))
    assert len(owner.list_pending_lifecycle_obligations()) == 128
    for debt in debts:
        owner._repository.resolve_lifecycle_obligation(owner.ownership, debt.obligation_id)  # noqa: SLF001
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()
    assert db.operations.inspect(owner.ownership.scope) is None


def recovery_owner(db: Database) -> OperationOwner:
    predecessor = OperationOwner.acquire(db.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
    predecessor.register_lifecycle_obligation(
        "old-registered", payload_version=1, payload=b"registered", obligation_id="a" * 31 + "1"
    )
    old = predecessor.register_lifecycle_obligation(
        "old-file", payload_version=1, payload=b"file", obligation_id="a" * 31 + "2"
    )
    old.mark_possible_effect()
    return OperationOwner.recover(db.operations, predecessor.ownership, "b" * 32)


def hold(owner: OperationOwner, native: FakeNative, observer: FakeObserver | WSL2GuestObserver | None = None):
    return WSL2PlatformHold(
        owner,
        "vm-one",
        "opaque-locator",
        "c" * 32,
        CONNECTION,
        native,
        observer if observer is not None else FakeObserver(native.events),
    )


def test_support_admission_precedes_launch_and_cleanup_preserves_old_debts(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = recovery_owner(db)
    old = owner.list_pending_lifecycle_obligations()
    native = FakeNative([])
    spawn = native.spawn_owned

    def check_admission(argv: tuple[str, ...], deadline: Deadline) -> None:
        rows = owner.list_pending_lifecycle_obligations()
        assert rows[:2] == old
        support = next(row for row in rows if row.obligation_id == SUPPORT_ID)
        assert support.state is LifecycleObligationState.POSSIBLE_EFFECT and support.payload_revision == 0
        payload = decode_hold_payload(support.payload)
        assert payload.guest is None and payload.user == "body-user" and payload.launch_user == "root"
        assert payload.controller.pid == 42 and payload.controller.creation_ticks == 123456789
        with pytest.raises(StateError):
            owner.record_effects_resolved()
        spawn(argv, deadline)

    monkeypatch.setattr(native, "spawn_owned", check_admission)
    subject = hold(owner, native)
    subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    assert native.events == ["controller", "dispatch"]
    assert native.argv[:5] == ("wsl.exe", "--distribution", "Ubuntu", "--user", "root")
    assert subject.payload is not None and subject.payload.guest == GUEST
    assert subject.obligation is not None and subject.obligation.obligation_id == SUPPORT_ID
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.register_lifecycle_obligation("ordinary", payload_version=1, payload=b"")

    # A live hold permits a distinct finite recovery attempt under an old debt.
    row = old[1]
    dispatch = owner.rebind_possible_effect_lifecycle_obligation(
        row.obligation_id,
        row.obligation_kind,
        payload_version=row.payload_version,
        payload=row.payload,
        payload_revision=row.payload_revision,
    ).open_dispatch()
    attempt = dispatch.begin_attempt()
    attempt.settle()
    dispatch.close()
    subject.release(Deadline.after(1))
    rows = owner.list_pending_lifecycle_obligations()
    assert rows[:2] == old
    assert obligation_receipt(owner, SUPPORT_ID).state is LifecycleObligationState.RESOLVED
    with pytest.raises(StateError):
        owner.record_effects_resolved()
    assert db.operations.inspect(owner.ownership.scope) is not None


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_lost_admission_retains_identity_without_launch_replay(
    db: Database, monkeypatch: pytest.MonkeyPatch, committed: bool, error_type: type[BaseException]
) -> None:
    owner = recovery_owner(db)
    before = owner.list_pending_lifecycle_obligations()
    repository = owner._repository  # noqa: SLF001
    admit = repository.admit_recovery_support_obligation

    def lost_reply(*args, **kwargs):
        if committed:
            admit(*args, **kwargs)
        raise error_type()

    native = FakeNative([])
    subject = hold(owner, native)
    with monkeypatch.context() as patch:
        patch.setattr(repository, "admit_recovery_support_obligation", lost_reply)
        with pytest.raises(error_type):
            subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    assert subject.registration_uncertain is (committed or error_type is KeyboardInterrupt)
    assert subject.obligation is None
    assert subject.payload is not None
    assert native.events == ["controller"]
    with pytest.raises(ValidationError):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    with pytest.raises(ValidationError):
        subject.start(Deadline.after(1))
    subject.release(Deadline.after(1))
    rows = owner.list_pending_lifecycle_obligations()
    assert rows[:2] == before and len(rows) == 2 + int(committed)
    if committed:
        support = rows[2]
        confirmed = owner.admit_recovery_support_obligation(
            OBLIGATION_KIND, payload_version=4, payload=support.payload, obligation_id=SUPPORT_ID
        )
        assert confirmed.obligation_id == SUPPORT_ID
        assert support.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert "dispatch" not in native.events


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_lost_ready_reply_retains_anchor_and_does_not_replay(
    db: Database, monkeypatch: pytest.MonkeyPatch, committed: bool, error_type: type[BaseException]
) -> None:
    owner = recovery_owner(db)
    before = owner.list_pending_lifecycle_obligations()
    repository = owner._repository  # noqa: SLF001
    publish = repository.publish_lifecycle_obligation_payload

    def lost_reply(*args, **kwargs):
        if committed:
            publish(*args, **kwargs)
        raise error_type()

    native = FakeNative([])
    subject = hold(owner, native)
    with monkeypatch.context() as patch:
        patch.setattr(repository, "publish_lifecycle_obligation_payload", lost_reply)
        with pytest.raises(error_type):
            subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    assert subject.payload is not None and subject.payload.guest == GUEST
    assert subject.evidence.identity == GUEST
    with pytest.raises(ValidationError):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    if committed:
        with pytest.raises(StateError):
            subject.release(Deadline.after(1))
        assert "observe" not in native.events
    else:
        subject.release(Deadline.after(1))
    rows = owner.list_pending_lifecycle_obligations()
    assert rows[:2] == before
    assert obligation_receipt(owner, SUPPORT_ID).state is (
        LifecycleObligationState.POSSIBLE_EFFECT if committed else LifecycleObligationState.RESOLVED
    )
    assert native.events.count("dispatch") == 1


@pytest.mark.parametrize("never_created", [False, True])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_launch_failure_resolves_only_positive_noncreation(
    db: Database, never_created: bool, error_type: type[BaseException]
) -> None:
    owner = recovery_owner(db)
    before = owner.list_pending_lifecycle_obligations()
    native = FakeNative([], fail_spawn=error_type(), never_created=never_created)
    subject = hold(owner, native)
    with pytest.raises(error_type):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    with pytest.raises(ValidationError):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    subject.release(Deadline.after(1))
    rows = owner.list_pending_lifecycle_obligations()
    assert rows[:2] == before
    assert obligation_receipt(owner, SUPPORT_ID).state is (
        LifecycleObligationState.RESOLVED if never_created else LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert native.events.count("dispatch") == 1 and "observe" not in native.events


def test_interrupted_query_retains_support_across_another_takeover(db: Database) -> None:
    owner = recovery_owner(db)
    native = FakeNative([])
    query_factory = QueryFactory(native.events)
    subject = hold(owner, native, WSL2GuestObserver(CONNECTION, client_factory=query_factory))
    subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    with pytest.raises(KeyboardInterrupt):
        subject.release(Deadline.after(1))
    subject.release(Deadline.after(1))
    assert len(query_factory.clients) == 1 and query_factory.clients[0].settlement_attempts == 2
    before = owner.list_pending_lifecycle_obligations()
    support = before[2]
    assert support.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert decode_hold_payload(support.payload).query_may_have_been_admitted
    successor = OperationOwner.recover(db.operations, owner.ownership, "e" * 32)
    assert successor.list_pending_lifecycle_obligations() == tuple(
        replace(row, ownership=successor.ownership) for row in before
    )
    with pytest.raises(StateError):
        successor.record_effects_resolved()


def test_takeover_after_admission_allows_admitted_launch_but_retains_debt(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = recovery_owner(db)
    old = owner.list_pending_lifecycle_obligations()
    repository = owner._repository  # noqa: SLF001
    admit = repository.admit_recovery_support_obligation
    successors: list[OperationOwner] = []

    def take_over(*args, **kwargs):
        result = admit(*args, **kwargs)
        successors.append(OperationOwner.recover(db.operations, owner.ownership, "e" * 32))
        return result

    native = FakeNative([])
    subject = hold(owner, native)
    monkeypatch.setattr(repository, "admit_recovery_support_obligation", take_over)
    with pytest.raises(StateError):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    successor = successors[0]
    rows = successor.list_pending_lifecycle_obligations()
    assert rows[:2] == tuple(replace(row, ownership=successor.ownership) for row in old)
    assert rows[2].obligation_id == SUPPORT_ID and rows[2].state is LifecycleObligationState.POSSIBLE_EFFECT
    assert decode_hold_payload(rows[2].payload).guest is None
    assert subject.evidence.identity == GUEST and native.events.count("dispatch") == 1
    with pytest.raises(StateError):
        subject.release(Deadline.after(1))
    with pytest.raises(StateError):
        owner.record_effects_resolved()
    with pytest.raises(StateError):
        successor.record_effects_resolved()
    assert successor.list_pending_lifecycle_obligations() == rows


@pytest.mark.parametrize("invalid", ["scope", "infinite", "expired", "id"])
def test_invalid_target_deadline_or_id_refuses_before_native_work(db: Database, invalid: str) -> None:
    owner = recovery_owner(db)
    before = owner.list_pending_lifecycle_obligations()
    native = FakeNative([])
    subject = hold(owner, native)
    if invalid == "scope":
        subject = WSL2PlatformHold(owner, "other-vm", "opaque-locator", "c" * 32, CONNECTION, native)
    deadline = Deadline(None) if invalid == "infinite" else Deadline.after(0 if invalid == "expired" else 1)
    with pytest.raises(ValidationError):
        subject.start_recovery(deadline, obligation_id="invalid" if invalid == "id" else SUPPORT_ID)
    assert not native.events and owner.list_pending_lifecycle_obligations() == before


def test_recovery_entry_requires_recovery_owner_and_normal_entry_stays_sealed(db: Database) -> None:
    normal = OperationOwner.acquire(db.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
    native = FakeNative([])
    with pytest.raises(StateError):
        hold(normal, native).start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    assert normal.list_pending_lifecycle_obligations() == () and "dispatch" not in native.events
    owner = OperationOwner.recover(db.operations, normal.ownership, "b" * 32)
    with pytest.raises(StateError):
        hold(owner, native).start(Deadline.after(1))
    assert owner.list_pending_lifecycle_obligations() == () and "dispatch" not in native.events


def test_paused_start_excludes_early_release_and_second_start_under_deadlines(db: Database) -> None:
    owner = recovery_owner(db)
    before = owner.list_pending_lifecycle_obligations()
    native = PausedNative([])
    subject = hold(owner, native)
    transition_lock = ObservedTransitionLock()
    subject._transition_lock = transition_lock  # type: ignore[assignment]  # noqa: SLF001
    errors: list[BaseException] = []

    def start() -> None:
        try:
            subject.start_recovery(Deadline.after(10), obligation_id=SUPPORT_ID)
        except BaseException as error:
            errors.append(error)

    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert native.entered.wait(5)
        during = owner.list_pending_lifecycle_obligations()
        assert during[:2] == before and during[2].state is LifecycleObligationState.POSSIBLE_EFFECT
        with pytest.raises(StateError):
            owner.record_effects_resolved()
        with pytest.raises(TimeoutError):
            subject.release(Deadline.after(0.05))
        with pytest.raises(TimeoutError):
            subject.start_recovery(Deadline.after(0.05), obligation_id="f" * 32)
        assert transition_lock.contended.is_set()
        assert owner.list_pending_lifecycle_obligations() == during and "observe" not in native.events
    finally:
        native.proceed.set()
        starter.join(timeout=10)
    assert not starter.is_alive() and not errors
    with pytest.raises(ValidationError):
        subject.start(Deadline.after(1))
    subject.release(Deadline.after(1))
    rows = owner.list_pending_lifecycle_obligations()
    assert rows == before
    assert obligation_receipt(owner, SUPPORT_ID).state is LifecycleObligationState.RESOLVED
    assert native.events.count("dispatch") == 1


def test_unknown_guest_absence_retains_support(db: Database) -> None:
    owner = recovery_owner(db)
    native = FakeNative([])
    subject = hold(owner, native, FakeObserver(native.events, GuestAnchorPresence.UNKNOWN))
    subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    subject.release(Deadline.after(1))
    assert obligation_receipt(owner, SUPPORT_ID).state is LifecycleObligationState.POSSIBLE_EFFECT


def test_ready_identity_is_published_after_startup_interrupt(db: Database) -> None:
    owner = recovery_owner(db)
    native = FakeNative([], snapshot_failure_at=3, snapshot_error=KeyboardInterrupt())
    subject = hold(owner, native)
    with pytest.raises(KeyboardInterrupt):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    support = obligation_receipt(owner, SUPPORT_ID)
    assert support.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert decode_hold_payload(support.payload).guest == GUEST
    with pytest.raises(ValidationError):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    subject.release(Deadline.after(1))
    assert obligation_receipt(owner, SUPPORT_ID).state is LifecycleObligationState.RESOLVED
    assert native.events.count("dispatch") == 1


def test_sibling_dispatch_refuses_ready_publication_without_erasing_support(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = recovery_owner(db)
    old = owner.list_pending_lifecycle_obligations()[1]
    native = FakeNative([])
    read = native.read_stdout_line
    active: list[tuple[RecoveryDispatch, RecoveryAttempt]] = []

    def concurrent_dispatch(limit: int, deadline: Deadline) -> bytes:
        result = read(limit, deadline)
        if limit == 513 and not active:
            dispatch = owner.rebind_possible_effect_lifecycle_obligation(
                old.obligation_id,
                old.obligation_kind,
                payload_version=old.payload_version,
                payload=old.payload,
                payload_revision=old.payload_revision,
            ).open_dispatch()
            active.append((dispatch, dispatch.begin_attempt()))
        return result

    monkeypatch.setattr(native, "read_stdout_line", concurrent_dispatch)
    subject = hold(owner, native)
    with pytest.raises(StateError):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    assert subject.evidence.identity == GUEST
    support = obligation_receipt(owner, SUPPORT_ID)
    assert support.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert decode_hold_payload(support.payload).guest is None
    with pytest.raises(StateError):
        owner.record_effects_resolved()
    dispatch, attempt = active[0]
    # No concrete sibling work was dispatched, so this test attempt is terminal.
    attempt.settle()
    dispatch.close()
    with pytest.raises(ValidationError):
        subject.start_recovery(Deadline.after(1), obligation_id=SUPPORT_ID)
    subject.release(Deadline.after(1))
    assert obligation_receipt(owner, SUPPORT_ID).state is LifecycleObligationState.RESOLVED
    assert native.events.count("dispatch") == 1
