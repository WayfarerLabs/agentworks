"""Behavioral boundaries for the private WSL2 ledger hold."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentworks.db import Database
from agentworks.db.operations import LifecycleObligationState, OperationOwnership, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
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
        return self.local

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot:
        self.events.append("settle")
        if not self.never_created:
            self.local = SETTLED
        return self.local


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
    for bad in (encoded + b" ", encoded.replace(b'"version":1', b'"version":true'), b"\xff", b"{}"):
        with pytest.raises(ValidationError):
            decode_hold_payload(bad)
    value: dict[str, Any] = json.loads(encoded)
    value["guest_boot_id"] = "not-a-uuid"
    with pytest.raises(ValidationError):
        decode_hold_payload(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
