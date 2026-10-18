"""Fixed Linux relay between borrowed process pipes and one protected run socket."""

from __future__ import annotations

import errno
import os
import selectors
import socket
import stat
import sys
import time
from typing import cast

from ._helper_identity import matches_current_identity
from ._managed_job_store import FactName, ManagedJobStore, StoreError
from ._managed_live_protocol import (
    ENVELOPE_HEADER_BYTES,
    LIVE_SOCKET_NAME,
    ManagedLiveError,
    ManagedLiveRequest,
    checked_launch,
    decode_header,
    decode_request,
    encode_handshake,
)
from ._vm_guest_identity_guest import _GuestRefusal, _identity

_SETUP_SECONDS = 30.0
_CHUNK_BYTES = 65536
_REFUSAL = 125


def _ready(fd: int, event: int, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ManagedLiveError("managed relay setup expired")
    with selectors.PollSelector() as selector:
        selector.register(fd, event)
        if not selector.select(remaining):
            raise ManagedLiveError("managed relay setup expired")


def _read_exact(fd: int, length: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < length:
        _ready(fd, selectors.EVENT_READ, deadline)
        try:
            chunk = os.read(fd, length - len(data))
        except BlockingIOError:
            continue
        if not chunk:
            raise ManagedLiveError("incomplete managed relay envelope")
        data.extend(chunk)
    return bytes(data)


def _read_request() -> ManagedLiveRequest:
    """Read exactly the envelope, leaving all application suffix bytes on FD 0."""
    os.set_blocking(0, False)
    deadline = time.monotonic() + _SETUP_SECONDS
    length = decode_header(_read_exact(0, ENVELOPE_HEADER_BYTES, deadline))
    request = decode_request(_read_exact(0, length, deadline))
    if not request.live_io.input:
        _ready(0, selectors.EVENT_READ, deadline)
        if os.read(0, 1):
            raise ManagedLiveError("unexpected managed relay input")
    return request


def _prepare(request: ManagedLiveRequest, store: ManagedJobStore) -> int:
    """Validate protected launch, request and admission state before connecting."""
    launch = checked_launch(request)
    if store.run_id != launch["run_id"] or store.read_fact(FactName.LAUNCH) != request.expected_launch:
        raise ManagedLiveError("managed relay launch unavailable")
    protected = store.read_request()
    if (
        protected is None
        or protected.launch != request.expected_launch
        or protected.live_io != request.live_io
        or store.read_stop_request()
        or store.read_fact(FactName.WAIT) is not None
        or store.read_fact(FactName.BOUNDARY_EMPTY) is not None
    ):
        raise ManagedLiveError("managed relay admission unavailable")
    directory = store._run_dir(create=False)
    if directory is None:
        raise ManagedLiveError("managed relay run unavailable")
    try:
        try:
            os.stat("disposal", dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ManagedLiveError("managed relay disposal committed")
        info = os.stat(LIVE_SOCKET_NAME, dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != store._owner_uid
            or info.st_nlink != 1
        ):
            raise ManagedLiveError("unsafe managed relay endpoint")
        return directory
    except BaseException:
        os.close(directory)
        raise


def _connect(directory: int, request: ManagedLiveRequest) -> socket.socket:
    """Connect once through the held validated directory and send fixed admission."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.setblocking(False)
        deadline = time.monotonic() + _SETUP_SECONDS
        result = connection.connect_ex(f"/proc/self/fd/{directory}/{LIVE_SOCKET_NAME}")
        if result not in (0, errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
            raise OSError(result, "managed relay connection failed")
        if result != 0:
            _ready(connection.fileno(), selectors.EVENT_WRITE, deadline)
            result = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if result:
                raise OSError(result, "managed relay connection failed")
        pending = memoryview(encode_handshake(request.live_io))
        while pending:
            _ready(connection.fileno(), selectors.EVENT_WRITE, deadline)
            try:
                count = connection.send(pending)
            except BlockingIOError:
                continue
            if count <= 0:
                raise ManagedLiveError("incomplete managed relay admission")
            pending = pending[count:]
        return connection
    except BaseException:
        connection.close()
        raise


def _relay(connection: socket.socket, *, live_input: bool, input_fd: int = 0, output_fd: int = 1) -> None:
    """Copy raw bytes with one bounded chunk in each direction and no frame parser.

    The input descriptor belongs to this helper. Closing it on consumer closure
    lets the outer carrier retire its borrowed source while output keeps draining.
    """
    connection.setblocking(False)
    os.set_blocking(output_fd, False)
    if live_input:
        os.set_blocking(input_fd, False)
    source_open = live_input
    input_owned = live_input
    write_open = True
    peer_open = True
    to_peer = b""
    to_output = b""

    def close_input() -> None:
        nonlocal source_open, input_owned, to_peer
        source_open = False
        to_peer = b""
        if input_owned:
            input_owned = False
            os.close(input_fd)

    try:
        while peer_open or to_output:
            if write_open and not source_open and not to_peer:
                connection.shutdown(socket.SHUT_WR)
                write_open = False
            with selectors.PollSelector() as selector:
                events = (selectors.EVENT_READ if peer_open and not to_output else 0) | (
                    selectors.EVENT_WRITE if write_open and to_peer else 0
                )
                if events:
                    selector.register(connection, events, "peer")
                if source_open and not to_peer:
                    selector.register(input_fd, selectors.EVENT_READ, "input")
                if to_output:
                    selector.register(output_fd, selectors.EVENT_WRITE, "output")
                ready = selector.select()
            for key, event in ready:
                if key.data == "input" and source_open:
                    try:
                        to_peer = os.read(input_fd, _CHUNK_BYTES)
                    except BlockingIOError:
                        continue
                    if not to_peer:
                        source_open = False
                elif key.data == "output":
                    try:
                        count = os.write(output_fd, to_output)
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise ManagedLiveError("managed relay output write failed")
                    to_output = to_output[count:]
                elif key.data == "peer":
                    if event & selectors.EVENT_WRITE and write_open and to_peer:
                        try:
                            count = connection.send(to_peer)
                        except BlockingIOError:
                            pass
                        except BrokenPipeError:
                            close_input()
                            connection.shutdown(socket.SHUT_WR)
                            write_open = False
                        else:
                            if count <= 0:
                                raise ManagedLiveError("managed relay input write failed")
                            to_peer = to_peer[count:]
                    if event & selectors.EVENT_READ and peer_open and not to_output:
                        try:
                            to_output = connection.recv(_CHUNK_BYTES)
                        except BlockingIOError:
                            continue
                        if not to_output:
                            peer_open = False
                            close_input()
                            write_open = False
    finally:
        close_input()


def main(nonce: str) -> int:
    """Refuse safely before attachment, without stopping or relaunching the job."""
    try:
        request = _read_request()
        if (
            sys.platform != "linux"
            or sys.version_info < (3, 11)
            or request.live_io.nonce != nonce
            or not matches_current_identity(request.identity)
            or _identity() != request.guest
        ):
            raise ManagedLiveError("managed relay identity mismatch")
        launch = checked_launch(request)
        with ManagedJobStore(cast("str", launch["run_id"])) as store:
            directory = _prepare(request, store)
            try:
                connection = _connect(directory, request)
            finally:
                os.close(directory)
        with connection:
            _relay(connection, live_input=request.live_io.input)
    except (ManagedLiveError, StoreError, _GuestRefusal, OSError, ValueError, TypeError):
        return _REFUSAL
    return 0
