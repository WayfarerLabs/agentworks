"""One-use, nonblocking foreground admission owned by the managed controller."""

from __future__ import annotations

import hashlib
import os
import selectors
import socket
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from ._evidence_wire import Frame, FrameKind, encode_frame
from ._inline_control import StreamEnd, StreamName, StreamRetention, empty_body, encode_stream_end
from ._managed_live_protocol import HANDSHAKE_BYTES, encode_handshake
from ._managed_live_protocol import LIVE_SOCKET_NAME as LIVE_SOCKET_NAME

if TYPE_CHECKING:
    from ._managed_job_request import ManagedLiveIO
    from ._managed_job_store import ManagedJobStore

_ADMISSION_SECONDS = 30.0


class LiveIO:
    """Keep one input chunk and one encoded frame while supervision keeps moving.

    Stream evidence counts complete socket records. Caller delivery remains the
    host's independent responsibility; durable stream facts never use this state.
    """

    def __init__(self, store: ManagedJobStore, metadata: ManagedLiveIO, launch: bytes) -> None:
        self.metadata = metadata
        self.launch = launch
        self.directory = store.open_live_directory()
        self.listener: socket.socket | None = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection: socket.socket | None = None
        self.deadline = time.monotonic() + _ADMISSION_SECONDS
        self.release_fd = -1
        self.admitted = False
        self.refused = False
        self.input_eof = not metadata.input
        self._input_shutdown = False
        self._draining_input = False
        self.input_pending = b""
        self.pending = b""
        self._pending_kind: FrameKind | None = None
        self._pending_body = b""
        self._sequence = 0
        self._handshake = b""
        self._expected = encode_handshake(metadata)
        self._counts = dict.fromkeys(StreamName, 0)
        self._digests = {name: hashlib.sha256() for name in StreamName}
        self._ended: set[StreamName] = set()
        self.finished = False
        try:
            self.listener.setblocking(False)
            self.listener.bind(f"/proc/self/fd/{self.directory}/{LIVE_SOCKET_NAME}")
            os.chmod(LIVE_SOCKET_NAME, 0o600, dir_fd=self.directory, follow_symlinks=False)
            self.listener.listen(1)
        except BaseException:
            self.close()
            raise

    def close_child(self) -> None:
        """Remove protected controller descriptors before child demotion."""
        if self.listener is not None:
            self.listener.close()
        if self.connection is not None:
            self.connection.close()
        os.close(self.directory)

    def take_gate(self, fd: int) -> None:
        self.release_fd = fd

    def _close_gate(self) -> None:
        if self.release_fd >= 0:
            os.close(self.release_fd)
            self.release_fd = -1

    def refuse(self) -> None:
        if not self.admitted:
            self.refused = True
            self._close_gate()
            self.retire()

    def retire(self) -> None:
        """Forget presentation on loss without stopping an entered workload."""
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.pending = self._pending_body = self.input_pending = b""
        self._pending_kind = None
        self.input_eof = True
        self._draining_input = False
        if not self.admitted:
            self.refused = True
            self._close_gate()

    def close(self) -> None:
        self.retire()
        self._close_gate()
        os.close(self.directory)

    @property
    def output_ready(self) -> bool:
        return self.admitted and self.connection is not None and self.metadata.output and not self.pending

    def queue(self, kind: FrameKind, body: bytes) -> None:
        assert not self.pending
        self.pending = encode_frame(self.metadata.nonce, Frame(self._sequence, kind, body))
        self._pending_kind, self._pending_body = kind, body
        self._sequence += 1

    def end_stream(self, name: StreamName) -> None:
        self.queue(
            FrameKind.STREAM_END,
            encode_stream_end(
                StreamEnd(
                    name, self._counts[name], self._digests[name].hexdigest(), True, False, StreamRetention.DELIVERED
                )
            ),
        )
        self._ended.add(name)

    def stream_ended(self, name: StreamName) -> bool:
        return name in self._ended

    def finish(self) -> None:
        if (
            self.admitted
            and self.connection is not None
            and not self.pending
            and not self.finished
            and not self._draining_input
        ):
            self.queue(FrameKind.FINISHED, empty_body())

    def stop_input(self) -> None:
        self.input_eof = True
        self.input_pending = b""
        if self.connection is not None and self.metadata.input and not self._input_shutdown:
            self._input_shutdown = True
            self._draining_input = True
            with suppress(OSError):
                self.connection.shutdown(socket.SHUT_RD)

    def sync(self, selector: selectors.BaseSelector) -> None:
        """Retire stale socket keys, including descriptors already closed on loss."""
        wanted: dict[int, tuple[socket.socket, int]] = {}
        if self.listener is not None:
            wanted[self.listener.fileno()] = (self.listener, selectors.EVENT_READ)
        if self.connection is not None:
            events = selectors.EVENT_WRITE if self.pending else 0
            if (
                (not self.admitted and self._pending_kind is not FrameKind.LAUNCHING)
                or self._draining_input
                or (self.admitted and self.metadata.input and not self.input_eof and not self.input_pending)
            ):
                events |= selectors.EVENT_READ
            if events:
                wanted[self.connection.fileno()] = (self.connection, events)
        for key in list(selector.get_map().values()):
            if key.data is self and key.fd not in wanted:
                selector.unregister(key.fd)
        for fd, (stream, events) in wanted.items():
            if fd in selector.get_map():
                selector.modify(fd, events, self)
            else:
                selector.register(stream, events, self)

    def progress(self, fd: int, events: int) -> None:
        """Perform at most one bounded read and write for this selector event."""
        try:
            if self.listener is not None and fd == self.listener.fileno():
                self.connection, _ = self.listener.accept()
                self.connection.setblocking(False)
                self.listener.close()
                self.listener = None
                return
            if self.connection is None:
                return
            if events & selectors.EVENT_READ:
                if not self.admitted and self._pending_kind is not FrameKind.LAUNCHING:
                    chunk = self.connection.recv(HANDSHAKE_BYTES - len(self._handshake))
                    if not chunk:
                        self.refuse()
                        return
                    self._handshake += chunk
                    if not self._expected.startswith(self._handshake):
                        self.refuse()
                        return
                    if self._handshake == self._expected:
                        self.queue(FrameKind.LAUNCHING, self.launch)
                elif self.admitted:
                    chunk = self.connection.recv(65536)
                    if self._draining_input:
                        # SHUT_RD prevents new peer writes but Linux retains
                        # already queued bytes. Drain them without forwarding so
                        # final close does not reset the remaining output half.
                        if not chunk:
                            self._draining_input = False
                    elif chunk:
                        self.input_pending = chunk
                    else:
                        self.input_eof = True
            if events & selectors.EVENT_WRITE and self.pending:
                sent = self.connection.send(self.pending)
                if sent == 0:
                    self.retire()
                    return
                self.pending = self.pending[sent:]
                if not self.pending:
                    kind, body = self._pending_kind, self._pending_body
                    if kind in (FrameKind.STDOUT, FrameKind.STDERR):
                        name = StreamName.STDOUT if kind is FrameKind.STDOUT else StreamName.STDERR
                        self._counts[name] += len(body)
                        self._digests[name].update(body)
                    elif kind is FrameKind.FINISHED:
                        self.finished = True
                        self.retire()
                    # LAUNCHING completion is consumed only after the loop
                    # rechecks stop and lease, before releasing the child.
                    if kind is not FrameKind.LAUNCHING:
                        self._pending_kind = None
                    self._pending_body = b""
        except BlockingIOError:
            pass
        except OSError:
            self.retire()

    def admit_if_bound(self) -> None:
        if self._pending_kind is FrameKind.LAUNCHING and not self.pending and self.connection is not None:
            os.write(self.release_fd, b"1")
            self._close_gate()
            self.admitted = True
            self._pending_kind = None
