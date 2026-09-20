"""Shared live byte modes cross the SSH carrier boundary deliberately."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterator
from typing import Any, cast

import pytest

from agentworks.execution.carrier import (
    CarrierIO,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    LiveInput,
    PreparedInvocation,
    Provenance,
    Retention,
    SinkOutput,
)
from agentworks.execution.carriers.ssh import client
from agentworks.execution.carriers.ssh.client import SSHCarrier
from agentworks.execution.carriers.ssh.connection import SSHConnection


class ChunkSource:
    def __init__(self, chunks: list[bytes | None]) -> None:
        self.chunks = chunks
        self.limits: list[int] = []
        self.closed = False

    def try_read(self, limit: int) -> bytes | None:
        self.limits.append(limit)
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        self.closed = True


class ShortSink:
    def __init__(self, limit: int, *, stall_every: int | None = None) -> None:
        self.limit = limit
        self.stall_every = stall_every
        self.calls = 0
        self.offers: list[int] = []
        self.data = bytearray()
        self.closed = False

    def try_write(self, data: memoryview) -> int | None:
        self.calls += 1
        self.offers.append(len(data))
        if self.stall_every is not None and self.calls % self.stall_every == 0:
            return None
        written = min(self.limit, len(data))
        self.data.extend(data[:written])
        return written

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    original = subprocess.Popen

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(argv, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


def execute(monkeypatch: pytest.MonkeyPatch, script: str, io: CarrierIO):
    connection = cast("SSHConnection", object())
    monkeypatch.setattr(client, "admit_connection", lambda unused: object())
    monkeypatch.setattr(client, "check_client_version", lambda unused, *, deadline: None)
    monkeypatch.setattr(
        client,
        "build_ssh_argv",
        lambda unused, invocation, *, trust: [sys.executable, "-c", script],
    )
    return SSHCarrier(connection).execute(
        PreparedInvocation(("/synthetic/program",)),
        io=io,
        deadline=Deadline.after(5),
    )


def assert_closed(children: list[subprocess.Popen[bytes]]) -> None:
    assert children
    for child in children:
        assert child.poll() is not None
        assert all(pipe is None or pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))


def test_ssh_advertises_live_byte_io_without_inspection() -> None:
    carrier = SSHCarrier(cast("SSHConnection", object()))

    assert carrier.features.live_stdio
    assert not carrier.features.terminal


@pytest.mark.windows
def test_live_duplex_preserves_binary_streams_under_backpressure(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = bytes(range(256)) * 512
    chunks: list[bytes | None] = [None]
    chunks.extend(data[offset : offset + 8192] for offset in range(0, len(data), 8192))
    chunks.append(b"")
    source = ChunkSource(chunks)
    stdout = ShortSink(4093, stall_every=4)
    stderr = ShortSink(3079, stall_every=3)

    report = execute(
        monkeypatch,
        "import hashlib,sys; "
        "sys.stdout.buffer.write(b'\\x00\\xffout\\r\\n'*10000); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'\\x80err\\n'*10000); sys.stderr.buffer.flush(); "
        "data=sys.stdin.buffer.read(); sys.stdout.buffer.write(hashlib.sha256(data).digest()); sys.exit(23)",
        CarrierIO(input=LiveInput(source), output=SinkOutput(stdout, stderr, require_live=True)),
    )

    assert bytes(stdout.data) == b"\x00\xffout\r\n" * 10_000 + hashlib.sha256(data).digest()
    assert bytes(stderr.data) == b"\x80err\n" * 10_000
    assert report.dispatch == Dispatch.SENT
    assert report.completion == ExitStatus(code=23)
    assert report.failure is None
    assert report.stdout.data == report.stderr.data == b""
    assert report.stdout.complete and report.stderr.complete
    assert report.stdout.retention == report.stderr.retention == Retention.DELIVERED
    assert report.stdout.provenance == Provenance.CARRIER_STDOUT
    assert report.stderr.provenance == Provenance.MIXED_STDERR
    assert source.limits and max(stdout.offers + stderr.offers) <= 65_536
    assert not source.closed and not stdout.closed and not stderr.closed
    assert_closed(children)


@pytest.mark.parametrize("endpoint", ["source", "sink"])
@pytest.mark.windows
def test_live_endpoint_fault_is_safe_and_cleans_the_client(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    class BrokenSource:
        def try_read(self, limit: int) -> bytes | None:
            raise ValueError("secret-source-canary")

    class BrokenSink:
        def try_write(self, data: memoryview) -> int | None:
            raise ValueError("secret-sink-canary")

    stdout = BrokenSink() if endpoint == "sink" else ShortSink(64)
    io = (
        CarrierIO(input=LiveInput(BrokenSource()))
        if endpoint == "source"
        else CarrierIO(output=SinkOutput(stdout, ShortSink(64), require_live=True))
    )
    report = execute(
        monkeypatch,
        "import sys,time; sys.stdout.buffer.write(b'payload'); sys.stdout.buffer.flush(); time.sleep(30)",
        io,
    )

    assert report.failure == (Failure.INPUT if endpoint == "source" else Failure.OUTPUT)
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert "secret-source-canary" not in repr(report)
    assert "secret-sink-canary" not in repr(report)
    assert_closed(children)


@pytest.mark.windows
def test_live_endpoint_interruption_reaps_before_propagating(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    class InterruptingSource:
        closed = False

        def try_read(self, limit: int) -> bytes | None:
            raise KeyboardInterrupt

        def close(self) -> None:
            self.closed = True

    source = InterruptingSource()
    with pytest.raises(KeyboardInterrupt):
        execute(monkeypatch, "import time; time.sleep(30)", CarrierIO(input=LiveInput(source)))

    assert not source.closed
    assert_closed(children)


@pytest.mark.windows
def test_live_delivery_does_not_turn_ssh_255_into_remote_completion(
    children: list[subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = ShortSink(2, stall_every=2)
    stderr = ShortSink(3, stall_every=3)
    report = execute(
        monkeypatch,
        "import sys; sys.stdout.buffer.write(b'out\\x00\\xff'); "
        "sys.stderr.buffer.write(b'err\\x80'); sys.exit(255)",
        CarrierIO(output=SinkOutput(stdout, stderr, require_live=True)),
    )

    assert bytes(stdout.data) == b"out\x00\xff"
    assert bytes(stderr.data) == b"err\x80"
    assert report.local_status == 255
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert report.failure == Failure.OBSERVATION
    assert report.stdout.complete and report.stderr.complete
    assert_closed(children)


@pytest.mark.integration
def test_installed_ssh_live_duplex_preserves_sensitive_binary_delivery(
    local_sshd: SSHConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = (b"sensitive-live-canary\x00\xff\r\n" + bytes(range(256))) * 512
    chunks: list[bytes | None] = [None]
    chunks.extend(payload[offset : offset + 8191] for offset in range(0, len(payload), 8191))
    chunks.append(b"")
    source = ChunkSource(chunks)
    stdout = ShortSink(4093, stall_every=4)
    stderr = ShortSink(3079, stall_every=3)
    clients: list[tuple[list[str], subprocess.Popen[bytes]]] = []
    original = subprocess.Popen

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if argv[0] == local_sshd.ssh_executable and argv[-1] != "-V":
            assert all(process.poll() is not None for unused, process in clients)
        process = original(argv, **kwargs)
        if argv[0] == local_sshd.ssh_executable:
            clients.append((argv, process))
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    script = (
        "import sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(b'\\x00out\\xff'+data); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'\\x80err\\x00'+data[::-1]); sys.stderr.buffer.flush(); sys.exit(23)"
    )
    report = SSHCarrier(local_sshd).execute(
        PreparedInvocation((sys.executable, "-c", script)),
        io=CarrierIO(
            input=LiveInput(source, sensitive=True),
            output=SinkOutput(stdout, stderr, require_live=True),
        ),
        deadline=Deadline.after(10),
    )

    assert bytes(stdout.data) == b"\x00out\xff" + payload
    assert bytes(stderr.data) == b"\x80err\x00" + payload[::-1]
    assert report.dispatch == Dispatch.SENT
    assert report.completion == ExitStatus(code=23)
    assert report.local_status == 23
    assert report.failure is None
    assert report.stdout.data == report.stderr.data == b""
    assert report.stdout.complete and report.stderr.complete
    assert report.stdout.retention == report.stderr.retention == Retention.DELIVERED
    assert report.stdout.provenance == Provenance.CARRIER_STDOUT
    assert report.stderr.provenance == Provenance.MIXED_STDERR
    assert "sensitive-live-canary" not in repr(report)
    assert source.limits and max(stdout.offers + stderr.offers) <= 65_536
    assert not source.closed and not stdout.closed and not stderr.closed
    command_clients = [process for argv, process in clients if argv[-1] != "-V"]
    assert len(command_clients) == 1
    assert all(process.poll() is not None for unused, process in clients)
    assert all(
        pipe is None or pipe.closed
        for unused, process in clients
        for pipe in (process.stdin, process.stdout, process.stderr)
    )
