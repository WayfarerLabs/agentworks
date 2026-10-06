"""Private Windows publication of a completely verified local download.

Create renames a held private stage with FileRenameInfo.ReplaceIfExists false.
Replace writes through a held existing file handle. A failed local mutation may
leave partial new bytes; publication_uncertain records that fact for the caller.
No API here enables a token privilege or changes destination access metadata.
"""

from __future__ import annotations

import ctypes
import hashlib
import secrets
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from agentworks.execution._local_download_stage import (
    LocalDownloadCleanupError,
    LocalDownloadCleanupUncertainError,
    LocalDownloadUnsupportedError,
)
from agentworks.execution.files import Create, Replace

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.carrier import Deadline

_CREATE = Create()
_READ = 0x80000000
_WRITE = 0x40000000
_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_READ_ATTRIBUTES = 0x80
_SHARE_READ = 1
_SHARE_WRITE = 2
_SHARE_DELETE = 4
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_NORMAL = 0x80
_DIRECTORY = 0x10
_REPARSE = 0x400
_OPEN_REPARSE = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_FILE_TYPE_DISK = 1
_FILE_ID_INFO = 18
_FILE_RENAME_INFO = 3
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ALREADY_EXISTS = 183
_ERROR_FILE_EXISTS = 80
_ERROR_NO_TOKEN = 1008
_ERROR_INSUFFICIENT_BUFFER = 122
_SAFE_FILE_ATTRIBUTES = _NORMAL | 0x20 | 0x02 | 0x04 | 0x2000  # Archive, hidden, system, not indexed.
_ARCHIVE = 0x20
_SECURITY_INFORMATION = 1 | 2 | 4  # Owner, group and DACL; SACL is not changed.
_CHUNK = 1024 * 1024


class LocalDownloadPartialMutationError(OSError):
    """A verified transfer reached the existing file; local bytes may have changed."""

    possible_local_change = True


class _FileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _ByHandleInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", _FileTime),
        ("accessed", _FileTime),
        ("modified", _FileTime),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("identifier", ctypes.c_ubyte * 16)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("replace", wintypes.DWORD),
        ("root", wintypes.HANDLE),
        ("name_length", wintypes.DWORD),
        ("name", wintypes.WCHAR * 1),
    ]


@dataclass(frozen=True)
class _Identity:
    volume: int
    file_id: bytes


@dataclass(frozen=True)
class _Observed:
    identity: _Identity
    attributes: int
    links: int
    size: int
    created: tuple[int, int]
    modified: tuple[int, int]


class _WindowsAPI:
    """Small Win32 boundary; every returned handle is owned by the publication."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise LocalDownloadUnsupportedError("Windows local publication requires Windows")
        self.kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security: Any = ctypes.WinDLL("advapi32", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        k = self.kernel
        a = self.security
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleInfo)]
        k.GetFileInformationByHandle.restype = wintypes.BOOL
        k.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.GetFileInformationByHandleEx.restype = wintypes.BOOL
        k.GetFileType.argtypes = [wintypes.HANDLE]
        k.GetFileType.restype = wintypes.DWORD
        k.WriteFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        k.WriteFile.restype = wintypes.BOOL
        k.ReadFile.argtypes = k.WriteFile.argtypes
        k.ReadFile.restype = wintypes.BOOL
        k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
        k.SetFilePointerEx.restype = wintypes.BOOL
        k.SetEndOfFile.argtypes = [wintypes.HANDLE]
        k.SetEndOfFile.restype = wintypes.BOOL
        k.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        k.FlushFileBuffers.restype = wintypes.BOOL
        k.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.SetFileInformationByHandle.restype = wintypes.BOOL
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
        a.OpenThreadToken.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        a.GetTokenInformation.restype = wintypes.BOOL
        a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        a.ConvertSidToStringSidW.restype = wintypes.BOOL
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
        ]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        a.GetKernelObjectSecurity.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        a.GetKernelObjectSecurity.restype = wintypes.BOOL
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree.restype = ctypes.c_void_p
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetCurrentThread.restype = wintypes.HANDLE

    @staticmethod
    def _error() -> OSError:
        return cast("OSError", ctypes.WinError(_WindowsAPI._last_error()))  # type: ignore[attr-defined]

    @staticmethod
    def _last_error() -> int:
        return cast("int", ctypes.get_last_error())  # type: ignore[attr-defined]

    @staticmethod
    def path(path: Path) -> str:
        raw = str(path)
        if len(raw) < 3 or raw[1:3] != ":\\" or not raw[0].isalpha():
            raise LocalDownloadUnsupportedError("Local Windows download requires a drive path")
        if "\x00" in raw or raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
            raise LocalDownloadUnsupportedError("Local Windows download path is unsupported")
        return "\\\\?\\" + raw

    def open(
        self,
        path: Path,
        access: int,
        sharing: int,
        disposition: int,
        flags: int,
        security: _SecurityAttributes | None = None,
    ) -> int:
        pointer = ctypes.byref(security) if security is not None else None
        handle = self.kernel.CreateFileW(self.path(path), access, sharing, pointer, disposition, flags, None)
        if handle is None or handle == ctypes.c_void_p(-1).value:
            raise self._error()
        return int(handle)

    def close(self, handle: int) -> None:
        if not self.kernel.CloseHandle(handle):
            raise self._error()

    def info(self, handle: int) -> _Observed:
        ordinary = _ByHandleInfo()
        identifier = _FileIdInfo()
        if self.kernel.GetFileType(handle) != _FILE_TYPE_DISK:
            raise LocalDownloadUnsupportedError("Local download object is not a disk file")
        if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(ordinary)):
            raise LocalDownloadUnsupportedError("Cannot inspect local file") from self._error()
        if not self.kernel.GetFileInformationByHandleEx(
            handle, _FILE_ID_INFO, ctypes.byref(identifier), ctypes.sizeof(identifier)
        ):
            raise LocalDownloadUnsupportedError("Cannot inspect full local file identity") from self._error()
        return _Observed(
            _Identity(identifier.volume, bytes(identifier.identifier)),
            ordinary.attributes,
            ordinary.links,
            (ordinary.size_high << 32) | ordinary.size_low,
            (ordinary.created.high, ordinary.created.low),
            (ordinary.modified.high, ordinary.modified.low),
        )

    def security_descriptor(self, handle: int) -> bytes:
        length = wintypes.DWORD()
        self.security.GetKernelObjectSecurity(handle, _SECURITY_INFORMATION, None, 0, ctypes.byref(length))
        if self._last_error() != _ERROR_INSUFFICIENT_BUFFER or not length.value:
            raise LocalDownloadUnsupportedError("Cannot inspect local access metadata") from self._error()
        buffer = ctypes.create_string_buffer(length.value)
        if not self.security.GetKernelObjectSecurity(
            handle, _SECURITY_INFORMATION, buffer, length.value, ctypes.byref(length)
        ):
            raise LocalDownloadUnsupportedError("Cannot inspect local access metadata") from self._error()
        return buffer.raw[: length.value]

    def private_security(self) -> tuple[_SecurityAttributes, int]:
        """Create an explicit caller-only DACL before the stage enters the namespace."""
        token = wintypes.HANDLE()
        descriptor = ctypes.c_void_p()
        if not self.security.OpenThreadToken(self.kernel.GetCurrentThread(), 0x0008, True, ctypes.byref(token)) and (
            self._last_error() != _ERROR_NO_TOKEN
            or not self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))
        ):
            raise LocalDownloadUnsupportedError("Cannot identify the workstation caller") from self._error()
        try:
            length = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            if self._last_error() != _ERROR_INSUFFICIENT_BUFFER or not length.value:
                raise LocalDownloadUnsupportedError("Cannot inspect the workstation caller") from self._error()
            buffer = ctypes.create_string_buffer(length.value)
            if not self.security.GetTokenInformation(token, 1, buffer, length.value, ctypes.byref(length)):
                raise LocalDownloadUnsupportedError("Cannot inspect the workstation caller") from self._error()
            sid_pointer = ctypes.c_void_p.from_buffer(buffer).value
            sid_string = wintypes.LPWSTR()
            if not self.security.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
                raise LocalDownloadUnsupportedError("Cannot encode the workstation caller") from self._error()
            try:
                sddl = f"D:P(A;;FA;;;{sid_string.value})"
                if not self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl, 1, ctypes.byref(descriptor), None
                ):
                    raise LocalDownloadUnsupportedError("Cannot prepare private local stage") from self._error()
            finally:
                self.kernel.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))
            if descriptor.value is None:
                raise LocalDownloadUnsupportedError("Cannot prepare private local stage")
            return _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, False), descriptor.value
        finally:
            if token.value is not None:
                try:
                    self.close(int(token.value))
                except BaseException as exc:
                    if descriptor.value is not None:
                        self.kernel.LocalFree(descriptor.value)
                    raise LocalDownloadCleanupUncertainError("Caller token close has an uncertain outcome") from exc

    def write(self, handle: int, data: memoryview) -> int:
        payload = data[:_CHUNK].tobytes()
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(payload)
        if not self.kernel.WriteFile(handle, buffer, len(payload), ctypes.byref(written), None):
            raise self._error()
        return written.value

    def read(self, handle: int) -> bytes:
        buffer = ctypes.create_string_buffer(_CHUNK)
        count = wintypes.DWORD()
        if not self.kernel.ReadFile(handle, buffer, _CHUNK, ctypes.byref(count), None):
            raise self._error()
        return buffer.raw[: count.value]

    def seek(self, handle: int, offset: int) -> None:
        if not self.kernel.SetFilePointerEx(handle, offset, None, 0):
            raise self._error()

    def flush(self, handle: int) -> None:
        if not self.kernel.FlushFileBuffers(handle):
            raise self._error()

    def truncate(self, handle: int) -> None:
        if not self.kernel.SetEndOfFile(handle):
            raise self._error()

    def rename_no_replace(self, stage: int, destination: Path) -> None:
        # All path components are held without FILE_SHARE_DELETE until this call returns.
        encoded = str(destination).encode("utf-16-le")
        size = _RenameInfo.name.offset + len(encoded) + 2
        buffer = ctypes.create_string_buffer(size)
        header = _RenameInfo.from_buffer(buffer)
        header.replace = 0
        header.root = None
        header.name_length = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + _RenameInfo.name.offset, encoded, len(encoded))
        if not self.kernel.SetFileInformationByHandle(stage, _FILE_RENAME_INFO, buffer, size):
            raise self._error()

    def delete_on_close(self, stage: int) -> None:
        flag = ctypes.c_ubyte(1)
        if not self.kernel.SetFileInformationByHandle(stage, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
            raise self._error()


class WindowsLocalDownloadPublication:
    """Owned Windows stage with the Linux writer's sink and publication interface.

    The caller serializes Agentworks operations and calls abort in a finally block.
    The destination remains unchanged until verified_complete, byte count, digest,
    remote cleanup and the deadline have been established by the coordinator.
    """

    def __init__(self, destination: Path, *, condition: Create | Replace = _CREATE) -> None:
        if type(condition) not in (Create, Replace):
            raise ValueError("Local download publication requires Create or Replace")
        self._api = _WindowsAPI()
        if any(part in (".", "..") for part in destination.parts):
            raise LocalDownloadUnsupportedError("Local download traversal components are unsupported")
        self._destination = destination.absolute()
        self._stage_path: Path | None = None
        self._stage: int | None = None
        self._target: int | None = None
        self._ancestors: list[int] = []
        self._original: _Observed | None = None
        self._original_security: bytes | None = None
        self._stage_identity: _Identity | None = None
        self._stage_delete_pending = False
        self._target_close_uncertain = False
        self._stage_close_uncertain = False
        self._ancestor_closes_uncertain: set[int] = set()
        self._observation_close_uncertain = False
        self._size = 0
        self._digest = hashlib.sha256()
        self.published = False
        self.publication_uncertain = False
        self.possible_local_change = False
        try:
            self._validate_path()
            for path in reversed((self._destination.parent, *self._destination.parent.parents)):
                handle = self._api.open(
                    path,
                    _READ_ATTRIBUTES,
                    _SHARE_READ | _SHARE_WRITE,
                    _OPEN_EXISTING,
                    _BACKUP_SEMANTICS | _OPEN_REPARSE,
                )
                self._ancestors.append(handle)
                observed = self._api.info(handle)
                if not observed.attributes & _DIRECTORY or observed.attributes & _REPARSE:
                    raise LocalDownloadUnsupportedError("Local download ancestor is a reparse point or not a directory")
            if isinstance(condition, Replace):
                self._target = self._api.open(
                    self._destination, _READ | _WRITE | _READ_CONTROL, _SHARE_READ, _OPEN_EXISTING, _OPEN_REPARSE
                )
                original = self._api.info(self._target)
                self._validate_file(original)
                self._original = original
                self._original_security = self._api.security_descriptor(self._target)
            else:
                existing = self._open_observation(self._destination)
                if existing is not None:
                    self._close_observation(existing)
                    raise FileExistsError("Local download destination already exists")
            security, descriptor = self._api.private_security()
            try:
                for _ in range(8):
                    self._stage_path = self._destination.with_name(f".agw-download-{secrets.token_hex(16)}")
                    try:
                        self._stage = self._api.open(
                            self._stage_path,
                            _READ | _WRITE | _DELETE,
                            _SHARE_READ | _SHARE_WRITE,
                            _CREATE_NEW,
                            _NORMAL,
                            security,
                        )
                    except OSError as exc:
                        if getattr(exc, "winerror", None) in (_ERROR_ALREADY_EXISTS, _ERROR_FILE_EXISTS):
                            continue
                        raise
                    break
                else:
                    raise FileExistsError("Cannot allocate a unique local download stage")
            finally:
                self._api.kernel.LocalFree(descriptor)
            assert self._stage is not None
            staged = self._api.info(self._stage)
            self._validate_file(staged)
            self._stage_identity = staged.identity
        except BaseException as setup_error:
            try:
                self.abort()
            except BaseException as cleanup_error:
                error_type = LocalDownloadCleanupUncertainError if self.cleanup_uncertain else LocalDownloadCleanupError
                raise error_type(
                    "Local download construction left unfinished cleanup",
                    unfinished_stage=self,
                    setup_error=setup_error,
                    cleanup_error=cleanup_error,
                ) from cleanup_error
            raise

    def _validate_path(self) -> None:
        raw = str(self._destination)
        name = self._destination.name
        if not name or ":" in name or name[-1] in (" ", ".") or self._destination.is_reserved():
            raise LocalDownloadUnsupportedError("Local download destination name is unsupported")
        if len(raw) >= 260:
            raise LocalDownloadUnsupportedError("Local Windows rename path exceeds MAX_PATH")
        self._api.path(self._destination)

    @staticmethod
    def _validate_file(info: _Observed) -> None:
        if info.links != 1 or info.attributes & (_DIRECTORY | _REPARSE):
            raise LocalDownloadUnsupportedError("Local destination must be a single-link ordinary file")
        if info.attributes & ~_SAFE_FILE_ATTRIBUTES:
            raise LocalDownloadUnsupportedError("Local file has unsupported attributes")

    def _open_observation(self, path: Path) -> int | None:
        try:
            return self._api.open(
                path, _READ_ATTRIBUTES, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE, _OPEN_EXISTING, _OPEN_REPARSE
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
                return None
            raise

    def _close_observation(self, handle: int) -> None:
        prior_uncertainty = self._observation_close_uncertain
        self._observation_close_uncertain = True
        self._api.close(handle)
        self._observation_close_uncertain = prior_uncertainty

    def _close_target(self) -> None:
        assert self._target is not None
        self._target_close_uncertain = True
        self._api.close(self._target)
        self._target = None
        self._target_close_uncertain = False

    def _close_stage(self) -> None:
        assert self._stage is not None
        self._stage_close_uncertain = True
        self._api.close(self._stage)
        self._stage = None
        self._stage_path = None
        self._stage_close_uncertain = False

    def _close_ancestor(self, handle: int) -> None:
        self._ancestor_closes_uncertain.add(handle)
        self._api.close(handle)
        self._ancestor_closes_uncertain.remove(handle)

    @property
    def cleanup_uncertain(self) -> bool:
        return (
            self._target_close_uncertain
            or self._stage_close_uncertain
            or bool(self._ancestor_closes_uncertain)
            or self._observation_close_uncertain
        )

    def try_write(self, data: memoryview) -> int:
        if self._stage is None or self.published or self.publication_uncertain or self.cleanup_uncertain:
            raise ValueError("Local download stage is closed")
        written = self._api.write(self._stage, data)
        self._digest.update(data[:written])
        self._size += written
        return written

    def _recheck_stage(self) -> None:
        stage = self._stage
        path = self._stage_path
        assert stage is not None and path is not None
        held = self._api.info(stage)
        self._validate_file(held)
        if held.identity != self._stage_identity or held.size != self._size:
            raise LocalDownloadUnsupportedError("Local download stage changed")
        observed = self._open_observation(path)
        if observed is None:
            raise FileNotFoundError("Local download stage name disappeared")
        try:
            if self._api.info(observed).identity != held.identity:
                raise LocalDownloadUnsupportedError("Local download stage name changed")
        finally:
            self._close_observation(observed)

    def _recheck_target(self) -> None:
        target = self._target
        assert target is not None and self._original is not None and self._original_security is not None
        current = self._api.info(target)
        self._validate_file(current)
        if current != self._original or self._api.security_descriptor(target) != self._original_security:
            raise FileExistsError("Local download destination changed before replacement")
        observed = self._open_observation(self._destination)
        if observed is None:
            raise FileNotFoundError("Local download destination disappeared")
        try:
            if self._api.info(observed).identity != current.identity:
                raise FileExistsError("Local download destination changed before replacement")
        finally:
            self._close_observation(observed)

    def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
        stage = self._stage
        if stage is None or self.published or self.publication_uncertain or self.cleanup_uncertain:
            raise ValueError("Local download stage is closed")
        if not verified_complete or size != self._size or sha256 != self._digest.hexdigest():
            raise ValueError("Local download is not completely verified")
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired before publication")
        self._recheck_stage()
        self._api.flush(stage)
        if self._target is None:
            if deadline is not None and deadline.expired:
                raise TimeoutError("Local download deadline expired before publication")
            assert self._ancestors and self._stage_path is not None
            self.publication_uncertain = True
            try:
                self._api.rename_no_replace(stage, self._destination)
            except BaseException:
                self._reconcile_create()
                raise
            self.published = True
            self.publication_uncertain = False
            self._stage_path = None
        else:
            self._recheck_target()
            self._api.seek(stage, 0)
            self._api.seek(self._target, 0)
            if deadline is not None and deadline.expired:
                raise TimeoutError("Local download deadline expired before publication")
            try:
                remaining = size
                while remaining:
                    if deadline is not None and deadline.expired:
                        raise TimeoutError("Local download deadline expired during replacement")
                    chunk = self._api.read(stage)
                    if not chunk or len(chunk) > remaining:
                        raise LocalDownloadUnsupportedError("Verified stage changed during replacement")
                    view = memoryview(chunk)
                    while view:
                        if deadline is not None and deadline.expired:
                            raise TimeoutError("Local download deadline expired during replacement")
                        self.possible_local_change = True
                        self.publication_uncertain = True
                        written = self._api.write(self._target, view)
                        if not written:
                            raise OSError("Local replacement made no write progress")
                        view = view[written:]
                    remaining -= len(chunk)
                if deadline is not None and deadline.expired:
                    raise TimeoutError("Local download deadline expired during replacement")
                self.possible_local_change = True
                self.publication_uncertain = True
                self._api.truncate(self._target)
                if deadline is not None and deadline.expired:
                    raise TimeoutError("Local download deadline expired during replacement")
                self._api.flush(self._target)
                if deadline is not None and deadline.expired:
                    raise TimeoutError("Local download deadline expired during replacement")
                final = self._api.info(self._target)
                assert self._original is not None
                if final.identity != self._original.identity or final.size != size or final.links != 1:
                    raise LocalDownloadUnsupportedError("Local replacement identity or size changed")
                if (
                    final.created != self._original.created
                    or final.attributes & ~_ARCHIVE != self._original.attributes & ~_ARCHIVE
                ):
                    raise LocalDownloadUnsupportedError("Local replacement file metadata changed")
                if self._api.security_descriptor(self._target) != self._original_security:
                    raise LocalDownloadUnsupportedError("Local replacement access metadata changed")
                if deadline is not None and deadline.expired:
                    raise TimeoutError("Local download deadline expired during replacement")
                self._close_target()
                if deadline is not None and deadline.expired:
                    raise TimeoutError("Local download deadline expired after replacement close")
            except Exception as exc:
                if self.possible_local_change:
                    raise LocalDownloadPartialMutationError("Local replacement may contain partial new bytes") from exc
                raise
            self.published = True
            self.publication_uncertain = False
        self.abort()

    def _reconcile_create(self) -> None:
        """Confirm publication only from the exact stage identity."""
        try:
            observed = self._open_observation(self._destination)
            if observed is None:
                return
            try:
                if self._api.info(observed).identity == self._stage_identity:
                    self.published = True
                    self.publication_uncertain = False
                    self._stage_path = None
            finally:
                self._close_observation(observed)
        except BaseException:
            pass

    def abort(self) -> None:
        """Clean owned stage and handles, retaining unknown close or publication facts."""
        first_error: BaseException | None = None
        if self._target is not None and not self._target_close_uncertain:
            try:
                self._close_target()
            except BaseException as exc:
                first_error = exc
        if self._stage is not None and not self._stage_close_uncertain:
            stage_ready_to_close = self._stage_path is None
            if self._stage_path is not None:
                try:
                    if self.publication_uncertain:
                        self._recheck_stage()
                    if not self._stage_delete_pending:
                        self._api.delete_on_close(self._stage)
                        self._stage_delete_pending = True
                    stage_ready_to_close = True
                except BaseException as exc:
                    first_error = first_error or exc
            if stage_ready_to_close:
                try:
                    self._close_stage()
                except BaseException as exc:
                    first_error = first_error or exc
        while self._ancestors:
            handle = self._ancestors.pop()
            try:
                self._close_ancestor(handle)
            except BaseException as exc:
                first_error = first_error or exc
        if self.cleanup_uncertain:
            raise LocalDownloadCleanupUncertainError(
                "Local download handle close has an uncertain outcome"
            ) from first_error
        if first_error is not None:
            raise LocalDownloadCleanupError(
                "Local download cleanup did not finish", cleanup_error=first_error
            ) from first_error
