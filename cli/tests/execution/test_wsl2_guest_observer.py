"""Ordinary-query custody and response proof for the private WSL2 observer."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentworks.execution._wsl2_guest_observer import WSL2GuestObserver
from agentworks.execution._wsl2_guest_query import FIXED_GUEST_QUERY_SOURCE
from agentworks.execution._wsl2_lifecycle import (
    GuestAnchorIdentity,
    GuestAnchorPresence,
    HandleSettlement,
    HostClientStatus,
    JobAssignment,
    LocalResourceSnapshot,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection

BOOT = "12345678-1234-1234-1234-123456789abc"
IDENTITY = GuestAnchorIdentity(BOOT, 137, 8192, 4096)
OPEN = LocalResourceSnapshot(
    HostClientStatus.ACTIVE, None, JobAssignment.ASSIGNED_AT_CREATION, HandleSettlement.OPEN, HandleSettlement.OPEN
)
SETTLED = LocalResourceSnapshot(
    HostClientStatus.EXITED, 0, JobAssignment.ASSIGNED_AT_CREATION, HandleSettlement.CLOSED, HandleSettlement.CLOSED
)


@dataclass
class FakeClient:
    events: list[str]
    fail_at: str | None = None
    spawn_error: Exception | None = None
    settle_error: BaseException | None = None
    trailing: bytes = b""
    exit_status: int | None = 0
    response: bytes | None = None
    kind: str = "missing"
    local: LocalResourceSnapshot = OPEN
    argv: tuple[str, ...] = ()
    reads: int = 0
    settlement_blocked: bool = False

    def _step(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise KeyboardInterrupt(name)

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        assert not deadline.expired
        self.argv = argv
        self._step("spawn")
        if self.spawn_error is not None:
            raise self.spawn_error

    def current_controller_identity(self) -> tuple[int, int]:
        raise AssertionError("query must not inspect controller identity")

    def close_stdin(self) -> None:
        self._step("close_stdin")

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        assert not deadline.expired
        self.reads += 1
        self._step("read_line" if self.reads == 1 else "read_eof")
        if self.reads == 1:
            nonce = self.argv[-2]
            ticks = "8192" if self.kind == "found" else "-"
            return (
                self.response
                if self.response is not None
                else f"AGW_GQ2 {nonce} 137 {BOOT} 4096 {self.kind} {ticks}\n".encode()
            )
        return self.trailing

    def wait(self, deadline: Deadline) -> int | None:
        self._step("wait")
        return self.exit_status

    def snapshot(self) -> LocalResourceSnapshot:
        self._step("snapshot")
        return self.local

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot:
        self._step("settle")
        if self.settle_error is not None:
            raise self.settle_error
        if not self.settlement_blocked:
            self.local = SETTLED
        return self.local


@dataclass
class Factory:
    events: list[str] = field(default_factory=list)
    clients: list[FakeClient] = field(default_factory=list)

    def __call__(self) -> FakeClient:
        self.events.append("create")
        client = FakeClient(self.events)
        self.clients.append(client)
        return client


def observer(factory: Factory) -> WSL2GuestObserver:
    return WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=factory)


def test_query_uses_exact_route_fixed_source_and_complete_local_proof() -> None:
    factory = Factory()
    result = observer(factory).observe(IDENTITY, Deadline.after(1))
    assert result is GuestAnchorPresence.ABSENT_CONFIRMED
    assert len(factory.clients) == 1
    argv = factory.clients[0].argv
    assert argv[:11] == (
        "wsl.exe",
        "--distribution",
        "Ubuntu",
        "--user",
        "root",
        "--exec",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        "-c",
    )
    assert argv[11] == FIXED_GUEST_QUERY_SOURCE
    assert len(argv[12]) == 32 and argv[13] == "137"
    assert factory.events == ["create", "spawn", "close_stdin", "read_line", "wait", "read_eof", "settle"]


@pytest.mark.parametrize("trailing", [b"x", b"\n", b"another line\n"])
def test_trailing_output_prevents_absence(trailing: bytes) -> None:
    factory = Factory()
    client = FakeClient(factory.events, trailing=trailing)
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: client)
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN


@pytest.mark.parametrize("fail_at", ["spawn", "close_stdin", "read_line", "wait", "read_eof", "settle"])
def test_interruption_retains_client_and_blocks_new_dispatch(fail_at: str) -> None:
    factory = Factory()
    first = FakeClient(factory.events, fail_at=fail_at)
    second = FakeClient(factory.events)
    created = iter((first, second))
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: next(created))
    with pytest.raises(KeyboardInterrupt):
        subject.observe(IDENTITY, Deadline.after(1))
    first.fail_at = None
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN
    assert not second.argv
    assert factory.events.count("spawn") == 1


def test_unsettled_attempt_remains_blocked_after_local_settlement_retry() -> None:
    factory = Factory()
    first = FakeClient(factory.events, fail_at="read_line", settlement_blocked=True)
    second = FakeClient(factory.events)
    created = iter((first, second))
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: next(created))
    with pytest.raises(KeyboardInterrupt):
        subject.observe(IDENTITY, Deadline.after(1))
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN
    assert not second.argv
    first.settlement_blocked = False
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN
    assert not second.argv
    assert factory.events.count("spawn") == 1


@pytest.mark.parametrize("cleanup_type", [KeyboardInterrupt, SystemExit])
def test_cleanup_control_interruption_after_ordinary_spawn_failure_propagates_and_retains_client(
    cleanup_type: type[BaseException],
) -> None:
    events: list[str] = []
    first = FakeClient(events, spawn_error=OSError("spawn failed"), settle_error=cleanup_type("cleanup interrupted"))
    second = FakeClient(events)
    created = iter((first, second))
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: next(created))
    with pytest.raises(cleanup_type):
        subject.observe(IDENTITY, Deadline.after(1))
    assert not second.argv
    first.settle_error = None
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN
    assert not second.argv
    assert events[:3] == ["spawn", "settle", "settle"]


def test_ordinary_cleanup_failure_after_spawn_failure_returns_unknown_and_retains_client() -> None:
    events: list[str] = []
    first = FakeClient(events, spawn_error=OSError("spawn failed"), settle_error=OSError("settle failed"))
    second = FakeClient(events)
    created = iter((first, second))
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: next(created))
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN
    assert not second.argv
    first.settle_error = None
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN
    assert not second.argv
    assert events[:3] == ["spawn", "settle", "settle"]


def test_complete_present_result_permits_later_absence_query() -> None:
    events: list[str] = []
    first = FakeClient(events, kind="found")
    second = FakeClient(events)
    created = iter((first, second))
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: next(created))
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.PRESENT
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.ABSENT_CONFIRMED
    assert events.count("spawn") == 2


def test_nonzero_exit_is_unknown_even_with_complete_output_and_settlement() -> None:
    factory = Factory()
    client = FakeClient(factory.events, exit_status=1)
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: client)
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN


@pytest.mark.parametrize("failure", ["trailing", "exit", "malformed"])
def test_complete_but_untrusted_attempt_blocks_later_query(failure: str) -> None:
    events: list[str] = []
    first = FakeClient(events)
    if failure == "trailing":
        first.trailing = b"extra"
    elif failure == "exit":
        first.exit_status = 1
    else:
        first.response = b"AGW_GQ2 malformed\n"
    second = FakeClient(events)
    created = iter((first, second))
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: next(created))
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN
    assert not second.argv


@pytest.mark.parametrize("response", [b"", b"AGW_GQ2 malformed\n"])
def test_incomplete_or_malformed_output_is_unknown(response: bytes) -> None:
    factory = Factory()
    client = FakeClient(factory.events, response=response)
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: client)
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN


def test_expired_deadline_does_not_create_client() -> None:
    factory = Factory()
    assert observer(factory).observe(IDENTITY, Deadline.after(0)) is GuestAnchorPresence.UNKNOWN
    assert factory.events == []
