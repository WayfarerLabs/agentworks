"""Real local Unix sockets, process pipes and protected-store relay refusals."""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_live_guest as guest
from agentworks.execution._managed_job_request import ManagedJobRequest, ManagedLiveIO
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, StoreError
from agentworks.execution._managed_lease_wire import sampled_lease
from agentworks.execution._managed_live_bundle import ROOT_PROGRAM
from agentworks.execution._managed_live_protocol import (
    HANDSHAKE_BYTES,
    LIVE_SOCKET_NAME,
    ManagedLiveError,
    ManagedLiveRequest,
    encode_envelope,
    encode_handshake,
)

from .test_managed_live_protocol import GUEST, NONCE, RUN, _request

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux protected store and procfs sockets")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ManagedJobStore]:
    if sys.platform != "linux":
        pytest.skip("requires Linux protected store and procfs sockets")
    os.chmod(tmp_path, 0o700)
    anchor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with ManagedJobStore(RUN, _namespace="managed", _anchor_fd=anchor, _owner_uid=os.getuid()) as target:
            yield target
    finally:
        os.close(anchor)


def _publish(store: ManagedJobStore, request: ManagedLiveRequest) -> None:
    store.publish_request(
        ManagedJobRequest(
            request.expected_launch,
            "command",
            ("/bin/true",),
            None,
            "discard",
            None,
            (),
            b"",
            b"" if request.live_io.input else b"finite-workload-input",
            sampled_lease(request.expected_launch, 1),
            live_io=request.live_io,
        )
    )
    store.publish_fact(FactName.LAUNCH, request.expected_launch)


def _listener(store: ManagedJobStore) -> socket.socket:
    directory = store._run_dir(create=True)
    assert directory is not None
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(f"/proc/self/fd/{directory}/{LIVE_SOCKET_NAME}")
        os.chmod(LIVE_SOCKET_NAME, 0o600, dir_fd=directory)
        listener.listen(1)
        listener.settimeout(5)
        return listener
    except BaseException:
        listener.close()
        raise
    finally:
        os.close(directory)


def _source() -> str:
    """Exercise exact packed code with explicit local identity/store seams."""
    return ROOT_PROGRAM.loader_source + (
        "_agw_load_identity()\n_agw_load_remaining()\n"
        "g=sys.modules['_agw_managed_live._managed_live_guest']\n"
        "s=sys.modules['_agw_managed_live._managed_job_store']\n"
        "v=sys.modules['_agw_managed_live._vm_guest_identity_protocol']\n"
        "g.matches_current_identity=lambda expected:True\n"
        f"g._identity=lambda:v.VMGuestIdentity({GUEST.instance_marker!r},{GUEST.boot_id!r},{GUEST.init_start_ticks})\n"
        "anchor=os.open(sys.argv[2],os.O_RDONLY|os.O_DIRECTORY)\n"
        "g.ManagedJobStore=lambda run_id:s.ManagedJobStore(run_id,_namespace='managed',"
        "_anchor_fd=anchor,_owner_uid=os.getuid())\n"
        "try:result=g.main(sys.argv[1])\n"
        "finally:os.close(anchor)\n"
        "raise SystemExit(result)\n"
    )


def _process(tmp_path: Path, nonce: str = NONCE, interpreter: str = sys.executable) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [interpreter, "-I", "-S", "-B", "-c", _source(), nonce, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
    )


def _serve(
    listener: socket.socket, action: Callable[[socket.socket], None]
) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(5)
                action(connection)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, errors


def _read_all(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while chunk := connection.recv(65536):
        chunks.extend(chunk)
    return bytes(chunks)


def _handshake(connection: socket.socket, request: ManagedLiveRequest) -> None:
    data = bytearray()
    while len(data) < HANDSHAKE_BYTES:
        data.extend(connection.recv(HANDSHAKE_BYTES - len(data)))
    assert bytes(data) == encode_handshake(request.live_io)


@pytest.mark.parametrize(("live_input", "live_output"), [(True, True), (True, False), (False, True)])
def test_packed_relay_exact_envelope_binary_duplex_and_finite_eof(
    store: ManagedJobStore, tmp_path: Path, live_input: bool, live_output: bool
) -> None:
    request = _request(input=live_input, output=live_output)
    _publish(store, request)
    payload = b"private-input-canary\0\xff" + bytes(range(256)) * 2048 if live_input else b""
    output = (
        b"raw-controller-bytes\0\xff" + bytes(reversed(range(256))) * 2048 if live_output else b"binding-and-finished"
    )
    received: list[bytes] = []

    def action(connection: socket.socket) -> None:
        _handshake(connection, request)
        # Output pressure and input pressure overlap. The relay must progress both.
        connection.sendall(output)
        received.append(_read_all(connection))
        connection.sendall(b"after-source-eof")

    with _listener(store) as listener:
        thread, errors = _serve(listener, action)
        with _process(tmp_path) as process:
            assert "private-input-canary" not in repr(process.args)
            stdout, stderr = process.communicate(ROOT_PROGRAM.prefix + encode_envelope(request) + payload, timeout=10)
        thread.join(6)
        assert not thread.is_alive() and not errors
    assert process.returncode == 0 and stderr == b""
    assert received == [payload]
    assert stdout == output + b"after-source-eof"


def test_peer_consumer_closure_retires_open_source_and_continues_output(store: ManagedJobStore, tmp_path: Path) -> None:
    request = _request()
    _publish(store, request)
    received_handshake = threading.Event()
    release_output = threading.Event()

    def action(connection: socket.socket) -> None:
        _handshake(connection, request)
        connection.shutdown(socket.SHUT_RD)
        received_handshake.set()
        assert release_output.wait(5)
        connection.sendall(b"output-after-consumer-closed\0\xff")
        # Discard bytes queued before SHUT_RD to permit a clean final peer close.
        _read_all(connection)

    with _listener(store) as listener:
        thread, errors = _serve(listener, action)
        with _process(tmp_path) as process:
            assert process.stdin is not None
            process.stdin.write(ROOT_PROGRAM.prefix + encode_envelope(request))
            process.stdin.flush()
            assert received_handshake.wait(5)
            try:
                while True:
                    process.stdin.write(b"input-until-consumer-closes" * 1024)
                    process.stdin.flush()
            except BrokenPipeError:
                pass
            release_output.set()
            process.stdin.close()
            process.stdin = None
            stdout, stderr = process.communicate(timeout=10)
        thread.join(6)
        assert not thread.is_alive() and not errors
    assert process.returncode == 0 and stderr == b""
    assert stdout == b"output-after-consumer-closed\0\xff"


@pytest.mark.parametrize("variant", ["wrong-nonce", "truncated", "noncanonical", "unexpected-suffix"])
def test_invalid_envelope_never_connects(store: ManagedJobStore, tmp_path: Path, variant: str) -> None:
    request = _request(input=variant != "unexpected-suffix")
    _publish(store, request)
    envelope = encode_envelope(request)
    if variant == "truncated":
        envelope = envelope[:-1]
    elif variant == "noncanonical":
        body = envelope[13:] + b" "
        envelope = b"AGWR1" + f"{len(body):08x}".encode() + body
    elif variant == "unexpected-suffix":
        envelope += b"forbidden-application-bytes"
    with _listener(store) as listener:
        listener.setblocking(False)
        with _process(tmp_path, "f" * 32 if variant == "wrong-nonce" else NONCE) as process:
            stdout, stderr = process.communicate(ROOT_PROGRAM.prefix + envelope, timeout=10)
        with pytest.raises(BlockingIOError):
            listener.accept()
    assert process.returncode == 125 and stdout == stderr == b""


@pytest.mark.parametrize("variant", ["malformed-control", "missing-asset"])
def test_actual_corrupt_store_refuses_before_connect(store: ManagedJobStore, tmp_path: Path, variant: str) -> None:
    request = _request()
    _publish(store, request)
    path = tmp_path / "managed" / RUN / "request-control"
    if variant == "malformed-control":
        path.chmod(0o600)
        path.write_bytes(b"private-corrupt-control")
        path.chmod(0o400)
    else:
        path.unlink()
    with _listener(store) as listener:
        listener.setblocking(False)
        with _process(tmp_path) as process:
            stdout, stderr = process.communicate(ROOT_PROGRAM.prefix + encode_envelope(request), timeout=10)
        with pytest.raises(BlockingIOError):
            listener.accept()
    assert process.returncode == 125 and stdout == stderr == b""


@pytest.mark.parametrize("variant", ["absent", "request-mismatch", "launch-mismatch", "stop", "terminal", "disposal"])
def test_protected_store_refusals_before_connect(
    store: ManagedJobStore, tmp_path: Path, variant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    if variant != "absent":
        _publish(store, request)
    if variant == "request-mismatch":
        store_request = store.read_request()
        assert store_request is not None
        monkeypatch.setattr(
            store, "read_request", lambda: replace(store_request, live_io=ManagedLiveIO("f" * 32, True, True))
        )
    if variant == "launch-mismatch":
        monkeypatch.setattr(store, "read_fact", lambda _name: b"different-launch")
    if variant == "stop":
        store.publish_stop_request(request.expected_launch)
    if variant == "terminal":
        store.publish_fact(
            FactName.WAIT,
            wire.encode_fact(
                {
                    "version": 1,
                    "kind": "wait",
                    "run_id": RUN,
                    "unit": f"agw-managed-{RUN}.service",
                    "receipt_sha256": wire.launch_sha256(wire.decode_fact(request.expected_launch)),
                    "exit_code": 0,
                    "signal": None,
                }
            ),
        )
    with _listener(store):
        if variant == "disposal":
            directory = store._run_dir(create=False)
            assert directory is not None
            try:
                fd = os.open("disposal", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=directory)
                os.close(fd)
            finally:
                os.close(directory)
        with pytest.raises((ManagedLiveError, StoreError)):
            guest._prepare(request, store)


@pytest.mark.parametrize("variant", ["regular", "symlink", "mode", "hardlink"])
def test_endpoint_type_mode_and_link_refusals(store: ManagedJobStore, variant: str) -> None:
    request = _request()
    _publish(store, request)
    directory = store._run_dir(create=False)
    assert directory is not None
    try:
        with _listener(store):
            if variant == "regular":
                os.unlink(LIVE_SOCKET_NAME, dir_fd=directory)
                fd = os.open(LIVE_SOCKET_NAME, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory)
                os.close(fd)
            elif variant == "symlink":
                os.rename(LIVE_SOCKET_NAME, "other", src_dir_fd=directory, dst_dir_fd=directory)
                os.symlink("other", LIVE_SOCKET_NAME, dir_fd=directory)
            elif variant == "mode":
                os.chmod(LIVE_SOCKET_NAME, 0o666, dir_fd=directory)
            else:
                os.link(LIVE_SOCKET_NAME, "other", src_dir_fd=directory, dst_dir_fd=directory)
            with pytest.raises(ManagedLiveError):
                guest._prepare(request, store)
    finally:
        os.close(directory)


@pytest.mark.parametrize("fault", [OSError(errno.EIO, "synthetic sink fault"), KeyboardInterrupt()])
def test_relay_preserves_nonbroken_fault_and_closes_owned_input(
    fault: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, producer = os.pipe()
    sink_reader, sink = os.pipe()
    before = len(os.listdir("/proc/self/fd"))
    try:
        relay, peer = socket.socketpair()
        with relay, peer:
            peer.sendall(b"pending-output")

            def write(_fd: int, _data: bytes) -> int:
                raise fault

            monkeypatch.setattr(os, "write", write)
            with pytest.raises(type(fault)) as caught:
                guest._relay(relay, live_input=True, expires=time.monotonic() + 30, input_fd=source, output_fd=sink)
            assert caught.value is fault
            with pytest.raises(OSError) as closed:
                os.fstat(source)
            assert closed.value.errno == errno.EBADF
        assert len(os.listdir("/proc/self/fd")) == before - 1
    finally:
        with suppress(OSError):
            os.close(source)
        os.close(producer)
        os.close(sink_reader)
        os.close(sink)


def test_detachment_never_reconnects_replays_or_requests_stop(store: ManagedJobStore, tmp_path: Path) -> None:
    request = _request()
    _publish(store, request)

    def action(connection: socket.socket) -> None:
        _handshake(connection, request)
        assert connection.recv(1) == b"x"
        # Close with unread queued source bytes. A reset must remain a relay fault.

    with _listener(store) as listener:
        thread, errors = _serve(listener, action)
        with _process(tmp_path) as process:
            stdout, stderr = process.communicate(
                ROOT_PROGRAM.prefix + encode_envelope(request) + b"x" * 524288, timeout=10
            )
        thread.join(6)
        assert not thread.is_alive() and not errors
        listener.setblocking(False)
        with pytest.raises(BlockingIOError):
            listener.accept()
    assert process.returncode == 125 and stdout == stderr == b""
    assert not store.read_stop_request()
    assert store.read_fact(FactName.LAUNCH) == request.expected_launch
    assert store.read_fact(FactName.WAIT) is None


@pytest.mark.parametrize("resume_after", [None, 5.1])
def test_stalled_output_after_peer_eof_obeys_original_observation_budget(
    store: ManagedJobStore, tmp_path: Path, resume_after: float | None
) -> None:
    import fcntl

    request = replace(_request(input=False), remaining_ns=200_000_000 if resume_after is None else 10_000_000_000)
    _publish(store, request)

    def action(connection: socket.socket) -> None:
        _handshake(connection, request)
        connection.sendall(b"x" * 65536)
        assert _read_all(connection) == b""

    with _listener(store) as listener:
        thread, errors = _serve(listener, action)
        with _process(tmp_path) as process:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            # Keep the sink open and unread after the controller has sent all output
            # and closed. Pending output must still have a finite observation bound.
            fcntl.fcntl(process.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            try:
                process.stdin.write(ROOT_PROGRAM.prefix + encode_envelope(request))
                process.stdin.close()
                process.stdin = None
                thread.join(1)
                assert not thread.is_alive() and not errors
                if resume_after is None:
                    assert process.wait(timeout=1) == 125
                    assert process.stderr.read() == b""
                else:
                    # A drained sink may resume beyond five seconds after peer EOF
                    # when the original caller observation budget still permits it.
                    time.sleep(resume_after)
                    stdout, stderr = process.communicate(timeout=3)
                    assert process.returncode == 0 and stderr == b""
                    assert stdout == b"x" * 65536
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
    assert not store.read_stop_request()
    assert store.read_fact(FactName.WAIT) is None


@pytest.mark.parametrize("variant", ["identity", "guest", "nonce"])
def test_main_identity_and_nonce_fail_before_store(monkeypatch: pytest.MonkeyPatch, variant: str) -> None:
    monkeypatch.setattr(guest, "_read_request", _request)
    monkeypatch.setattr(guest, "matches_current_identity", lambda _expected: variant != "identity")
    monkeypatch.setattr(guest, "_identity", lambda: replace(GUEST, init_start_ticks=9) if variant == "guest" else GUEST)

    def forbidden_store(_run_id: str) -> None:
        pytest.fail("store opened before identity was validated")

    monkeypatch.setattr(guest, "ManagedJobStore", forbidden_store)
    assert guest.main("f" * 32 if variant == "nonce" else NONCE) == 125


def test_control_exception_identity_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    control = KeyboardInterrupt()

    def interrupt() -> ManagedLiveRequest:
        raise control

    monkeypatch.setattr(guest, "_read_request", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        guest.main(NONCE)
    assert caught.value is control


def test_setup_deadline_and_failed_connection_close_owned_socket(store: ManagedJobStore) -> None:
    with pytest.raises(ManagedLiveError):
        guest._ready(0, 1, -1)
    request = _request()
    _publish(store, request)
    directory = store._run_dir(create=False)
    assert directory is not None
    before = set(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(OSError) as caught:
            guest._connect(directory, request, expires=time.monotonic() + 30)
        assert caught.value.errno == errno.ENOENT
        assert set(os.listdir("/proc/self/fd")) == before
    finally:
        os.close(directory)
