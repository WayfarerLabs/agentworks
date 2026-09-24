"""Protected, boot-local target store for canonical managed-job facts.

Production always walks /run/agentworks/managed-runs-v1 from the filesystem
root. The private anchor and namespace arguments permit isolated tests only.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from . import _managed_job_request as request_wire
from . import _managed_job_wire as wire


class StoreError(ValueError):
    """A protected store object is invalid or an operation conflicts."""


class FactName(StrEnum):
    LAUNCH = "launch"
    WAIT = "wait"
    STDOUT_END = "stdout-end"
    STDERR_END = "stderr-end"
    BOUNDARY_EMPTY = "boundary-empty"


class RequestAsset(StrEnum):
    LAUNCH = "request-launch"
    CONTROL = "request-control"
    ENVIRONMENT = "request-environment"
    SOURCE = "request-source"
    STDIN = "request-stdin"


class Stream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class CaptureDisposition(StrEnum):
    COMPLETE = "complete-capture"
    TRUNCATED = "truncated-capture"


@dataclass(frozen=True)
class CapturedPrefix:
    length: int
    sha256: str
    disposition: CaptureDisposition


class CaptureWriter:
    """One private, bounded spool. Finish syncs and closes before an end fact."""

    def __init__(self, directory: int, fd: int, limit: int) -> None:
        self._directory = directory
        self._fd = fd
        self._limit = limit
        self._digest = hashlib.sha256()
        self._length = 0
        self._omitted = False

    def write(self, chunk: bytes) -> None:
        if type(chunk) is not bytes or self._fd < 0:
            raise StoreError("invalid capture chunk")
        prefix = chunk[: self._limit - self._length]
        if prefix:
            _write_all(self._fd, prefix)
            self._digest.update(prefix)
            self._length += len(prefix)
        self._omitted |= len(prefix) != len(chunk)

    def finish(self) -> CapturedPrefix:
        if self._fd < 0:
            raise StoreError("capture already closed")
        os.fsync(self._fd)
        self.close()
        return CapturedPrefix(
            self._length,
            self._digest.hexdigest(),
            CaptureDisposition.TRUNCATED if self._omitted else CaptureDisposition.COMPLETE,
        )

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
            os.close(self._directory)


_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_REQUEST_STAGE = re.compile(r"\.request-stage-[0-9a-f]{32}\Z")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_KIND = {
    FactName.LAUNCH: "launch",
    FactName.WAIT: "wait",
    FactName.STDOUT_END: "stream-end",
    FactName.STDERR_END: "stream-end",
    FactName.BOUNDARY_EMPTY: "boundary-empty",
}
_REQUEST_BOUNDS = {
    RequestAsset.LAUNCH: request_wire.MAX_LAUNCH_BYTES,
    RequestAsset.CONTROL: request_wire.MAX_CONTROL_BYTES,
    RequestAsset.ENVIRONMENT: request_wire.MAX_ENVIRONMENT_BYTES,
    RequestAsset.SOURCE: request_wire.MAX_SOURCE_BYTES,
    RequestAsset.STDIN: request_wire.MAX_STDIN_BYTES,
}


def _safe_stat(fd: int, mode: int, owner: int, *, links: tuple[int, ...] | None = None) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise StoreError("unsafe file")
    if info.st_uid != owner or stat.S_IMODE(info.st_mode) != mode or (links is not None and info.st_nlink not in links):
        raise StoreError("unsafe store object")


def _open_leaf(directory: int, name: str, mode: int, owner: int, *, links: tuple[int, ...]) -> int | None:
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError("unsafe store leaf") from exc
    try:
        _safe_stat(fd, mode, owner, links=links)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_request_leaf(directory: int, name: str, owner: int) -> int | None:
    """Accept only a final leaf or its one private publication-stage link."""
    fd = _open_leaf(directory, name, 0o400, owner, links=(1, 2))
    if fd is None or os.fstat(fd).st_nlink == 1:
        return fd
    final = os.fstat(fd)
    stage_matches = 0
    try:
        for candidate in os.listdir(directory):
            if _REQUEST_STAGE.fullmatch(candidate) is None:
                continue
            try:
                stage = _open_leaf(directory, candidate, 0o400, owner, links=(2,))
            except StoreError:
                continue
            if stage is None:
                continue
            try:
                observed = os.fstat(stage)
                if (observed.st_dev, observed.st_ino) == (final.st_dev, final.st_ino):
                    stage_matches += 1
            finally:
                os.close(stage)
        if stage_matches != 1 and os.fstat(fd).st_nlink != 1:
            raise StoreError("unsafe request asset link")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_all(fd: int, bound: int) -> bytes:
    chunks = bytearray()
    while len(chunks) <= bound:
        chunk = os.read(fd, min(65536, bound + 1 - len(chunks)))
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)
    raise StoreError("store object exceeds bound")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise StoreError("short store write")
        view = view[written:]


class ManagedJobStore:
    """One run's fixed facts and bounded closed capture spools."""

    def __init__(
        self,
        run_id: str,
        *,
        _namespace: str = "/run/agentworks/managed-runs-v1",
        _owner_uid: int = 0,
        _anchor_fd: int | None = None,
    ) -> None:
        if sys.platform != "linux":
            raise StoreError("managed job store requires Linux")
        if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
            raise StoreError("invalid run identity")
        if type(_owner_uid) is not int or _owner_uid < 0:
            raise StoreError("invalid owner")
        if _anchor_fd is None:
            if type(_namespace) is not str or _namespace != "/run/agentworks/managed-runs-v1":
                raise StoreError("invalid namespace")
            self._anchor_fd = os.open("/", _DIR_FLAGS)
            parts: tuple[str, ...] = ("run", "agentworks", "managed-runs-v1")
        else:
            if type(_anchor_fd) is not int or type(_namespace) is not str or not _namespace.isascii():
                raise StoreError("invalid test namespace")
            parts = tuple(_namespace.split("/"))
            if not parts or any(not part or part in (".", "..") or not part.isascii() for part in parts):
                raise StoreError("invalid test namespace")
            self._anchor_fd = os.dup(_anchor_fd)
        self.run_id = run_id
        self._owner_uid = _owner_uid
        self._parts = parts
        try:
            anchor = os.fstat(self._anchor_fd)
            if not stat.S_ISDIR(anchor.st_mode) or anchor.st_uid != _owner_uid or stat.S_IMODE(anchor.st_mode) & 0o022:
                raise StoreError("unsafe directory")
        except BaseException:
            os.close(self._anchor_fd)
            raise

    def close(self) -> None:
        os.close(self._anchor_fd)

    def __enter__(self) -> ManagedJobStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _run_dir(self, *, create: bool) -> int | None:
        parent = os.dup(self._anchor_fd)
        try:
            for index, part in enumerate((*self._parts, self.run_id)):
                exact = index >= len(self._parts) - 1
                try:
                    child = os.open(part, _DIR_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    if not create:
                        os.close(parent)
                        return None
                    created = False
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent)
                        created = True
                        os.chmod(part, 0o700, dir_fd=parent, follow_symlinks=False)
                        os.fsync(parent)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise StoreError("cannot create directory") from exc
                    try:
                        child = os.open(part, _DIR_FLAGS, dir_fd=parent)
                    except OSError as exc:
                        raise StoreError("unsafe directory") from exc
                    if created:
                        os.fsync(child)
                except OSError as exc:
                    raise StoreError("unsafe directory") from exc
                try:
                    info = os.fstat(child)
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != self._owner_uid:
                        raise StoreError("unsafe directory")
                    mode = stat.S_IMODE(info.st_mode)
                    if (exact and mode != 0o700) or (not exact and mode & 0o022):
                        raise StoreError("unsafe directory")
                except BaseException:
                    os.close(child)
                    raise
                os.close(parent)
                parent = child
            return parent
        except BaseException:
            os.close(parent)
            raise

    def _checked_fact(self, name: FactName, data: bytes) -> dict[str, object]:
        if type(name) is not FactName or type(data) is not bytes:
            raise StoreError("invalid fact")
        try:
            value = wire.decode_fact(data)
        except wire.ManagedJobWireError as exc:
            raise StoreError("invalid fact") from exc
        if value["kind"] != _KIND[name] or value["run_id"] != self.run_id:
            raise StoreError("wrong fact identity")
        if name in (FactName.STDOUT_END, FactName.STDERR_END) and value["stream"] != name.value[:-4]:
            raise StoreError("wrong stream identity")
        return value

    def read_fact(self, name: FactName) -> bytes | None:
        if type(name) is not FactName:
            raise StoreError("invalid fact name")
        directory = self._run_dir(create=False)
        if directory is None:
            return None
        try:
            # Publication can crash after link and before stage unlink. That
            # leaves exactly two root-owned links; neither is user input.
            fd = _open_leaf(directory, name.value, 0o400, self._owner_uid, links=(1, 2))
            if fd is None:
                return None
            try:
                data = _read_all(fd, wire.MAX_MANAGED_JOB_FACT_BYTES)
            finally:
                os.close(fd)
            self._checked_fact(name, data)
            return data
        finally:
            os.close(directory)

    def publish_fact(self, name: FactName, data: bytes) -> None:
        self._checked_fact(name, data)
        self._publish_immutable(name, data)

    def _publish_immutable(self, name: FactName | RequestAsset, data: bytes) -> None:
        """Publish one fixed validated leaf with byte-exact reconciliation."""
        directory = self._run_dir(create=True)
        assert directory is not None
        stage = (".fact-stage-" if isinstance(name, FactName) else ".request-stage-") + uuid4().hex
        try:
            existing = self.read_fact(name) if isinstance(name, FactName) else self.read_request_asset(name)
            if existing is not None:
                if existing != data:
                    raise StoreError("conflicting store object")
                return
            fd = os.open(stage, _CREATE_FLAGS, 0o400, dir_fd=directory)
            try:
                os.fchmod(fd, 0o400)
                _safe_stat(fd, 0o400, self._owner_uid, links=(1,))
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(stage, name.value, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.fsync(directory)
            except FileExistsError:
                existing = self.read_fact(name) if isinstance(name, FactName) else self.read_request_asset(name)
                if existing != data:
                    raise StoreError("conflicting store object") from None
            finally:
                os.unlink(stage, dir_fd=directory)
                os.fsync(directory)
        finally:
            os.close(directory)

    def read_request_asset(self, name: RequestAsset) -> bytes | None:
        """Read one protected fixed leaf without treating absence as evidence."""
        if type(name) is not RequestAsset:
            raise StoreError("invalid request asset name")
        directory = self._run_dir(create=False)
        if directory is None:
            return None
        try:
            fd = _open_request_leaf(directory, name.value, self._owner_uid)
            if fd is None:
                return None
            try:
                return _read_all(fd, _REQUEST_BOUNDS[name])
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def publish_request_asset(self, name: RequestAsset, data: bytes) -> None:
        """Publish a fixed, create-once request leaf with byte-exact reconciliation."""
        if type(name) is not RequestAsset or type(data) is not bytes or len(data) > _REQUEST_BOUNDS[name]:
            raise StoreError("invalid request asset")
        if name is RequestAsset.LAUNCH:
            try:
                launch = request_wire.decode_request_launch(data)
            except request_wire.RequestError:
                raise StoreError("invalid request launch") from None
            if launch["run_id"] != self.run_id:
                raise StoreError("wrong request run identity")
        elif name is RequestAsset.CONTROL:
            try:
                control = request_wire.decode_control(data)
            except request_wire.RequestError:
                raise StoreError("invalid request control") from None
            if control.get("run_id") != self.run_id:
                raise StoreError("wrong request run identity")
        elif name is RequestAsset.ENVIRONMENT:
            try:
                request_wire.decode_environment(data)
            except request_wire.RequestError:
                raise StoreError("invalid request environment") from None
        self._publish_immutable(name, data)

    def publish_request(self, request: request_wire.ManagedJobRequest) -> None:
        """Validate the entire request before publishing any asset."""
        try:
            assets = request_wire.encode_request(request)
        except request_wire.RequestError:
            raise StoreError("invalid managed request") from None
        if request_wire.decode_request_launch(request.launch)["run_id"] != self.run_id:
            raise StoreError("wrong request run identity")
        for name in RequestAsset:
            self.publish_request_asset(name, assets[name.value])

    def read_request(self) -> request_wire.ManagedJobRequest | None:
        """Only a complete validated set is consumable; partial sets refuse."""
        assets = {name.value: self.read_request_asset(name) for name in RequestAsset}
        if all(value is None for value in assets.values()):
            return None
        if any(value is None for value in assets.values()):
            raise StoreError("incomplete managed request")
        try:
            request = request_wire.decode_request(assets)  # type: ignore[arg-type]
        except request_wire.RequestError:
            raise StoreError("invalid managed request") from None
        if request_wire.decode_request_launch(request.launch)["run_id"] != self.run_id:
            raise StoreError("wrong request run identity")
        return request

    def open_capture(self, stream: Stream, limit: int) -> CaptureWriter:
        """Create a single protected spool for event-driven target capture."""
        if type(stream) is not Stream or type(limit) is not int or not 0 <= limit <= wire.MAX_CAPTURE_PREFIX_BYTES_V1:
            raise StoreError("invalid capture request")
        directory = self._run_dir(create=True)
        assert directory is not None
        try:
            fd = os.open(stream.value, _CREATE_FLAGS, 0o600, dir_fd=directory)
            try:
                os.fchmod(fd, 0o600)
                _safe_stat(fd, 0o600, self._owner_uid, links=(1,))
            except BaseException:
                os.close(fd)
                raise
            os.fsync(directory)
            return CaptureWriter(directory, fd, limit)
        except BaseException:
            os.close(directory)
            raise

    def read_capture(self, stream: Stream, expected_launch: bytes) -> bytes | None:
        if type(stream) is not Stream or type(expected_launch) is not bytes:
            raise StoreError("invalid capture read")
        self._checked_fact(FactName.LAUNCH, expected_launch)
        launch = self.read_fact(FactName.LAUNCH)
        if launch is None or launch != expected_launch:
            raise StoreError("launch binding mismatch")
        name = FactName.STDOUT_END if stream is Stream.STDOUT else FactName.STDERR_END
        ended = self.read_fact(name)
        if ended is None:
            return None
        fact = self._checked_fact(name, ended)
        if (
            fact["unit"] != f"agw-managed-{self.run_id}.service"
            or fact["receipt_sha256"] != hashlib.sha256(launch).hexdigest()
        ):
            raise StoreError("launch binding mismatch")
        directory = self._run_dir(create=False)
        assert directory is not None
        try:
            fd = _open_leaf(directory, stream.value, 0o600, self._owner_uid, links=(1,))
            if fact["disposition"] in ("discarded", "sensitivity-suppressed"):
                if fd is not None:
                    os.close(fd)
                    raise StoreError("unexpected capture spool")
                return None
            if fd is None:
                raise StoreError("missing capture spool")
            try:
                length = fact["retained_bytes"]
                if type(length) is not int or length > wire.MAX_CAPTURE_PREFIX_BYTES_V1:
                    raise StoreError("capture spool exceeds bound")
                if os.fstat(fd).st_size != length:
                    raise StoreError("capture spool mismatch")
                data = _read_all(fd, length)
            finally:
                os.close(fd)
            if len(data) != length or hashlib.sha256(data).hexdigest() != fact["retained_sha256"]:
                raise StoreError("capture spool mismatch")
            return data
        finally:
            os.close(directory)
