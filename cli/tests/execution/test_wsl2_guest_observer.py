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
    trailing: bytes = b""
    exit_status: int | None = 0
    response: bytes | None = None
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
            return (
                self.response if self.response is not None else f"AGW_GQ2 {nonce} 137 {BOOT} 4096 missing -\n".encode()
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
    assert factory.events == ["create", "spawn", "close_stdin", "read_line", "wait", "read_eof", "settle", "snapshot"]


@pytest.mark.parametrize("trailing", [b"x", b"\n", b"another line\n"])
def test_trailing_output_prevents_absence(trailing: bytes) -> None:
    factory = Factory()
    client = FakeClient(factory.events, trailing=trailing)
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: client)
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN


@pytest.mark.parametrize("fail_at", ["spawn", "close_stdin", "read_line", "wait", "read_eof", "settle", "snapshot"])
def test_interruption_retains_client_then_settles_before_new_dispatch(fail_at: str) -> None:
    factory = Factory()
    first = FakeClient(factory.events, fail_at=fail_at)
    second = FakeClient(factory.events)
    created = iter((first, second))
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: next(created))
    with pytest.raises(KeyboardInterrupt):
        subject.observe(IDENTITY, Deadline.after(1))
    first.fail_at = None
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.ABSENT_CONFIRMED
    assert factory.events.index("spawn", factory.events.index(fail_at) + 1) > factory.events.index("snapshot")


def test_unsettled_attempt_blocks_new_dispatch_until_retry_succeeds() -> None:
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
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.ABSENT_CONFIRMED
    assert second.argv


def test_nonzero_exit_is_unknown_even_with_complete_output_and_settlement() -> None:
    factory = Factory()
    client = FakeClient(factory.events, exit_status=1)
    subject = WSL2GuestObserver(WSL2Connection("Ubuntu", "root", "wsl.exe"), client_factory=lambda: client)
    assert subject.observe(IDENTITY, Deadline.after(1)) is GuestAnchorPresence.UNKNOWN


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
