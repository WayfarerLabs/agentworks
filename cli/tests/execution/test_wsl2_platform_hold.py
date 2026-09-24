"""Behavioral boundaries for the private WSL2 ledger hold."""

from __future__ import annotations

import json
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationOwnership, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution import _wsl2_platform_hold as hold_module
from agentworks.execution._wsl2_lifecycle import (
    GuestAnchorIdentity,
    GuestAnchorPresence,
    HandleSettlement,
    HostClientStatus,
    JobAssignment,
    LocalResourceSnapshot,
)
from agentworks.execution._wsl2_platform_hold import (
    OBLIGATION_KIND,
    WSL2PlatformHold,
    decode_hold_payload,
    encode_hold_payload,
    locator_digest,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection
from agentworks.operations import OperationOwner

BOOT = "12345678-1234-1234-1234-123456789abc"
GUEST = GuestAnchorIdentity(BOOT, 137, 8192)
OPEN = LocalResourceSnapshot(
    HostClientStatus.ACTIVE, None, JobAssignment.ASSIGNED_AT_CREATION, HandleSettlement.OPEN, HandleSettlement.OPEN
)
SETTLED = LocalResourceSnapshot(
    HostClientStatus.EXITED, 0, JobAssignment.ASSIGNED_AT_CREATION, HandleSettlement.CLOSED, HandleSettlement.CLOSED
)
NEVER = LocalResourceSnapshot(
    HostClientStatus.NOT_CREATED,
    None,
    JobAssignment.NOT_CREATED,
    HandleSettlement.NOT_CREATED,
    HandleSettlement.NOT_CREATED,
)


@dataclass
class FakeObligation:
    events: list[str]
    fail_at: str | None = None
    payload_revision: int = 0
    payload: bytes = b""
    resolved: bool = False

    def mark_possible_effect(self) -> None:
        self.events.append("mark")
        if self.fail_at == "mark":
            raise OSError("lost mark reply")

    def publish_payload(self, *, expected_revision: int, payload_version: int, payload: bytes) -> None:
        assert expected_revision == 0 and payload_version == 1
        self.events.append("publish")
        self.payload = payload
        if self.fail_at == "publish":
            raise OSError("lost publication reply")
        self.payload_revision = 1

    def resolve(self) -> None:
        self.events.append("resolve")
        if self.fail_at == "resolve":
            raise OSError("lost resolution reply")
        self.resolved = True


@dataclass
class FakeOwner:
    events: list[str] = field(default_factory=list)
    fail_at: str | None = None
    obligations: list[FakeObligation] = field(default_factory=list)
    ownership: OperationOwnership = field(
        default_factory=lambda: OperationOwnership(
            OperationScope(OperationResourceKind.VM, "vm-one"), "a" * 32, "b" * 32
        )
    )

    def register_lifecycle_obligation(self, kind: str, *, payload_version: int, payload: bytes) -> FakeObligation:
        assert kind == OBLIGATION_KIND and payload_version == 1
        self.events.append("register")
        obligation = FakeObligation(self.events, self.fail_at, payload=payload)
        self.obligations.append(obligation)
        if self.fail_at == "register":
            raise OSError("lost registration reply")
        return obligation

    def borrow(self) -> str:
        self.events.append("borrow")
        return "borrowed"


@dataclass
class FakeNative:
    events: list[str]
    local: LocalResourceSnapshot = NEVER
    fail_spawn: BaseException | None = None
    fail_identity: BaseException | None = None
    never_created: bool = False
    nonce: str = ""
    argv: tuple[str, ...] = ()
    stdout_reads: int = 0
    snapshot_calls: int = 0
    snapshot_failure_at: int | None = None
    snapshot_error: BaseException | None = None

    def current_controller_identity(self) -> tuple[int, int]:
        self.events.append("controller")
        if self.fail_identity:
            raise self.fail_identity
        return 42, 123456789

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        assert not deadline.expired
        self.events.append("dispatch")
        self.argv = argv
        self.nonce = argv[-1]
        self.local = NEVER if self.never_created else OPEN
        if self.fail_spawn:
            raise self.fail_spawn

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        assert limit == 513 and not deadline.expired
        self.stdout_reads += 1
        if self.stdout_reads == 1:
            return f"READY {self.nonce} {BOOT} 137 8192\n".encode()
        return f"EXITING {self.nonce}\n".encode()

    def close_stdin(self) -> None:
        self.events.append("eof")

    def wait(self, deadline: Deadline) -> int | None:
        self.local = SETTLED
        return 0

    def snapshot(self) -> LocalResourceSnapshot:
        self.snapshot_calls += 1
        if self.snapshot_calls == self.snapshot_failure_at:
            assert self.snapshot_error is not None
            raise self.snapshot_error
        return self.local

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot:
        self.events.append("settle")
        if not self.never_created:
            self.local = SETTLED
        return self.local


@dataclass
class PausedNative(FakeNative):
    entered: threading.Event = field(default_factory=threading.Event)
    proceed: threading.Event = field(default_factory=threading.Event)

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        self.entered.set()
        if not self.proceed.wait(10):
            raise TimeoutError("test dispatch gate was not released")
        super().spawn_owned(argv, deadline)


class ObservedTransitionLock:
    """Signal a second acquire attempt while retaining real lock behavior."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def __enter__(self) -> None:
        if self._lock.locked():
            self.contended.set()
        self._lock.acquire()

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._lock.release()


@dataclass
class FakeObserver:
    events: list[str]
    presence: GuestAnchorPresence = GuestAnchorPresence.ABSENT_CONFIRMED

    def observe(self, identity: GuestAnchorIdentity, deadline: Deadline) -> GuestAnchorPresence:
        assert identity == GUEST and not deadline.expired
        self.events.append("observe")
        return self.presence


def hold(
    owner: FakeOwner,
    *,
    native: FakeNative | None = None,
    observer: FakeObserver | None = None,
    vm_name: str = "vm-one",
    locator: str = "opaque-locator",
    connection: WSL2Connection | None = None,
) -> WSL2PlatformHold:
    native = native or FakeNative(owner.events)
    observer = observer or FakeObserver(owner.events)
    connection = connection or WSL2Connection("Ubuntu", "root", "wsl.exe")
    return WSL2PlatformHold(
        owner,  # type: ignore[arg-type]
        vm_name,
        locator,
        "c" * 32,
        connection,
        native,
        observer,
    )


def test_registration_order_exact_connection_and_release() -> None:
    owner = FakeOwner()
    native = FakeNative(owner.events)
    subject = hold(owner, native=native)
    subject.start(Deadline.after(1))
    assert owner.events == ["controller", "register", "mark", "dispatch", "publish"]
    assert native.argv[2:5] == ("Ubuntu", "--user", "root")
    assert subject.payload is not None and subject.payload.guest == GUEST
    assert decode_hold_payload(owner.obligations[0].payload) == subject.payload
    assert owner.borrow() == "borrowed"
    subject.release(Deadline.after(1))
    assert owner.events[-2:] == ["observe", "resolve"]
    assert owner.obligations[0].resolved


def test_nested_holds_are_independent() -> None:
    owner = FakeOwner()
    first, second = hold(owner), hold(owner)
    first.start(Deadline.after(1))
    second.start(Deadline.after(1))
    assert len(owner.obligations) == 2
    assert (
        decode_hold_payload(owner.obligations[0].payload).nonce
        != decode_hold_payload(owner.obligations[1].payload).nonce
    )
    first.release(Deadline.after(1))
    assert owner.obligations[0].resolved and not owner.obligations[1].resolved


def test_real_owner_persists_independent_rows_and_remains_borrowable(tmp_path: Path) -> None:
    with closing(Database(tmp_path / "hold.db")) as database:
        owner = OperationOwner.acquire(
            database.operations,
            OperationScope(OperationResourceKind.VM, "vm-one"),
            "wsl2-private-proof",
        )
        events: list[str] = []
        first = WSL2PlatformHold(
            owner,
            "vm-one",
            "opaque-locator",
            "c" * 32,
            WSL2Connection("Ubuntu", "root", "wsl.exe"),
            FakeNative(events),
            FakeObserver(events),
        )
        second = WSL2PlatformHold(
            owner,
            "vm-one",
            "opaque-locator",
            "c" * 32,
            WSL2Connection("Ubuntu", "root", "wsl.exe"),
            FakeNative(events),
            FakeObserver(events),
        )
        first.start(Deadline.after(1))
        second.start(Deadline.after(1))
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 2
        assert len({row.obligation_id for row in rows}) == 2
        assert {row.obligation_kind for row in rows} == {OBLIGATION_KIND}
        assert all(row.state is LifecycleObligationState.POSSIBLE_EFFECT and row.payload_revision == 1 for row in rows)
        assert {decode_hold_payload(row.payload).nonce for row in rows} == {first.payload.nonce, second.payload.nonce}  # type: ignore[union-attr]
        borrow = owner.borrow()
        borrow.close()
        first.release(Deadline.after(1))
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert [row.state for row in rows].count(LifecycleObligationState.RESOLVED) == 1
        assert [row.state for row in rows].count(LifecycleObligationState.POSSIBLE_EFFECT) == 1
        borrow = owner.borrow()
        borrow.close()
        second.release(Deadline.after(1))
        assert all(
            row.state is LifecycleObligationState.RESOLVED
            for row in database.operations.list_lifecycle_obligations(owner.ownership)
        )


def test_release_waits_for_start_after_mark_before_ready(tmp_path: Path) -> None:
    with closing(Database(tmp_path / "hold.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
        events: list[str] = []
        native = PausedNative(events)
        subject = WSL2PlatformHold(
            owner,
            "vm-one",
            "opaque-locator",
            "c" * 32,
            WSL2Connection("Ubuntu", "root", "wsl.exe"),
            native,
            FakeObserver(events),
        )
        transition_lock = ObservedTransitionLock()
        subject._transition_lock = transition_lock  # type: ignore[assignment]  # noqa: SLF001
        errors: list[BaseException] = []

        def start() -> None:
            try:
                subject.start(Deadline.after(10))
            except BaseException as error:
                errors.append(error)

        def release() -> None:
            try:
                subject.release(Deadline.after(10))
            except BaseException as error:
                errors.append(error)

        starter = threading.Thread(target=start)
        releaser = threading.Thread(target=release)
        starter.start()
        try:
            assert native.entered.wait(5)
            rows = database.operations.list_lifecycle_obligations(owner.ownership)
            assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT
            assert rows[0].payload_revision == 0
            releaser.start()
            assert transition_lock.contended.wait(5)
            rows = database.operations.list_lifecycle_obligations(owner.ownership)
            assert rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT
        finally:
            native.proceed.set()
            starter.join(timeout=10)
            if releaser.ident is not None:
                releaser.join(timeout=10)
        assert not starter.is_alive() and not releaser.is_alive() and not errors
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1 and rows[0].state is LifecycleObligationState.RESOLVED
        assert events.index("dispatch") < events.index("observe")


def test_concurrent_start_registers_and_dispatches_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(Database(tmp_path / "hold.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "proof")
        events: list[str] = []
        native = FakeNative(events)
        subject = WSL2PlatformHold(
            owner,
            "vm-one",
            "opaque-locator",
            "c" * 32,
            WSL2Connection("Ubuntu", "root", "wsl.exe"),
            native,
            FakeObserver(events),
        )
        transition_lock = ObservedTransitionLock()
        subject._transition_lock = transition_lock  # type: ignore[assignment]  # noqa: SLF001
        digest_entered = threading.Event()
        digest_continue = threading.Event()
        original_digest = hold_module.locator_digest

        def paused_digest(locator: str) -> str:
            if threading.current_thread().name == "first-start":
                digest_entered.set()
                if not digest_continue.wait(10):
                    raise TimeoutError("test preparation gate was not released")
            return original_digest(locator)

        monkeypatch.setattr(hold_module, "locator_digest", paused_digest)
        results: list[BaseException | None] = []

        def run() -> None:
            try:
                subject.start(Deadline.after(10))
                results.append(None)
            except BaseException as error:
                results.append(error)

        first = threading.Thread(target=run, name="first-start")
        second = threading.Thread(target=run, name="second-start")
        first.start()
        try:
            assert digest_entered.wait(5)
            second.start()
            assert transition_lock.contended.wait(5)
        finally:
            digest_continue.set()
            first.join(timeout=10)
            if second.ident is not None:
                second.join(timeout=10)
        assert not first.is_alive() and not second.is_alive()
        assert len(results) == 2 and results.count(None) == 1
        assert sum(isinstance(result, ValidationError) for result in results) == 1
        assert events.count("dispatch") == 1
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("vm_name", ["other", ""])
def test_wrong_scope_refuses_before_native_work(vm_name: str) -> None:
    owner = FakeOwner()
    with pytest.raises(ValidationError):
        hold(owner, vm_name=vm_name).start(Deadline.after(1))
    assert owner.events == []


def test_deadline_and_oversize_refuse_before_native_work() -> None:
    owner = FakeOwner()
    with pytest.raises(ValidationError):
        hold(owner).start(Deadline.after(None))
    with pytest.raises(ValidationError):
        hold(owner, connection=WSL2Connection("a" * 9000, "root", "wsl.exe")).start(Deadline.after(1))
    with pytest.raises(ValidationError):
        hold(owner, connection=WSL2Connection("\ud800", "root", "wsl.exe")).start(Deadline.after(1))
    assert owner.events == []


@pytest.mark.parametrize("executable", [r"C:\Windows\System32\wsl.exe", "./wsl.exe", "other"])
def test_hold_refuses_untrusted_wsl_executable_before_native_work(executable: str) -> None:
    owner = FakeOwner()
    with pytest.raises(ValidationError):
        hold(owner, connection=WSL2Connection("Ubuntu", "root", executable)).start(Deadline.after(1))
    assert owner.events == []


@pytest.mark.parametrize("executable", ["wsl", "wsl.exe", "WSL.EXE"])
def test_hold_accepts_bare_wsl_executable_case_insensitively(executable: str) -> None:
    owner = FakeOwner()
    native = FakeNative(owner.events)
    hold(owner, native=native, connection=WSL2Connection("Ubuntu", "root", executable)).start(Deadline.after(1))
    assert native.argv[0] == executable
    assert owner.events.count("dispatch") == 1


@pytest.mark.parametrize("phase", ["register", "mark", "publish", "resolve"])
def test_lost_ledger_reply_keeps_state_without_replay(phase: str) -> None:
    owner = FakeOwner(fail_at=phase)
    subject = hold(owner)
    if phase == "resolve":
        subject.start(Deadline.after(1))
        with pytest.raises(OSError):
            subject.release(Deadline.after(1))
        assert owner.events.count("dispatch") == 1
        return
    with pytest.raises(OSError):
        subject.start(Deadline.after(1))
    with pytest.raises(ValidationError):
        subject.start(Deadline.after(1))
    assert owner.events.count("dispatch") == (1 if phase == "publish" else 0)
    if phase == "register":
        assert subject.registration_uncertain and subject.obligation is None
    else:
        assert subject.obligation is not None
    if phase == "mark":
        subject.release(Deadline.after(1))
        assert owner.obligations[0].resolved
    elif phase == "publish":
        subject.release(Deadline.after(1))
        assert owner.obligations[0].resolved
        assert owner.events.count("dispatch") == 1
    else:
        subject.release(Deadline.after(1))
        assert not owner.obligations[0].resolved


def test_start_interrupt_retains_object_and_never_replays() -> None:
    owner = FakeOwner()
    native = FakeNative(owner.events, fail_spawn=KeyboardInterrupt())
    subject = hold(owner, native=native)
    with pytest.raises(KeyboardInterrupt):
        subject.start(Deadline.after(1))
    assert subject.obligation is not None and owner.events.count("dispatch") == 1
    with pytest.raises(ValidationError):
        subject.start(Deadline.after(1))


def test_post_ready_start_failure_still_publishes_guest_identity() -> None:
    owner = FakeOwner()
    original = OSError("post-READY snapshot failed")
    native = FakeNative(owner.events, snapshot_failure_at=3, snapshot_error=original)
    subject = hold(owner, native=native)
    with pytest.raises(OSError) as caught:
        subject.start(Deadline.after(1))
    assert caught.value is original
    assert subject.payload is not None and subject.payload.guest == GUEST
    assert decode_hold_payload(owner.obligations[0].payload).guest == GUEST
    assert owner.events.count("dispatch") == 1
    assert owner.events.count("publish") == 1
    subject.release(Deadline.after(1))
    assert owner.obligations[0].resolved


def test_post_ready_start_and_publication_failure_keep_original_control() -> None:
    owner = FakeOwner(fail_at="publish")
    original = KeyboardInterrupt()
    native = FakeNative(owner.events, snapshot_failure_at=3, snapshot_error=original)
    subject = hold(owner, native=native)
    with pytest.raises(KeyboardInterrupt) as caught:
        subject.start(Deadline.after(1))
    assert caught.value is original
    assert caught.value.__notes__ == ["WSL2 hold READY identity publication is uncertain"]
    assert subject.payload is not None and subject.payload.guest == GUEST
    assert subject.obligation is not None
    assert owner.events.count("dispatch") == 1
    subject.release(Deadline.after(1))
    assert owner.obligations[0].resolved


def test_controller_observation_failure_prevents_registration_and_dispatch() -> None:
    owner = FakeOwner()
    native = FakeNative(owner.events, fail_identity=OSError("GetProcessTimes failed"))
    subject = hold(owner, native=native)
    with pytest.raises(OSError):
        subject.start(Deadline.after(1))
    assert owner.events == ["controller"]
    assert subject.obligation is None


@pytest.mark.parametrize("presence", [GuestAnchorPresence.PRESENT, GuestAnchorPresence.UNKNOWN])
def test_guest_uncertainty_retains_obligation(presence: GuestAnchorPresence) -> None:
    owner = FakeOwner()
    subject = hold(owner, observer=FakeObserver(owner.events, presence))
    subject.start(Deadline.after(1))
    subject.release(Deadline.after(1))
    assert not owner.obligations[0].resolved


def test_never_created_resolves_without_ready() -> None:
    owner = FakeOwner()
    native = FakeNative(owner.events, fail_spawn=OSError("no client"), never_created=True)
    subject = hold(owner, native=native)
    with pytest.raises(OSError):
        subject.start(Deadline.after(1))
    subject.release(Deadline.after(1))
    assert owner.obligations[0].resolved


def test_canonical_payload_rejects_malformed_boundary() -> None:
    owner = FakeOwner()
    subject = hold(owner)
    subject.start(Deadline.after(1))
    payload = subject.payload
    assert payload is not None
    encoded = encode_hold_payload(payload)
    assert len(encoded) < 8192 and b"opaque-locator" not in encoded
    assert locator_digest("opaque-locator") == payload.locator_sha256
    for bad in (
        encoded + b" ",
        encoded.replace(b'"version":1', b'"version":true'),
        encoded[:-1] + b',"version":1}',
        encoded[:-1] + b',"version":2}',
        b"\xff",
        b"{}",
    ):
        with pytest.raises(ValidationError):
            decode_hold_payload(bad)
    value: dict[str, Any] = json.loads(encoded)
    value["guest_boot_id"] = "not-a-uuid"
    with pytest.raises(ValidationError):
        decode_hold_payload(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    value = json.loads(encoded)
    value["extra"] = 1
    with pytest.raises(ValidationError):
        decode_hold_payload(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    value = json.loads(encoded)
    del value["user"]
    with pytest.raises(ValidationError):
        decode_hold_payload(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    value = json.loads(encoded)
    del value["guest_pid"]
    with pytest.raises(ValidationError):
        decode_hold_payload(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
