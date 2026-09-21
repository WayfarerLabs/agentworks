"""Owned forwarding lifetime, using synthetic children and optional local sshd."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _process as process_core
from agentworks.execution.carrier import Deadline, Failure
from agentworks.execution.carriers.ssh import forwarding
from agentworks.execution.carriers.ssh.connection import SSHConnection, admit_connection
from agentworks.execution.carriers.ssh.forwarding import ForwardingError, LocalForward, open_local_forwards
from agentworks.execution.carriers.ssh.trust import (
    SSHTrustFiles,
    block_trust,
    import_trust,
    refresh_trust,
    resolve_trust,
    trust_status,
)

pytestmark = pytest.mark.windows


def _forward(port: int = 12345) -> LocalForward:
    return LocalForward(IPv4Address("127.0.0.1"), port, "localhost", 80)


@dataclass
class SyntheticForwarding:
    connection: SSHConnection
    script: str = "os.write(1, marker); sys.stdin.buffer.read()"
    version: str = "import sys; sys.stderr.write('OpenSSH_9.2p1\\n')"
    children: list[subprocess.Popen[bytes]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def open(self, seconds: float | None = 5) -> forwarding.OwnedForwarding:
        return open_local_forwards(self.connection, [_forward()], deadline=Deadline.after(seconds))

    def assert_closed(self) -> None:
        for child in self.children:
            assert child.poll() is not None
            assert all(pipe is None or pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))


@pytest.fixture
def synthetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SyntheticForwarding]:
    tmp_path = tmp_path.resolve()
    identity, trust = tmp_path / "identity", tmp_path / "trust"
    identity.write_bytes(b"synthetic")
    trust.write_bytes(b"synthetic")
    value = SyntheticForwarding(SSHConnection("fixture.invalid", "fixture", identity, SSHTrustFiles((trust,))))
    original = subprocess.Popen

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        value.calls.append(argv)
        if argv[-1] == "-V":
            script = value.version
        else:
            match = re.search(r"agw-forward-ready-[0-9a-f]{32}", argv[-1])
            assert match is not None
            marker = (match[0] + "\n").encode()
            script = f"import os,sys,time,socket; marker={marker!r}; " + value.script
        child = original([sys.executable, "-c", script], **kwargs)
        value.children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    yield value
    for child in value.children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bind_address": "localhost"},
        {"bind_address": IPv6Address("fe80::1%eth0")},
        {"local_port": 0},
        {"destination_port": 65536},
        {"local_port": True},
        {"destination_host": "host:22"},
        {"destination_host": "[::1]"},
        {"destination_host": "host:/socket"},
        {"destination_host": "-Lmalicious"},
        {"destination_host": "host\nother"},
    ],
)
def test_invalid_external_forward_values_are_refused(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = dict(
        bind_address=IPv4Address("127.0.0.1"), local_port=12345, destination_host="localhost", destination_port=80
    )
    values.update(kwargs)
    with pytest.raises(ValidationError):
        LocalForward(**values)


def test_split_acknowledgment_and_idempotent_close(synthetic: SyntheticForwarding) -> None:
    synthetic.script = "os.write(1,marker[:5]); time.sleep(.03); os.write(1,marker[5:]); sys.stdin.buffer.read()"
    resource = synthetic.open()
    assert resource._thread.is_alive()
    assert synthetic.children[-1].poll() is None
    resource.close()
    resource.close()
    assert not resource._thread.is_alive()
    synthetic.assert_closed()


@pytest.mark.parametrize(
    "script",
    [
        "os.write(1,b'wrong\\n'); sys.stdin.buffer.read()",
        "os.write(1,b'noise'+marker); sys.stdin.buffer.read()",
        "os.write(1,b'x'*200000); sys.stdin.buffer.read()",
        "os.close(1); sys.stdin.buffer.read()",
        "sys.exit(255)",
    ],
)
def test_failed_acknowledgment_closes_resources(synthetic: SyntheticForwarding, script: str) -> None:
    synthetic.script = script
    with pytest.raises(ForwardingError):
        synthetic.open()
    synthetic.assert_closed()


@pytest.mark.parametrize("split", [0, 5, 51])
def test_acknowledgment_acceptance_is_independent_of_read_partition(synthetic: SyntheticForwarding, split: int) -> None:
    synthetic.script = (
        "data=marker+b'extra'; "
        + (
            f"os.write(1,data[:{split}]); time.sleep(.05); os.write(1,data[{split}:]); "
            if split
            else "os.write(1,data); "
        )
        + "sys.stdin.buffer.read()"
    )
    with synthetic.open() as resource:
        assert resource._thread.is_alive()
    synthetic.assert_closed()


def test_startup_deadline_is_not_resource_lifetime(synthetic: SyntheticForwarding) -> None:
    with synthetic.open(seconds=1) as resource:
        time.sleep(1.05)
        assert synthetic.children[-1].poll() is None
    assert not resource._thread.is_alive()
    synthetic.assert_closed()


def test_missing_acknowledgment_obeys_deadline(synthetic: SyntheticForwarding) -> None:
    synthetic.script = "sys.stdin.buffer.read()"
    started = time.monotonic()
    with pytest.raises(ForwardingError) as caught:
        synthetic.open(seconds=0.2)
    assert caught.value.failure == Failure.DEADLINE
    assert time.monotonic() - started < 2
    synthetic.assert_closed()


def test_empty_or_expired_request_never_dispatches(synthetic: SyntheticForwarding) -> None:
    with pytest.raises(ValidationError):
        open_local_forwards(synthetic.connection, [], deadline=Deadline.after(1))
    with pytest.raises(ForwardingError) as caught:
        synthetic.open(seconds=0)
    assert caught.value.failure == Failure.DEADLINE
    assert synthetic.calls == []


def test_version_probe_uses_startup_deadline(synthetic: SyntheticForwarding) -> None:
    synthetic.version = "import time; time.sleep(10)"
    with pytest.raises(ForwardingError) as caught:
        synthetic.open(seconds=0.2)
    assert caught.value.failure == Failure.DEADLINE
    assert len(synthetic.calls) == 1
    synthetic.assert_closed()


def test_diagnostics_drain_before_and_after_readiness(synthetic: SyntheticForwarding) -> None:
    synthetic.script = (
        "os.write(2,b'x'*200000); os.write(1,marker); time.sleep(.05); "
        "os.write(2,b'y'*200000); os.write(1,b'z'*200000); sys.exit(23)"
    )
    with synthetic.open() as resource:
        assert resource.wait() == 23
        assert resource._terminal is not None
        assert resource._terminal.exit_status == resource._terminal.local_status == 23
    synthetic.assert_closed()


def test_natural_exit_with_held_stdin_retains_natural_status(synthetic: SyntheticForwarding) -> None:
    synthetic.script = "os.write(1,marker); time.sleep(.05); sys.exit(23)"

    resource = synthetic.open()
    assert synthetic.children[-1].stdin is not None and not synthetic.children[-1].stdin.closed
    assert resource.wait() == 23
    assert resource._terminal is not None
    assert resource._terminal.exit_status == resource._terminal.local_status == 23
    assert resource._terminal.cleaned
    synthetic.assert_closed()


def test_forward_dispatch_failure_retains_only_safe_evidence(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = subprocess.Popen

    def fail_forward(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if argv[-1] == "-V":
            return spawn(argv, **kwargs)
        raise OSError("private-forward-dispatch-canary")

    monkeypatch.setattr(subprocess, "Popen", fail_forward)

    with pytest.raises(ForwardingError) as caught:
        synthetic.open()

    assert caught.value.failure == Failure.DISPATCH
    assert caught.value.local_status is None
    assert "private-forward-dispatch-canary" not in repr(caught.value)
    synthetic.assert_closed()


@pytest.mark.skipif(os.name == "nt", reason="waitpid ownership is POSIX-specific")
def test_lost_wait_status_never_becomes_forwarding_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(23)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.waitpid(process.pid, 0)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    owner = process_core.LocalProcessOwner()
    resource = forwarding.OwnedForwarding(owner, b"unused\n")
    resource._start(process_core.LocalProcessRequest(("unused",), True))

    with pytest.raises(ForwardingError) as caught:
        resource.wait()

    assert caught.value.failure == Failure.OBSERVATION
    assert caught.value.local_status is None
    assert not resource._thread.is_alive()
    assert resource._terminal is not None
    assert resource._terminal.observation_failed and not resource._terminal.cleaned
    assert all(pipe is None or pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))


def test_close_while_another_caller_waits_preserves_cleanup_evidence(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = synthetic.open()
    waiting = threading.Event()
    outcomes: list[int | BaseException] = []
    original_wait = resource._done.wait

    def observe_wait(timeout: float | None = None) -> bool:
        if threading.current_thread() is waiter:
            waiting.set()
        return original_wait(timeout)

    def wait_for_client() -> None:
        try:
            outcomes.append(resource.wait())
        except BaseException as error:
            outcomes.append(error)

    waiter = threading.Thread(target=wait_for_client, name="ssh-forwarding-wait-test")
    monkeypatch.setattr(resource._done, "wait", observe_wait)
    waiter.start()
    try:
        assert waiting.wait(timeout=2)
        assert synthetic.children[-1].poll() is None
        resource.close()
        waiter.join(timeout=2)
        assert not waiter.is_alive()
    finally:
        try:
            resource.close()
        finally:
            waiter.join(timeout=2)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert isinstance(outcome, ForwardingError)
    assert outcome.failure is Failure.OBSERVATION
    assert outcome.local_status == synthetic.children[-1].returncode
    assert not resource._thread.is_alive()
    synthetic.assert_closed()


def test_wait_interruption_closes_before_propagating(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = synthetic.open()

    def interrupt(timeout: float | None = None) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(resource._done, "wait", interrupt)
    with pytest.raises(KeyboardInterrupt):
        resource.wait()
    assert not resource._thread.is_alive()
    synthetic.assert_closed()


def test_startup_interruption_closes_before_propagating(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(self: forwarding.OwnedForwarding, deadline: Deadline) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(forwarding.OwnedForwarding, "_await_ready", interrupt)
    with pytest.raises(KeyboardInterrupt):
        synthetic.open()
    synthetic.assert_closed()


def test_read_error_closes_owned_client(synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch) -> None:
    resource = synthetic.open()

    def fail(fd: int, size: int) -> bytes:
        raise OSError("unretained diagnostic")

    monkeypatch.setattr(os, "read", fail)
    with pytest.raises(ForwardingError) as caught:
        resource.wait()
    assert caught.value.failure == Failure.OUTPUT
    assert resource._terminal is not None
    assert resource._terminal.exit_status is None
    assert resource._terminal.local_status is not None
    assert not resource._thread.is_alive()
    synthetic.assert_closed()


def test_owner_interruption_before_pipe_publication_retains_cleanup(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers: list[threading.Thread] = []
    original_start = threading.Thread.start

    def capture_start(worker: threading.Thread) -> None:
        workers.append(worker)
        original_start(worker)

    def interrupt_publication(
        owner: process_core.LocalProcessOwner,
        pipes: process_core.LocalProcessPipes,
    ) -> None:
        raise KeyboardInterrupt("owner-publication-boundary")

    monkeypatch.setattr(threading.Thread, "start", capture_start)
    monkeypatch.setattr(process_core.LocalProcessOwner, "_publish_ready", interrupt_publication)

    with pytest.raises(ForwardingError) as caught:
        synthetic.open()

    assert caught.value.failure == Failure.OBSERVATION
    assert all(not worker.is_alive() for worker in workers)
    synthetic.assert_closed()


def test_pipe_setup_error_closes_before_returning(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The version probe uses the same syscall; bypass only that completed check
    # to inject the failure at the forwarding process's own pipe boundary.
    monkeypatch.setattr(forwarding, "check_client_version", lambda *args, **kwargs: None)

    def fail(fd: int, blocking: bool) -> None:
        raise OSError("unretained diagnostic")

    monkeypatch.setattr(os, "set_blocking", fail)
    with pytest.raises(ForwardingError) as caught:
        synthetic.open()
    assert caught.value.failure == Failure.OBSERVATION
    synthetic.assert_closed()


def test_delayed_worker_exit_reports_uncertainty_without_losing_process_cleanup(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    blocked = threading.Event()
    original = forwarding.OwnedForwarding._read

    def delayed(self: forwarding.OwnedForwarding, pipe: Any) -> bytes | None:
        chunk = original(self, pipe)
        if self._ready.is_set() and not release.is_set():
            blocked.set()
            release.wait(timeout=5)
        return chunk

    synthetic.script = "os.write(1,marker); os.write(2,b'x'); sys.stdin.buffer.read()"
    monkeypatch.setattr(forwarding.OwnedForwarding, "_read", delayed)
    resource = synthetic.open()
    assert blocked.wait(timeout=2)
    started = time.monotonic()
    try:
        with pytest.raises(ForwardingError) as caught:
            resource.close()
        assert caught.value.failure == Failure.OBSERVATION
        assert time.monotonic() - started < 2
        assert resource._terminal is not None and resource._terminal.cleaned
        synthetic.assert_closed()
    finally:
        release.set()
        resource.close()
    assert not resource._thread.is_alive()
    synthetic.assert_closed()


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_setup_failure_releases_partial_owned_listener(synthetic: SyntheticForwarding) -> None:
    port = _unused_port()
    synthetic.script = (
        f"listener=socket.socket(); listener.bind(('127.0.0.1',{port})); listener.listen(); "
        "os.write(1,b'wrong\\n'); sys.stdin.buffer.read()"
    )
    with pytest.raises(ForwardingError):
        synthetic.open()
    synthetic.assert_closed()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


@pytest.mark.integration
def test_installed_ssh_forwards_bytes_and_releases_listener(local_sshd: SSHConnection) -> None:
    with socket.socket() as destination:
        destination.bind(("127.0.0.1", 0))
        destination.listen()
        destination.settimeout(5)
        received: list[bytes] = []

        def echo() -> None:
            peer, _ = destination.accept()
            with peer:
                peer.settimeout(5)
                received.append(peer.recv(100))
                peer.sendall(received[0])

        worker = threading.Thread(target=echo)
        worker.start()
        port = _unused_port()
        spec = LocalForward(IPv4Address("127.0.0.1"), port, "127.0.0.1", destination.getsockname()[1])
        try:
            with (
                open_local_forwards(local_sshd, [spec], deadline=Deadline.after(5)),
                socket.create_connection(("127.0.0.1", port), timeout=2) as peer,
            ):
                peer.sendall(b"\x00\xffowned-forward\n")
                assert peer.recv(100) == b"\x00\xffowned-forward\n"
        finally:
            worker.join(timeout=6)
        assert not worker.is_alive()
        assert received == [b"\x00\xffowned-forward\n"]
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))


@pytest.mark.integration
@pytest.mark.parametrize("occupied_first", [True, False])
def test_installed_ssh_partial_failure_releases_listeners(local_sshd: SSHConnection, occupied_first: bool) -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        free_port = _unused_port()
        specs = [_forward(free_port), _forward(occupied.getsockname()[1])]
        if occupied_first:
            specs.reverse()
        with pytest.raises(ForwardingError):
            open_local_forwards(local_sshd, specs, deadline=Deadline.after(5))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", free_port))


@pytest.mark.integration
def test_installed_ssh_readiness_does_not_claim_destination_health(local_sshd: SSHConnection) -> None:
    spec = LocalForward(IPv4Address("127.0.0.1"), _unused_port(), "127.0.0.1", _unused_port())
    with open_local_forwards(local_sshd, [spec], deadline=Deadline.after(5)) as resource:
        with socket.create_connection(("127.0.0.1", spec.local_port), timeout=2) as peer:
            assert peer.recv(1) == b""
        assert resource._owner.snapshot().exit_status is None


@pytest.mark.integration
def test_installed_ssh_ipv6_success_cannot_hide_ipv4_failure(local_sshd: SSHConnection) -> None:
    with socket.socket(socket.AF_INET6) as ipv6:
        try:
            ipv6.bind(("::1", 0))
        except OSError:
            pytest.skip("IPv6 loopback unavailable")
        ipv6_port = ipv6.getsockname()[1]
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        specs = [
            LocalForward(IPv6Address("::1"), ipv6_port, "::1", 80),
            _forward(occupied.getsockname()[1]),
        ]
        with pytest.raises(ForwardingError):
            open_local_forwards(local_sshd, specs, deadline=Deadline.after(5))
    with socket.socket(socket.AF_INET6) as released:
        released.bind(("::1", ipv6_port))


@pytest.mark.integration
@pytest.mark.parametrize("refusal", ["trust", "identity", "exec"])
def test_installed_ssh_refusal_releases_requested_port(local_sshd: SSHConnection, refusal: str) -> None:
    assert isinstance(local_sshd.trust, SSHTrustFiles)
    if refusal == "trust":
        local_sshd.trust.known_hosts[0].write_bytes(b"")
    else:
        authorized = local_sshd.identity_file.parent / "authorized"
        if refusal == "identity":
            authorized.write_bytes(b"")
        else:
            authorized.write_bytes(b'command="exit 1" ' + authorized.read_bytes())
    port = _unused_port()
    with pytest.raises(ForwardingError):
        open_local_forwards(local_sshd, [_forward(port)], deadline=Deadline.after(5))
    with socket.socket() as released:
        released.bind(("127.0.0.1", port))


@pytest.mark.parametrize("refusal", ["blocked", "corrupt"])
def test_each_forward_admits_current_managed_policy(
    synthetic: SyntheticForwarding, tmp_path: Path, refusal: str
) -> None:
    bundle = import_trust(tmp_path.resolve() / "managed", sources=synthetic.connection.trust, authority="fixture")
    synthetic.connection = replace(synthetic.connection, trust=bundle)
    first = resolve_trust(bundle)
    with synthetic.open():
        assert f'UserKnownHostsFile="{first.known_hosts[0].as_posix()}"' in synthetic.calls[-1]
    source = tmp_path.resolve() / "replacement"
    source.write_bytes(b"replacement policy")
    refresh_trust(
        bundle,
        sources=SSHTrustFiles((source,)),
        authority="fixture",
        expected_generation=trust_status(bundle).generation,
    )
    second = resolve_trust(bundle)
    with synthetic.open():
        assert f'UserKnownHostsFile="{second.known_hosts[0].as_posix()}"' in synthetic.calls[-1]
    assert first != second
    if refusal == "blocked":
        block_trust(bundle, expected_generation=trust_status(bundle).generation)
    else:
        second.known_hosts[0].write_bytes(b"corrupt policy")
    calls = len(synthetic.calls)
    with pytest.raises(ForwardingError) as error:
        synthetic.open()
    assert error.value.failure == Failure.DISPATCH
    assert len(synthetic.calls) == calls
    synthetic.assert_closed()


@pytest.mark.parametrize("stage", ["admission", "version"])
def test_forwarding_checks_expiry_after_local_work(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    original = admit_connection

    def admit(connection: SSHConnection) -> SSHTrustFiles:
        trust = original(connection)
        if stage == "admission":
            clock[0] = 2.0
        return trust

    def version(connection: SSHConnection, *, deadline: Deadline) -> None:
        clock[0] = 2.0

    monkeypatch.setattr(forwarding, "admit_connection", admit)
    monkeypatch.setattr(forwarding, "check_client_version", version)
    with pytest.raises(ForwardingError) as error:
        synthetic.open(seconds=1)
    assert error.value.failure == Failure.DEADLINE
    assert synthetic.calls == []


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_forwarding_admission_preserves_control_flow(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException]
) -> None:
    def interrupt(connection: SSHConnection) -> SSHTrustFiles:
        raise interruption()

    monkeypatch.setattr(forwarding, "admit_connection", interrupt)
    with pytest.raises(interruption):
        synthetic.open()
    assert synthetic.calls == []


@pytest.mark.parametrize("after_start", [False, True])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_thread_start_interruption_retains_worker_ownership(
    synthetic: SyntheticForwarding,
    monkeypatch: pytest.MonkeyPatch,
    after_start: bool,
    interruption: type[BaseException],
) -> None:
    original_start = threading.Thread.start
    original_drain = forwarding.OwnedForwarding._drain
    workers: list[threading.Thread] = []

    def delayed_drain(self: forwarding.OwnedForwarding) -> None:
        time.sleep(0.05)
        original_drain(self)

    def interrupt_start(self: threading.Thread) -> None:
        workers.append(self)
        if after_start:
            original_start(self)
        raise interruption()

    monkeypatch.setattr(threading.Thread, "start", interrupt_start)
    monkeypatch.setattr(forwarding.OwnedForwarding, "_drain", delayed_drain)
    try:
        with pytest.raises(interruption) as caught:
            synthetic.open()
        assert not any(worker.is_alive() for worker in workers)
        assert len(synthetic.calls) == 1
        if not after_start:
            assert caught.value.__notes__
        synthetic.assert_closed()
    finally:
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=2)


def test_ordinary_thread_start_failure_is_safe_observation(
    synthetic: SyntheticForwarding, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_start(worker: threading.Thread) -> None:
        raise RuntimeError("private-thread-start-canary")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(ForwardingError) as caught:
        synthetic.open()

    assert caught.value.failure == Failure.OBSERVATION
    assert "private-thread-start-canary" not in repr(caught.value)
    assert len(synthetic.calls) == 1
    synthetic.assert_closed()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_post_admission_interruption_never_admits_pipe_borrowing(
    synthetic: SyntheticForwarding,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    original_admit = process_core.LocalProcessOwner._admit
    original_read = forwarding.OwnedForwarding._read
    workers: list[threading.Thread] = []
    reads = 0

    def interrupt_after_admit(
        owner: process_core.LocalProcessOwner,
        request: process_core.LocalProcessRequest,
    ) -> bool:
        original_admit(owner, request)
        raise interruption("post-admission-boundary")

    def count_read(self: forwarding.OwnedForwarding, pipe: Any) -> bytes | None:
        nonlocal reads
        reads += 1
        return original_read(self, pipe)

    original_start = threading.Thread.start

    def capture_start(worker: threading.Thread) -> None:
        workers.append(worker)
        original_start(worker)

    monkeypatch.setattr(process_core.LocalProcessOwner, "_admit", interrupt_after_admit)
    monkeypatch.setattr(forwarding.OwnedForwarding, "_read", count_read)
    monkeypatch.setattr(threading.Thread, "start", capture_start)

    with pytest.raises(interruption, match="post-admission-boundary"):
        synthetic.open()

    assert reads == 0
    assert all(not worker.is_alive() for worker in workers)
    synthetic.assert_closed()


def test_repeated_close_interruptions_preserve_first_and_finish_cleanup(
    synthetic: SyntheticForwarding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_drain = forwarding.OwnedForwarding._drain
    original_join = threading.Thread.join
    first = KeyboardInterrupt("first-close-control")
    second = SystemExit("second-close-control")
    interruptions = iter((first, second))

    def delayed_return(self: forwarding.OwnedForwarding) -> None:
        original_drain(self)
        time.sleep(0.05)

    def interrupt_join(worker: threading.Thread, timeout: float | None = None) -> None:
        if worker.name == "ssh-forwarding":
            try:
                raise next(interruptions)
            except StopIteration:
                pass
        original_join(worker, timeout)

    monkeypatch.setattr(forwarding.OwnedForwarding, "_drain", delayed_return)
    monkeypatch.setattr(threading.Thread, "join", interrupt_join)
    resource = synthetic.open()

    with pytest.raises(KeyboardInterrupt) as caught:
        resource.close()

    assert caught.value is first
    resource.close()
    assert resource._terminal is not None and resource._terminal.cleaned
    assert not resource._thread.is_alive()
    synthetic.assert_closed()
