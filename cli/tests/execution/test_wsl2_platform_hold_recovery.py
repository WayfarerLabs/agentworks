"""Persisted WSL2 hold takeover boundaries across a lost controller."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.db import Database
from agentworks.db.operations import (
    LifecycleObligationState,
    OperationOwnership,
    OperationRepository,
    OperationResourceKind,
    OperationScope,
)
from agentworks.errors import StateError
from agentworks.execution._wsl2_controller_observer import ControllerPresence
from agentworks.execution._wsl2_guest_observer import WSL2GuestObserver
from agentworks.execution._wsl2_lifecycle import (
    HandleSettlement,
    HostClientStatus,
    JobAssignment,
    LocalResourceSnapshot,
)
from agentworks.execution._wsl2_platform_hold import (
    OBLIGATION_KIND,
    PAYLOAD_VERSION,
    WSL2HoldPayload,
    WSL2PlatformHold,
    decode_hold_payload,
    encode_hold_payload,
    locator_digest,
)
from agentworks.execution._wsl2_platform_hold_recovery import recover_platform_hold
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection
from agentworks.operations import OperationOwner

BOOT = "12345678-1234-1234-1234-123456789abc"
SCOPE = OperationScope(OperationResourceKind.VM, "vm-one")
CONNECTION = WSL2Connection("Ubuntu", "root", "wsl.exe")
SETTLED = LocalResourceSnapshot(
    HostClientStatus.EXITED, 0, JobAssignment.ASSIGNED_AT_CREATION, HandleSettlement.CLOSED, HandleSettlement.CLOSED
)
OPEN = LocalResourceSnapshot(
    HostClientStatus.ACTIVE, None, JobAssignment.ASSIGNED_AT_CREATION, HandleSettlement.OPEN, HandleSettlement.OPEN
)
NEVER = LocalResourceSnapshot(
    HostClientStatus.NOT_CREATED,
    None,
    JobAssignment.NOT_CREATED,
    HandleSettlement.NOT_CREATED,
    HandleSettlement.NOT_CREATED,
)


@dataclass
class AnchorClient:
    nonce: str = ""
    local: LocalResourceSnapshot = NEVER

    def current_controller_identity(self) -> tuple[int, int]:
        return 42, 123456789

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        self.nonce = argv[-1]
        self.local = OPEN

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        return f"READY {self.nonce} {BOOT} 137 8192 4096\n".encode()

    def snapshot(self) -> LocalResourceSnapshot:
        return self.local

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot:
        self.local = SETTLED
        return SETTLED

    def close_stdin(self) -> None:
        pass

    def wait(self, deadline: Deadline) -> int:
        return 0


@dataclass
class Controller:
    presence: ControllerPresence = ControllerPresence.ABSENT_CONFIRMED
    calls: int = 0

    def observe(self, identity: object, deadline: Deadline) -> ControllerPresence:
        self.calls += 1
        assert identity.pid == 42 and identity.creation_ticks == 123456789  # type: ignore[attr-defined]
        return self.presence


@dataclass
class QueryClient:
    spawned: bool = False
    nonce: str = ""
    pid: str = ""
    settled: bool = False
    reads: int = 0
    incomplete: bool = False

    def current_controller_identity(self) -> tuple[int, int]:
        raise AssertionError("guest query cannot inspect controller identity")

    def snapshot(self) -> LocalResourceSnapshot:
        return SETTLED if self.settled else OPEN

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        self.spawned = True
        self.nonce, self.pid = argv[-2:]

    def close_stdin(self) -> None:
        pass

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        self.reads += 1
        if self.reads == 1:
            return f"AGW_GQ2 {self.nonce} {self.pid} {BOOT} 4096 missing -\n".encode()
        return b"late" if self.incomplete else b""

    def wait(self, deadline: Deadline) -> int:
        return 0

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot:
        self.settled = True
        return SETTLED


def _start_hold(path: Path) -> tuple[OperationOwnership, str]:
    with closing(Database(path)) as database:
        owner = OperationOwner.acquire(database.operations, SCOPE, "proof")
        subject = WSL2PlatformHold(owner, "vm-one", "opaque-locator", "c" * 32, CONNECTION, AnchorClient())
        subject.start(Deadline.after(2))
        row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        return owner.ownership, row.obligation_id


def _recover(
    path: Path,
    predecessor: OperationOwnership,
    *,
    generation: str = "d" * 32,
    controller: Controller,
    query: QueryClient | None = None,
) -> bool:
    with closing(Database(path)) as database:
        owner = OperationOwner.recover(database.operations, predecessor, generation)
        row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
        observer = WSL2GuestObserver(CONNECTION, client_factory=lambda: query) if query is not None else None
        return recover_platform_hold(
            owner,
            row,
            locator="opaque-locator",
            instance_marker="c" * 32,
            connection=CONNECTION,
            deadline=Deadline.after(2),
            controller_observer=controller,
            guest_observer=observer,
        )


def test_ready_recovery_resolves_only_after_exact_settled_absence(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    predecessor, _ = _start_hold(path)
    query = QueryClient()
    controller = Controller()
    assert _recover(path, predecessor, controller=controller, query=query)
    assert controller.calls == 1 and query.spawned and query.settled
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None and current.ownership != predecessor
        row = database.operations.list_lifecycle_obligations(current.ownership)[0]
        assert row.state is LifecycleObligationState.RESOLVED
        assert decode_hold_payload(row.payload).query_may_have_been_admitted


def test_missing_ready_and_marked_query_retain_claim(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    with closing(Database(path)) as database:
        owner = OperationOwner.acquire(database.operations, SCOPE, "proof")
        from agentworks.execution._wsl2_controller_observer import ControllerIdentity

        payload = WSL2HoldPayload(
            locator_digest("opaque-locator"),
            "c" * 32,
            "Ubuntu",
            "root",
            "a" * 32,
            ControllerIdentity(42, 123456789),
        )
        obligation = owner.register_lifecycle_obligation(
            OBLIGATION_KIND, payload_version=PAYLOAD_VERSION, payload=encode_hold_payload(payload)
        )
        obligation.mark_possible_effect()
        predecessor = owner.ownership
    controller = Controller()
    query = QueryClient()
    assert not _recover(path, predecessor, controller=controller, query=query)
    assert controller.calls == 0 and not query.spawned
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        assert (
            database.operations.list_lifecycle_obligations(current.ownership)[0].state
            is LifecycleObligationState.POSSIBLE_EFFECT
        )

    other = tmp_path / "marked.db"
    predecessor, _ = _start_hold(other)
    with closing(Database(other)) as database:
        row = database.operations.list_lifecycle_obligations(predecessor)[0]
        payload = decode_hold_payload(row.payload)
        from dataclasses import replace

        database.operations.publish_lifecycle_obligation_payload(
            predecessor,
            row.obligation_id,
            expected_revision=row.payload_revision,
            payload_version=PAYLOAD_VERSION,
            payload=encode_hold_payload(replace(payload, query_may_have_been_admitted=True)),
        )
    controller = Controller()
    query = QueryClient()
    assert not _recover(other, predecessor, controller=controller, query=query)
    assert controller.calls == 0 and not query.spawned


def test_registered_takeover_resolves_and_mismatch_refuses(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    with closing(Database(path)) as database:
        owner = OperationOwner.acquire(database.operations, SCOPE, "proof")
        from agentworks.execution._wsl2_controller_observer import ControllerIdentity

        payload = WSL2HoldPayload(
            locator_digest("opaque-locator"),
            "c" * 32,
            "Ubuntu",
            "root",
            "a" * 32,
            ControllerIdentity(42, 123456789),
        )
        owner.register_lifecycle_obligation(
            OBLIGATION_KIND, payload_version=PAYLOAD_VERSION, payload=encode_hold_payload(payload)
        )
        predecessor = owner.ownership
    with closing(Database(path)) as database:
        owner = OperationOwner.recover(database.operations, predecessor, "d" * 32)
        row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
        with pytest.raises(StateError):
            recover_platform_hold(
                owner,
                row,
                locator="wrong",
                instance_marker="c" * 32,
                connection=CONNECTION,
                deadline=Deadline.after(2),
            )
        assert recover_platform_hold(
            owner,
            row,
            locator="opaque-locator",
            instance_marker="c" * 32,
            connection=CONNECTION,
            deadline=Deadline.after(2),
        )
        assert (
            database.operations.list_lifecycle_obligations(owner.ownership)[0].state
            is LifecycleObligationState.RESOLVED
        )


def test_controller_presence_refuses_without_query_admission(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    predecessor, _ = _start_hold(path)
    controller = Controller(ControllerPresence.PRESENT)
    query = QueryClient()
    assert not _recover(path, predecessor, controller=controller, query=query)
    assert controller.calls == 1 and not query.spawned
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        row = database.operations.list_lifecycle_obligations(current.ownership)[0]
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert not decode_hold_payload(row.payload).query_may_have_been_admitted


def test_incomplete_query_survives_second_takeover_without_replay(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    predecessor, _ = _start_hold(path)
    first_query = QueryClient(incomplete=True)
    assert not _recover(path, predecessor, controller=Controller(), query=first_query)
    assert first_query.spawned and first_query.settled
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        row = database.operations.list_lifecycle_obligations(current.ownership)[0]
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_hold_payload(row.payload).query_may_have_been_admitted
        predecessor = current.ownership
    second_query = QueryClient()
    second_controller = Controller()
    assert not _recover(path, predecessor, generation="e" * 32, controller=second_controller, query=second_query)
    assert second_controller.calls == 0 and not second_query.spawned


def test_lost_query_admission_reply_does_not_dispatch_or_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "hold.db"
    predecessor, _ = _start_hold(path)
    original = OperationRepository.publish_lifecycle_obligation_payload

    def lost_reply(self: OperationRepository, *args: object, **kwargs: object) -> object:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        raise OSError("lost committed admission reply")

    monkeypatch.setattr(OperationRepository, "publish_lifecycle_obligation_payload", lost_reply)
    query = QueryClient()
    with pytest.raises(OSError):
        _recover(path, predecessor, controller=Controller(), query=query)
    assert not query.spawned
    monkeypatch.setattr(OperationRepository, "publish_lifecycle_obligation_payload", original)
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        row = database.operations.list_lifecycle_obligations(current.ownership)[0]
        assert decode_hold_payload(row.payload).query_may_have_been_admitted
        predecessor = current.ownership
    retry_query = QueryClient()
    assert not _recover(path, predecessor, generation="e" * 32, controller=Controller(), query=retry_query)
    assert not retry_query.spawned


def test_lost_resolution_reply_is_recognized_after_takeover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "hold.db"
    predecessor, _ = _start_hold(path)
    original = OperationRepository.resolve_lifecycle_obligation

    def lost_reply(self: OperationRepository, *args: object) -> object:
        original(self, *args)  # type: ignore[arg-type]
        raise OSError("lost committed resolution reply")

    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", lost_reply)
    first_query = QueryClient()
    with pytest.raises(OSError):
        _recover(path, predecessor, controller=Controller(), query=first_query)
    assert first_query.spawned and first_query.settled
    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", original)
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        row = database.operations.list_lifecycle_obligations(current.ownership)[0]
        assert row.state is LifecycleObligationState.RESOLVED
        predecessor = current.ownership
    retry_query = QueryClient()
    assert _recover(path, predecessor, generation="e" * 32, controller=Controller(), query=retry_query)
    assert not retry_query.spawned
