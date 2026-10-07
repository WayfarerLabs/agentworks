"""Persisted WSL2 hold takeover boundaries across a lost controller."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from weakref import ReferenceType, ref

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
from agentworks.execution._wsl2_controller_observer import ControllerIdentity, ControllerPresence
from agentworks.execution._wsl2_guest_observer import WSL2GuestObserver
from agentworks.execution._wsl2_lifecycle import (
    GuestAnchorIdentity,
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
from agentworks.execution._wsl2_platform_hold_recovery import WSL2PlatformHoldRecovery
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection
from agentworks.operations import OperationOwner
from tests.execution._bound_carrier_support import obligation_receipt

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


@pytest.mark.parametrize("version", [3, 4])
def test_recovery_default_observer_uses_and_publishes_original_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    query = QueryClient()
    monkeypatch.setattr("agentworks.execution._wsl2_windows.WindowsWSL2HostClient", lambda: query)
    connection = WSL2Connection("Ubuntu", "configured-agent", "wsl.exe")
    payload = WSL2HoldPayload(
        locator_digest("opaque-locator"),
        "c" * 32,
        "Ubuntu",
        connection.user,
        "a" * 32,
        ControllerIdentity(42, 123456789),
        GuestAnchorIdentity(BOOT, 137, 8192, 4096),
        launch_user=None if version == 3 else "root",
    )
    with closing(Database(tmp_path / "versions.db")) as database:
        predecessor = OperationOwner.acquire(database.operations, SCOPE, "proof")
        obligation = predecessor.register_lifecycle_obligation(
            OBLIGATION_KIND, payload_version=version, payload=encode_hold_payload(payload)
        )
        obligation.mark_possible_effect()
        owner = OperationOwner.recover(database.operations, predecessor.ownership, "d" * 32)
        row = database.operations.list_pending_lifecycle_obligations(owner.ownership)[0]
        recovery = WSL2PlatformHoldRecovery(
            owner,
            row,
            locator="opaque-locator",
            instance_marker="c" * 32,
            connection=connection,
            controller_observer=Controller(),
        )
        assert recovery.recover(Deadline.after(2))
        persisted = obligation_receipt(owner, row.obligation_id)
        assert persisted.state is LifecycleObligationState.RESOLVED
        assert persisted.payload_version == version
        assert decode_hold_payload(persisted.payload) == replace(payload, query_may_have_been_admitted=True)
        assert query.argv[4] == ("configured-agent" if version == 3 else "root")
        assert query.runtime_reads == (0 if version == 3 else 1)


@pytest.mark.parametrize("envelope,body", [(3, 4), (4, 3), (2, 3), (5, 4)])
def test_recovery_refuses_unknown_or_mismatched_envelope_before_rebinding(
    tmp_path: Path, envelope: int, body: int
) -> None:
    payload = WSL2HoldPayload(
        locator_digest("opaque-locator"),
        "c" * 32,
        "Ubuntu",
        "root",
        "a" * 32,
        ControllerIdentity(42, 123456789),
        launch_user=None if body == 3 else "root",
    )
    with closing(Database(tmp_path / "mismatch.db")) as database:
        predecessor = OperationOwner.acquire(database.operations, SCOPE, "proof")
        predecessor.register_lifecycle_obligation(
            OBLIGATION_KIND, payload_version=envelope, payload=encode_hold_payload(payload)
        )
        owner = OperationOwner.recover(database.operations, predecessor.ownership, "d" * 32)
        row = database.operations.list_pending_lifecycle_obligations(owner.ownership)[0]
        with pytest.raises(StateError):
            WSL2PlatformHoldRecovery(
                owner, row, locator="opaque-locator", instance_marker="c" * 32, connection=CONNECTION
            )
        assert database.operations.list_pending_lifecycle_obligations(owner.ownership)[0] == row


@dataclass
class AnchorClient:
    nonce: str = ""
    local: LocalResourceSnapshot = NEVER

    def current_controller_identity(self) -> tuple[int, int]:
        return 42, 123456789

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        self.nonce = next(arg for arg in argv if len(arg) == 32 and set(arg) <= set("0123456789abcdef"))
        self.local = OPEN

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        if limit == 129:
            return f"AGW_RUNTIME_1:{self.nonce}:ready:0\n".encode()
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
    present: bool = False
    settle_open_count: int = 0
    settle_calls: int = 0
    runtime_reads: int = 0
    argv: tuple[str, ...] = ()

    def current_controller_identity(self) -> tuple[int, int]:
        raise AssertionError("guest query cannot inspect controller identity")

    def snapshot(self) -> LocalResourceSnapshot:
        return SETTLED if self.settled else OPEN

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        self.spawned = True
        self.argv = argv
        self.nonce = next(arg for arg in argv if len(arg) == 32 and set(arg) <= set("0123456789abcdef"))
        self.pid = "137"

    def close_stdin(self) -> None:
        pass

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        if limit == 129:
            self.runtime_reads += 1
            return f"AGW_RUNTIME_1:{self.nonce}:ready:0\n".encode()
        self.reads += 1
        if self.reads == 1:
            if self.present:
                return f"AGW_GQ2 {self.nonce} {self.pid} {BOOT} 4096 found 8192\n".encode()
            return f"AGW_GQ2 {self.nonce} {self.pid} {BOOT} 4096 missing -\n".encode()
        return b"late" if self.incomplete else b""

    def wait(self, deadline: Deadline) -> int:
        return 0

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot:
        self.settle_calls += 1
        if self.settle_calls <= self.settle_open_count:
            return OPEN
        self.settled = True
        return SETTLED


def _start_hold(path: Path) -> tuple[OperationOwnership, str]:
    with closing(Database(path)) as database:
        owner = OperationOwner.acquire(database.operations, SCOPE, "proof")
        subject = WSL2PlatformHold(owner, "vm-one", "opaque-locator", "c" * 32, CONNECTION, AnchorClient())
        subject.start(Deadline.after(2))
        row = database.operations.list_pending_lifecycle_obligations(owner.ownership)[0]
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        return owner.ownership, row.obligation_id


def _recover(
    path: Path,
    predecessor: OperationOwnership,
    receipt_id: str,
    *,
    generation: str = "d" * 32,
    controller: Controller,
    query: QueryClient | None = None,
) -> bool:
    with closing(Database(path)) as database:
        owner = OperationOwner.recover(database.operations, predecessor, generation)
        row = obligation_receipt(owner, receipt_id)
        observer = WSL2GuestObserver(CONNECTION, client_factory=lambda: query) if query is not None else None
        recovery = WSL2PlatformHoldRecovery(
            owner,
            row,
            locator="opaque-locator",
            instance_marker="c" * 32,
            connection=CONNECTION,
            controller_observer=controller,
            guest_observer=observer,
        )
        return recovery.recover(Deadline.after(2))


def test_ready_recovery_resolves_only_after_exact_settled_absence(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    predecessor, receipt_id = _start_hold(path)
    query = QueryClient()
    controller = Controller()
    assert _recover(path, predecessor, receipt_id, controller=controller, query=query)
    assert controller.calls == 1 and query.spawned and query.settled
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None and current.ownership != predecessor
        row = database.operations.inspect_lifecycle_obligation(current.ownership, receipt_id)
        assert row is not None
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
            launch_user="root",
        )
        obligation = owner.register_lifecycle_obligation(
            OBLIGATION_KIND, payload_version=PAYLOAD_VERSION, payload=encode_hold_payload(payload)
        )
        obligation.mark_possible_effect()
        predecessor = owner.ownership
        receipt_id = obligation.obligation_id
    controller = Controller()
    query = QueryClient()
    assert not _recover(path, predecessor, receipt_id, controller=controller, query=query)
    assert controller.calls == 0 and not query.spawned
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        assert (
            database.operations.list_pending_lifecycle_obligations(current.ownership)[0].state
            is LifecycleObligationState.POSSIBLE_EFFECT
        )

    other = tmp_path / "marked.db"
    predecessor, receipt_id = _start_hold(other)
    with closing(Database(other)) as database:
        row = database.operations.list_pending_lifecycle_obligations(predecessor)[0]
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
    assert not _recover(other, predecessor, receipt_id, controller=controller, query=query)
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
            launch_user="root",
        )
        owner.register_lifecycle_obligation(
            OBLIGATION_KIND, payload_version=PAYLOAD_VERSION, payload=encode_hold_payload(payload)
        )
        predecessor = owner.ownership
    with closing(Database(path)) as database:
        owner = OperationOwner.recover(database.operations, predecessor, "d" * 32)
        row = database.operations.list_pending_lifecycle_obligations(owner.ownership)[0]
        with pytest.raises(StateError):
            WSL2PlatformHoldRecovery(
                owner,
                row,
                locator="wrong",
                instance_marker="c" * 32,
                connection=CONNECTION,
            )
        recovery = WSL2PlatformHoldRecovery(
            owner,
            row,
            locator="opaque-locator",
            instance_marker="c" * 32,
            connection=CONNECTION,
        )
        assert recovery.recover(Deadline.after(2))
        assert obligation_receipt(owner, row.obligation_id).state is LifecycleObligationState.RESOLVED


def test_controller_presence_refuses_without_query_admission(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    predecessor, receipt_id = _start_hold(path)
    controller = Controller(ControllerPresence.PRESENT)
    query = QueryClient()
    assert not _recover(path, predecessor, receipt_id, controller=controller, query=query)
    assert controller.calls == 1 and not query.spawned
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        row = database.operations.inspect_lifecycle_obligation(current.ownership, receipt_id)
        assert row is not None
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert not decode_hold_payload(row.payload).query_may_have_been_admitted


def test_incomplete_query_survives_second_takeover_without_replay(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    predecessor, receipt_id = _start_hold(path)
    first_query = QueryClient(incomplete=True)
    assert not _recover(path, predecessor, receipt_id, controller=Controller(), query=first_query)
    assert first_query.spawned and first_query.settled
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        row = database.operations.inspect_lifecycle_obligation(current.ownership, receipt_id)
        assert row is not None
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_hold_payload(row.payload).query_may_have_been_admitted
        predecessor = current.ownership
    second_query = QueryClient()
    second_controller = Controller()
    assert not _recover(
        path, predecessor, receipt_id, generation="e" * 32, controller=second_controller, query=second_query
    )
    assert second_controller.calls == 0 and not second_query.spawned


def test_lost_query_admission_reply_does_not_dispatch_or_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "hold.db"
    predecessor, receipt_id = _start_hold(path)
    original = OperationRepository.publish_lifecycle_obligation_payload

    def lost_reply(self: OperationRepository, *args: object, **kwargs: object) -> object:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        raise OSError("lost committed admission reply")

    monkeypatch.setattr(OperationRepository, "publish_lifecycle_obligation_payload", lost_reply)
    query = QueryClient()
    with pytest.raises(OSError):
        _recover(path, predecessor, receipt_id, controller=Controller(), query=query)
    assert not query.spawned
    monkeypatch.setattr(OperationRepository, "publish_lifecycle_obligation_payload", original)
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        row = database.operations.inspect_lifecycle_obligation(current.ownership, receipt_id)
        assert row is not None
        assert decode_hold_payload(row.payload).query_may_have_been_admitted
        predecessor = current.ownership
    retry_query = QueryClient()
    assert not _recover(path, predecessor, receipt_id, generation="e" * 32, controller=Controller(), query=retry_query)
    assert not retry_query.spawned


def test_lost_resolution_reply_is_recognized_after_takeover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "hold.db"
    predecessor, receipt_id = _start_hold(path)
    original = OperationRepository.resolve_lifecycle_obligation

    def lost_reply(self: OperationRepository, *args: object) -> object:
        original(self, *args)  # type: ignore[arg-type]
        raise OSError("lost committed resolution reply")

    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", lost_reply)
    first_query = QueryClient()
    with pytest.raises(OSError):
        _recover(path, predecessor, receipt_id, controller=Controller(), query=first_query)
    assert first_query.spawned and first_query.settled
    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", original)
    with closing(Database(path)) as database:
        current = database.operations.inspect(SCOPE)
        assert current is not None
        row = database.operations.inspect_lifecycle_obligation(current.ownership, receipt_id)
        assert row is not None
        assert row.state is LifecycleObligationState.RESOLVED
        predecessor = current.ownership
    retry_query = QueryClient()
    assert _recover(path, predecessor, receipt_id, generation="e" * 32, controller=Controller(), query=retry_query)
    assert not retry_query.spawned


@pytest.mark.parametrize("committed", [False, True])
def test_terminal_absence_retries_resolution_without_guest_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, committed: bool
) -> None:
    path = tmp_path / "hold.db"
    predecessor, receipt_id = _start_hold(path)
    with closing(Database(path)) as database:
        owner = OperationOwner.recover(database.operations, predecessor, "d" * 32)
        row = database.operations.list_pending_lifecycle_obligations(owner.ownership)[0]
        query = QueryClient()
        created: list[QueryClient] = []

        def factory() -> QueryClient:
            created.append(query)
            return query

        controller = Controller()
        recovery = WSL2PlatformHoldRecovery(
            owner,
            row,
            locator="opaque-locator",
            instance_marker="c" * 32,
            connection=CONNECTION,
            controller_observer=controller,
            guest_observer=WSL2GuestObserver(CONNECTION, client_factory=factory),
        )
        original = OperationRepository.resolve_lifecycle_obligation

        def failed_reply(self: OperationRepository, *args: object) -> object:
            if committed:
                original(self, *args)  # type: ignore[arg-type]
            raise OSError("resolution reply unavailable")

        monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", failed_reply)
        with pytest.raises(OSError):
            recovery.recover(Deadline.after(2))
        assert query.spawned and query.settled and len(created) == 1
        persisted = obligation_receipt(owner, row.obligation_id)
        assert persisted.state is (
            LifecycleObligationState.RESOLVED if committed else LifecycleObligationState.POSSIBLE_EFFECT
        )
        monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", original)
        assert recovery.recover(Deadline.after(2))
        assert len(created) == 1 and controller.calls == 1
        assert obligation_receipt(owner, row.obligation_id).state is LifecycleObligationState.RESOLVED


def test_unsettled_unknown_retains_native_client_for_cleanup_only(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    predecessor, receipt_id = _start_hold(path)
    with closing(Database(path)) as database:
        owner = OperationOwner.recover(database.operations, predecessor, "d" * 32)
        row = database.operations.list_pending_lifecycle_obligations(owner.ownership)[0]
        created: list[ReferenceType[QueryClient]] = []

        def factory() -> QueryClient:
            client = QueryClient(settle_open_count=1)
            created.append(ref(client))
            return client

        recovery = WSL2PlatformHoldRecovery(
            owner,
            row,
            locator="opaque-locator",
            instance_marker="c" * 32,
            connection=CONNECTION,
            controller_observer=Controller(),
            guest_observer=WSL2GuestObserver(CONNECTION, client_factory=factory),
        )
        assert not recovery.recover(Deadline.after(2))
        assert len(created) == 1
        client = created[0]()
        assert client is not None
        assert client.spawned and not client.settled
        assert recovery.settle_pending(Deadline.after(2))
        assert client.settled and client.settle_calls == 2
        assert not recovery.recover(Deadline.after(2))
        assert len(created) == 1
        persisted = obligation_receipt(owner, row.obligation_id)
        assert persisted.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_hold_payload(persisted.payload).query_may_have_been_admitted


def test_complete_present_allows_same_controller_absence_retry(tmp_path: Path) -> None:
    path = tmp_path / "hold.db"
    predecessor, receipt_id = _start_hold(path)
    with closing(Database(path)) as database:
        owner = OperationOwner.recover(database.operations, predecessor, "d" * 32)
        row = database.operations.list_pending_lifecycle_obligations(owner.ownership)[0]
        clients = [QueryClient(present=True), QueryClient()]
        created: list[QueryClient] = []

        def factory() -> QueryClient:
            client = clients[len(created)]
            created.append(client)
            return client

        recovery = WSL2PlatformHoldRecovery(
            owner,
            row,
            locator="opaque-locator",
            instance_marker="c" * 32,
            connection=CONNECTION,
            controller_observer=Controller(),
            guest_observer=WSL2GuestObserver(CONNECTION, client_factory=factory),
        )
        assert not recovery.recover(Deadline.after(2))
        assert len(created) == 1 and clients[0].settled
        assert recovery.recover(Deadline.after(2))
        assert len(created) == 2 and clients[1].settled
        persisted = obligation_receipt(owner, row.obligation_id)
        assert persisted.state is LifecycleObligationState.RESOLVED
