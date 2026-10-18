"""Real local socket/fork/exec checks, with simulated boundary and identity admission."""

from __future__ import annotations

import hashlib
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest

from agentworks.execution import _helper_identity
from agentworks.execution import _managed_job_request as requests
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_lease_wire as leases
from agentworks.execution import _managed_live_io as live_wire
from agentworks.execution import _managed_service_bundle as bundle
from agentworks.execution import _managed_service_guest as guest
from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._inline_control import StreamName, parse_stream_end
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, StoreError, Stream

from .test_managed_service_guest import RUN, _launch, _store

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux controller")
NONCE = "b" * 32


class Boundary:
    """Simulate empty observation and exact anchor killing, never native cgroups."""

    def __init__(self) -> None:
        self.pid = 0
        self.killed = False

    def place(self, pid: int) -> None:
        self.pid = pid

    def kill(self) -> None:
        self.killed = True
        with suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)

    def empty(self) -> bool:
        return self.killed


def _request(
    *,
    input: bool = True,
    output: bool = True,
    argv: tuple[str, ...] = ("/bin/cat",),
    stdin: bytes = b"",
    mode: str = "discard",
) -> requests.ManagedJobRequest:
    launch = wire.decode_fact(_launch())
    launch.update(owner={"kind": "operation", "owner_id": "c" * 32}, lifetime="operation")
    data = wire.encode_fact(launch)
    return requests.ManagedJobRequest(
        data,
        "command",
        argv,
        None,
        mode,
        31 if mode == "capture" else None,
        (),
        b"",
        stdin,
        leases.sampled_lease(data, leases.boottime_ns()),
        live_io=requests.ManagedLiveIO(NONCE, input, output),
    )


def _handshake(request: requests.ManagedJobRequest) -> bytes:
    assert request.live_io is not None
    return f"AGWL1 {NONCE} {int(request.live_io.input)} {int(request.live_io.output)}\n".encode()


def _frames(client: socket.socket) -> list[Frame]:
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    while chunk := client.recv(65536):
        remaining = memoryview(chunk)
        while remaining:
            remaining = remaining[reader.try_write(remaining) :]
    reader.finish()
    assert reader.error is None
    return frames


def _execute(
    tmp_path: Path,
    request: requests.ManagedJobRequest,
    peer: Callable[[socket.socket, ManagedJobStore], None] | None,
) -> ManagedJobStore:
    store = _store(tmp_path)
    store.publish_request(request)
    errors: list[BaseException] = []
    worker: threading.Thread | None = None

    def notify() -> None:
        nonlocal worker
        assert store.read_fact(FactName.LAUNCH) == request.launch
        if peer is None:
            return

        def connect() -> None:
            directory = store.open_live_directory()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(5)
                    client.connect(f"/proc/self/fd/{directory}/{live_wire.LIVE_SOCKET_NAME}")
                    peer(client, store)
            except BaseException as exc:
                errors.append(exc)
            finally:
                os.close(directory)

        worker = threading.Thread(target=connect, daemon=True)
        worker.start()

    boundary = Boundary()
    result = guest.run(
        RUN, _store=store, _boundary=boundary, _notify=notify, _identity_check=False, _apply_identity=False
    )
    if worker is not None:
        worker.join(6)
        assert not worker.is_alive()
    assert not errors, errors
    assert result == 0
    assert boundary.killed
    assert store.read_fact(FactName.BOUNDARY_EMPTY) is not None
    return store


@pytest.mark.parametrize("code", [0, 1, 255])
def test_bidirectional_binary_eof_and_presentation_status(tmp_path: Path, code: int) -> None:
    payload = b"\0\xffABC" * 20000
    request = _request(argv=("/bin/sh", "-c", f"cat; printf '\\377err' >&2; exit {code}"))
    frames: list[Frame] = []

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request) + payload)
        client.shutdown(socket.SHUT_WR)
        frames.extend(_frames(client))

    with _execute(tmp_path, request, peer) as store:
        assert frames[0].kind is FrameKind.LAUNCHING
        assert frames[0].body == request.launch
        assert frames[-1].kind is FrameKind.FINISHED
        assert not any(frame.kind is FrameKind.WAITED for frame in frames)
        for name, expected in ((StreamName.STDOUT, payload), (StreamName.STDERR, b"\xfferr")):
            kind = FrameKind.STDOUT if name is StreamName.STDOUT else FrameKind.STDERR
            assert b"".join(frame.body for frame in frames if frame.kind is kind) == expected
            ends = [parse_stream_end(frame.body) for frame in frames if frame.kind is FrameKind.STREAM_END]
            end = next(end for end in ends if end.stream is name)
            assert end.count == len(expected)
            assert end.sha256 == hashlib.sha256(expected).hexdigest()
            assert end.complete and not end.truncated
        assert wire.decode_fact(store.read_fact(FactName.WAIT))["exit_code"] == code
        directory = tmp_path / "managed" / RUN
        for stream in Stream:
            assert not (directory / stream.value).exists()
            assert store.read_capture_range(stream, request.launch, offset=0, count=31) is None
        assert store.dispose(request.launch)
        assert sorted(path.name for path in directory.iterdir()) == ["disposal"]


@pytest.mark.parametrize("mode", ["capture", "discard", "sensitivity-suppressed"])
def test_input_only_preserves_retention_and_omits_presentation(tmp_path: Path, mode: str) -> None:
    payload = b"input-prefix" * 10000
    request = _request(output=False, mode=mode)
    frames: list[Frame] = []

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request) + payload)
        client.shutdown(socket.SHUT_WR)
        frames.extend(_frames(client))

    with _execute(tmp_path, request, peer) as store:
        assert [frame.kind for frame in frames] == [FrameKind.LAUNCHING, FrameKind.FINISHED]
        assert store.read_capture_range(Stream.STDOUT, request.launch, offset=0, count=32) == (
            payload[:31] if mode == "capture" else None
        )


def test_output_only_relay_eof_does_not_replace_finite_stdin(tmp_path: Path) -> None:
    payload = b"finite\0\xff" * 20000
    request = _request(input=False, stdin=payload)
    frames: list[Frame] = []

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request))
        client.shutdown(socket.SHUT_WR)
        frames.extend(_frames(client))

    with _execute(tmp_path, request, peer):
        assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == payload
        assert frames[-1].kind is FrameKind.FINISHED


@pytest.mark.parametrize("attach", ["missing", "partial", "wrong", "directions", "expired"])
def test_one_use_admission_refuses_without_application_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attach: str,
) -> None:
    monkeypatch.setattr(live_wire, "_ADMISSION_SECONDS", 0.15)
    marker = tmp_path / "entered"
    request = _request(argv=("/bin/sh", "-c", f"printf entered > {marker}"))

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        if attach == "partial":
            client.sendall(_handshake(request)[:15])
        elif attach == "wrong":
            client.sendall(_handshake(request).replace(b"b", b"c"))
        elif attach == "directions":
            client.sendall(_handshake(request)[:-4] + b"0 1\n")
        elif attach == "expired":
            time.sleep(0.2)
            with suppress(BrokenPipeError):
                client.sendall(_handshake(request))
        assert client.recv(4096) == b""
        if attach in ("wrong", "directions"):
            directory = _store.open_live_directory()
            try:
                with (
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as retry,
                    pytest.raises(ConnectionRefusedError),
                ):
                    retry.connect(f"/proc/self/fd/{directory}/{live_wire.LIVE_SOCKET_NAME}")
            finally:
                os.close(directory)

    with _execute(tmp_path, request, None if attach == "missing" else peer) as store:
        assert not marker.exists()
        assert store.read_fact(FactName.WAIT) is None


def test_open_source_prefix_runs_before_source_eof(tmp_path: Path) -> None:
    request = _request(argv=("/bin/sh", "-c", "dd bs=1 count=1 2>/dev/null; exit 1"))
    frames: list[Frame] = []

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request) + b"p")
        frames.extend(_frames(client))

    with _execute(tmp_path, request, peer) as store:
        assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == b"p"
        assert wire.decode_fact(store.read_fact(FactName.WAIT))["exit_code"] == 1


def test_workload_input_closure_signals_broken_pipe_but_output_continues(tmp_path: Path) -> None:
    request = _request(argv=("/bin/sh", "-c", "exec 0<&-; sleep 0.15; printf output"))
    frames: list[Frame] = []
    broken = threading.Event()

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request))

        def source() -> None:
            try:
                while True:
                    client.sendall(b"x" * 65536)
            except BrokenPipeError:
                broken.set()

        sender = threading.Thread(target=source, daemon=True)
        sender.start()
        frames.extend(_frames(client))
        sender.join(2)
        assert not sender.is_alive()

    with _execute(tmp_path, request, peer):
        assert broken.is_set()
        assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == b"output"
        assert frames[-1].kind is FrameKind.FINISHED


def test_disconnected_consumer_keeps_entered_job_and_durable_capture(tmp_path: Path) -> None:
    marker = tmp_path / "completed"
    request = _request(output=False, mode="capture", argv=("/bin/sh", "-c", f"sleep 0.1; printf done; touch {marker}"))

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request))
        # Wait for the complete binding, proving release before observation loss.
        data = b""
        while not data.endswith(b"\n"):
            data += client.recv(8192)

    with _execute(tmp_path, request, peer) as store:
        assert marker.exists()
        assert store.read_capture_range(Stream.STDOUT, request.launch, offset=0, count=31) == b"done"
        assert wire.decode_fact(store.read_fact(FactName.WAIT))["exit_code"] == 0


@pytest.mark.parametrize("cause", ["stop", "lease"])
@pytest.mark.parametrize("stage", ["handshake", "output"])
def test_supervision_progresses_under_socket_stalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
    stage: str,
) -> None:
    monkeypatch.setattr(guest, "_STOP_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(guest, "_CLEANUP_SECONDS", 0.5)
    if cause == "lease":
        monkeypatch.setattr(leases, "WINDOW_NS", 300_000_000)
    marker = tmp_path / "entered"
    request = _request(argv=("/bin/sh", "-c", f"printf entered > {marker}; exec /usr/bin/yes"))

    def peer(client: socket.socket, store: ManagedJobStore) -> None:
        client.sendall(_handshake(request) if stage == "output" else b"AGW")
        time.sleep(0.1)
        if cause == "stop":
            store.publish_stop_request(request.launch)
        time.sleep(0.6)

    started = time.monotonic()
    with _execute(tmp_path, request, peer) as store:
        assert time.monotonic() - started < 2
        assert marker.exists() == (stage == "output")
        if stage == "handshake":
            assert store.read_fact(FactName.WAIT) is None


def test_fixed_socket_create_only_and_closed_controller_descriptors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    request = _request()
    store.publish_request(request)
    assert request.live_io is not None
    before = set(os.listdir("/proc/self/fd"))
    live = live_wire.LiveIO(store, request.live_io, request.launch)
    assert (tmp_path / "managed" / RUN / live_wire.LIVE_SOCKET_NAME).is_socket()
    with pytest.raises(OSError):
        live_wire.LiveIO(store, request.live_io, request.launch)
    live.close()
    assert set(os.listdir("/proc/self/fd")) == before
    store.close()


def test_shutdown_read_drains_prior_bytes_without_resetting_output() -> None:
    consumer, producer = socket.socketpair()
    with consumer, producer:
        producer.sendall(b"queued input")
        consumer.shutdown(socket.SHUT_RD)
        with pytest.raises(BrokenPipeError):
            producer.sendall(b"later input")
        consumer.setblocking(False)
        assert consumer.recv(65536) == b"queued input"
        assert consumer.recv(65536) == b""
        consumer.sendall(b"remaining output")
        consumer.close()
        assert producer.recv(65536) == b"remaining output"
        assert producer.recv(65536) == b""


def test_child_closes_protected_live_descriptors_before_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "entered"
    request = _request(argv=("/bin/sh", "-c", f"touch {marker}"))

    def identity(_expected: object) -> bool:
        for name in os.listdir("/proc/self/fd"):
            try:
                path = os.readlink("/proc/self/fd/" + name)
            except FileNotFoundError:
                continue
            if path.startswith("socket:") or path.endswith("/managed/" + RUN):
                return False
        return True

    monkeypatch.setattr(_helper_identity, "matches_current_identity", identity)

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request))
        assert _frames(client)[-1].kind is FrameKind.FINISHED

    with _execute(tmp_path, request, peer):
        assert marker.exists()


def test_lost_output_consumer_drains_without_spool_or_implicit_stop(tmp_path: Path) -> None:
    marker = tmp_path / "completed"
    request = _request(input=False, argv=("/bin/sh", "-c", f"head -c 524288 /dev/zero; touch {marker}; exit 255"))

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request))
        data = b""
        while not data.endswith(b"\n"):
            data += client.recv(1)

    with _execute(tmp_path, request, peer) as store:
        assert marker.exists()
        assert wire.decode_fact(store.read_fact(FactName.WAIT))["exit_code"] == 255
        assert store.read_fact(FactName.STDOUT_END) is not None
        assert not (tmp_path / "managed" / RUN / "stdout").exists()
        assert not (tmp_path / "managed" / RUN / "stderr").exists()


def test_unread_open_source_does_not_delay_known_exit(tmp_path: Path) -> None:
    request = _request(argv=("/bin/sh", "-c", "printf no-read; exit 255"))
    frames: list[Frame] = []

    def peer(client: socket.socket, _store: ManagedJobStore) -> None:
        client.sendall(_handshake(request) + b"offered input")
        frames.extend(_frames(client))

    with _execute(tmp_path, request, peer) as store:
        assert wire.decode_fact(store.read_fact(FactName.WAIT))["exit_code"] == 255
        assert b"".join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT) == b"no-read"
        assert frames[-1].kind is FrameKind.FINISHED


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_stdlib_bundle_actual_socket_fork_and_exec(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    request = _request()
    loader = build_helper_modules("_test_managed_live", bundle._MODULES)
    source = loader + textwrap.dedent(f"""
        import hashlib,os,socket,threading
        from contextlib import suppress
        from _test_managed_live import _managed_service_guest as guest
        from _test_managed_live import _managed_job_request as requests
        from _test_managed_live import _managed_lease_wire as leases
        from _test_managed_live._managed_job_store import ManagedJobStore,FactName
        from _test_managed_live._evidence_wire import FrameReader,FrameKind
        os.chmod({str(tmp_path)!r},0o700)
        anchor=os.open({str(tmp_path)!r},os.O_RDONLY|os.O_DIRECTORY)
        store=ManagedJobStore({RUN!r},_anchor_fd=anchor,_namespace='managed',_owner_uid=os.getuid())
        os.close(anchor)
        launch={request.launch!r}
        request=requests.ManagedJobRequest(launch,'command',('/bin/sh','-c','cat; exit 255'),None,
            'discard',None,(),b'',b'',leases.sampled_lease(launch,leases.boottime_ns()),
            live_io=requests.ManagedLiveIO({NONCE!r},True,True))
        store.publish_request(request)
        class Boundary:
            def place(self,pid): self.pid=pid
            def kill(self):
                with suppress(ProcessLookupError): os.kill(self.pid,9)
            def empty(self): return True
        frames=[]
        errors=[]
        workers=[]
        def notify():
            def peer():
                directory=store.open_live_directory()
                try:
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                        client.settimeout(5)
                        client.connect('/proc/self/fd/'+str(directory)+'/live-io.sock')
                        client.sendall(b'AGWL1 '+{NONCE.encode()!r}+b' 1 1\\n'+b'\\0\\xff'*10000)
                        client.shutdown(socket.SHUT_WR)
                        reader=FrameReader({NONCE!r},frames.append)
                        while chunk:=client.recv(65536):
                            view=memoryview(chunk)
                            while view: view=view[reader.try_write(view):]
                        reader.finish()
                        assert reader.error is None
                except BaseException as exc: errors.append(type(exc).__name__)
                finally: os.close(directory)
            worker=threading.Thread(target=peer,daemon=True)
            workers.append(worker)
            worker.start()
        assert guest.run({RUN!r},_store=store,_boundary=Boundary(),_notify=notify,
            _identity_check=False,_apply_identity=False)==0
        workers[0].join(6)
        assert not workers[0].is_alive() and not errors,errors
        assert frames[0].kind is FrameKind.LAUNCHING and frames[0].body==launch
        assert frames[-1].kind is FrameKind.FINISHED
        assert b''.join(frame.body for frame in frames if frame.kind is FrameKind.STDOUT)==b'\\0\\xff'*10000
        assert store.read_fact(FactName.WAIT) is not None
        for name in ('stdout','stderr'):
            assert not os.path.exists({str(tmp_path)!r}+'/managed/'+{RUN!r}+'/'+name)
        store.close()
        """)
    completed = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source], capture_output=True, check=False, timeout=10
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == completed.stderr == b""


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
@pytest.mark.parametrize("selector_name", ["DefaultSelector", "SelectSelector"])
def test_continuous_stdout_cannot_starve_stderr_needed_for_exit(
    interpreter: str,
    selector_name: str,
    tmp_path: Path,
) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    request = _request(input=False)
    workload = textwrap.dedent("""
        import os,threading
        def stdout():
            while True:
                os.write(1,b'o'*1048576)
        threading.Thread(target=stdout,daemon=True).start()
        remaining=262144
        while remaining:
            remaining-=os.write(2,b'e'*remaining)
        os._exit(0)
        """)
    loader = build_helper_modules("_test_live_fairness", bundle._MODULES)
    source = loader + textwrap.dedent(f"""
        import json,os,selectors,signal,socket,threading,time
        from contextlib import suppress
        from _test_live_fairness import _managed_service_guest as guest
        from _test_live_fairness import _managed_job_request as requests
        from _test_live_fairness import _managed_lease_wire as leases
        from _test_live_fairness import _managed_job_wire as wire
        from _test_live_fairness._managed_job_store import ManagedJobStore,FactName
        from _test_live_fairness._evidence_wire import FrameReader,FrameKind
        guest.selectors.DefaultSelector=getattr(selectors,{selector_name!r})
        guest._STOP_GRACE_SECONDS=0.1
        guest._CLEANUP_SECONDS=0.5
        os.chmod({str(tmp_path)!r},0o700)
        anchor=os.open({str(tmp_path)!r},os.O_RDONLY|os.O_DIRECTORY)
        store=ManagedJobStore({RUN!r},_anchor_fd=anchor,_namespace='managed',_owner_uid=os.getuid())
        os.close(anchor)
        launch={request.launch!r}
        request=requests.ManagedJobRequest(launch,'command',(sys.executable,'-c',{workload!r}),None,
            'discard',None,(),b'',b'',leases.sampled_lease(launch,leases.boottime_ns()),
            live_io=requests.ManagedLiveIO({NONCE!r},False,True))
        store.publish_request(request)
        class Boundary:
            pid=None
            def place(self,pid): self.pid=pid
            def kill(self):
                if self.pid is not None:
                    with suppress(ProcessLookupError): os.kill(self.pid,signal.SIGKILL)
            def empty(self): return True
        boundary=Boundary()
        counts={{'stdout':0,'stderr':0}}
        bounded_stop=[]
        errors=[]
        workers=[]
        finished=[]
        def notify():
            def peer():
                directory=store.open_live_directory()
                stopped=False
                try:
                    def record(frame):
                        if frame.kind is FrameKind.STDOUT: counts['stdout']+=len(frame.body)
                        if frame.kind is FrameKind.STDERR: counts['stderr']+=len(frame.body)
                        if frame.kind is FrameKind.FINISHED: finished.append(True)
                    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                        client.settimeout(2)
                        client.connect('/proc/self/fd/'+str(directory)+'/live-io.sock')
                        client.sendall(b'AGWL1 '+{NONCE.encode()!r}+b' 0 1\\n')
                        client.shutdown(socket.SHUT_WR)
                        reader=FrameReader({NONCE!r},record)
                        deadline=time.monotonic()+2
                        while chunk:=client.recv(65536):
                            view=memoryview(chunk)
                            while view: view=view[reader.try_write(view):]
                            if not stopped and (counts['stdout']>=1048576 or time.monotonic()>=deadline):
                                bounded_stop.append(dict(counts))
                                store.publish_stop_request(launch)
                                stopped=True
                        reader.finish()
                        assert reader.error is None
                except BaseException as exc: errors.append(repr(exc))
                finally:
                    with suppress(BaseException): store.publish_stop_request(launch)
                    os.close(directory)
            worker=threading.Thread(target=peer,daemon=True)
            workers.append(worker)
            worker.start()
        try:
            assert guest.run({RUN!r},_store=store,_boundary=boundary,_notify=notify,
                _identity_check=False,_apply_identity=False)==0
            workers[0].join(3)
            assert not workers[0].is_alive() and not errors,errors
            wait=store.read_fact(FactName.WAIT)
            evidence={{'counts':counts,'bounded_stop':bounded_stop,'finished':bool(finished),
                'wait':None if wait is None else wire.decode_fact(wait)}}
            print(json.dumps(evidence,sort_keys=True))
            assert not bounded_stop,evidence
            assert counts['stderr']==262144 and finished,evidence
            assert wait is not None and wire.decode_fact(wait)['exit_code']==0,evidence
        finally:
            boundary.kill()
            if boundary.pid is not None:
                with suppress(ChildProcessError): os.waitpid(boundary.pid,0)
            store.close()
        """)
    completed = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source], capture_output=True, check=False, timeout=10
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.parametrize("object_type", ["file", "symlink", "fifo", "wrong-mode"])
def test_disposal_refuses_wrong_fixed_socket_object(tmp_path: Path, object_type: str) -> None:
    from .test_managed_disposal import _terminal

    with _store(tmp_path) as store:
        launch = _terminal(store)
        path = tmp_path / "managed" / RUN / live_wire.LIVE_SOCKET_NAME
        if object_type == "file":
            path.write_bytes(b"")
        elif object_type == "symlink":
            path.symlink_to("launch")
        elif object_type == "fifo":
            os.mkfifo(path, 0o600)
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                directory = store.open_live_directory()
                try:
                    listener.bind(f"/proc/self/fd/{directory}/{live_wire.LIVE_SOCKET_NAME}")
                finally:
                    os.close(directory)
                path.chmod(0o666)
        with pytest.raises(StoreError):
            store.dispose(launch)
        assert path.exists()


def test_disposal_rechecks_fixed_socket_inode_before_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.execution import _managed_disposal_store as disposal

    from .test_managed_disposal import _terminal

    with _store(tmp_path) as store:
        launch = _terminal(store)
        directory = store.open_live_directory()
        path = tmp_path / "managed" / RUN / live_wire.LIVE_SOCKET_NAME
        original = disposal._validate_facts
        validations = 0
        with (
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as first,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement,
        ):
            first.bind(f"/proc/self/fd/{directory}/{live_wire.LIVE_SOCKET_NAME}")
            path.chmod(0o600)

            def replace_after_inventory(inventory: object, expected: bytes) -> bool:
                nonlocal validations
                result = original(inventory, expected)
                validations += 1
                if validations == 2:
                    path.unlink()
                    replacement.bind(f"/proc/self/fd/{directory}/{live_wire.LIVE_SOCKET_NAME}")
                    path.chmod(0o600)
                return result

            monkeypatch.setattr(disposal, "_validate_facts", replace_after_inventory)
            try:
                with pytest.raises(StoreError):
                    store.dispose(launch)
                assert path.is_socket()
            finally:
                os.close(directory)
