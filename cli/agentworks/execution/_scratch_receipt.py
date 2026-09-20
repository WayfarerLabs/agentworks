"""Bounded immutable ownership receipts for private scratch objects."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum

from ._helper_identity import IdentityExpectation

_DATA_NAME = "data"
_RECEIPT_NAME = "receipt"
_DIRECTORY_MODE = 0o700
_DATA_MODE = 0o600
_RECEIPT_BUILD_MODE = 0o600
_RECEIPT_MODE = 0o400
_NAME_PREFIX = ".agentworks-scratch-"
_TOKEN_BYTES = 16
_MAX_RECEIPT_BYTES = 1024
_MAX_LENGTH = (1 << 63) - 1
_MAX_IDENTITY_NUMBER = (1 << 64) - 1


class ScratchOperation(Enum):
    """Closed scratch operation families recorded by ownership receipts."""

    STAGE = "stage"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True, repr=False)
class ScratchReceiptContext:
    """Core-bound operation and execution identity for one scratch lifetime."""

    operation: ScratchOperation
    identity: IdentityExpectation


@dataclass(frozen=True, repr=False)
class _Identity:
    device: int
    inode: int


@dataclass(frozen=True, repr=False)
class ScratchOwnership:
    """Receipt-backed exact ownership shared by active and historical views."""

    _token: bytes
    _context: ScratchReceiptContext
    _parent: _Identity
    _directory: _Identity
    _data: _Identity
    _gid: int
    _length: int
    _receipt: _Identity


@dataclass(frozen=True, repr=False)
class ScratchHistoricalOwnership:
    """Historical ownership that authorizes exact cleanup, never content use."""

    _ownership: ScratchOwnership


@dataclass(frozen=True, slots=True, repr=False)
class ScratchOwnershipUncertainty:
    """No valid receipt established ownership or terminal absence."""


@dataclass(frozen=True, repr=False)
class ScratchCleanupDebt:
    """Exact private identity retained for bounded cleanup retry."""

    _name: str
    _parent: _Identity | None
    _directory: _Identity | None
    _object: _Identity | None
    _receipt: _Identity | None
    _receipt_modes: tuple[int, ...]
    _uid: int
    _gid: int


@dataclass(repr=False)
class ScratchReceiptAcquisition:
    """Known receipt identity retained across partial creation."""

    identity: _Identity | None = None


class ScratchReceiptFailureKind(Enum):
    """Closed receipt primitive failures."""

    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    IO = "io"


class ScratchReceiptError(Exception):
    """Closed receipt failure without receipt content or names."""

    def __init__(self, kind: ScratchReceiptFailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


ScratchReconciliation = ScratchHistoricalOwnership | ScratchOwnershipUncertainty


def scratch_name(token: bytes) -> str:
    """Derive the sole scratch directory name for a core-generated token."""
    _validate_token(token)
    return _NAME_PREFIX + token.hex()


def current_receipt_context(operation: ScratchOperation) -> ScratchReceiptContext:
    """Build a typed context for direct private callers in the current identity."""
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return ScratchReceiptContext(operation, IdentityExpectation(os.geteuid(), os.getegid(), groups))


def matches_receipt_context(context: ScratchReceiptContext) -> bool:
    """Return whether the current process still has the bound execution identity."""
    return _matches_current_identity(context.identity)


def create_receipt(
    directory_fd: int,
    token: bytes,
    context: ScratchReceiptContext,
    parent: _Identity,
    directory: _Identity,
    data: _Identity,
    gid: int,
    length: int,
    acquisition: ScratchReceiptAcquisition,
    *,
    expires_at: float | None,
) -> ScratchOwnership:
    """Create and validate the fixed immutable receipt exactly once."""
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        _check_deadline(expires_at)
        try:
            descriptor = os.open(_RECEIPT_NAME, flags, _RECEIPT_BUILD_MODE, dir_fd=directory_fd)
        except OSError as error:
            kind = ScratchReceiptFailureKind.CONFLICT if error.errno == errno.EEXIST else ScratchReceiptFailureKind.IO
            raise ScratchReceiptError(kind) from None
        observed = _fstat(descriptor)
        acquisition.identity = _identity(observed)
        assert acquisition.identity is not None
        ownership = ScratchOwnership(token, context, parent, directory, data, gid, length, acquisition.identity)
        content = _encode_receipt(ownership)
        _require_receipt_stat(observed, context.identity.euid, gid, _RECEIPT_BUILD_MODE)
        _write_all(descriptor, content, expires_at)
        try:
            os.fchmod(descriptor, _RECEIPT_MODE)
        except OSError:
            raise ScratchReceiptError(ScratchReceiptFailureKind.IO) from None
        _check_deadline(expires_at)
        _require_unchanged_receipt(directory_fd, descriptor, ownership, len(content))
        observed_content = _pread_bounded(descriptor, expires_at)
        if not hmac.compare_digest(observed_content, content):
            raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)
        _check_deadline(expires_at)
        return ownership
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def validate_receipt(
    directory_fd: int,
    ownership: ScratchOwnership,
    *,
    expires_at: float | None,
) -> None:
    """Require the exact immutable receipt before active scratch access."""
    _check_deadline(expires_at)
    if not _matches_current_identity(ownership._context.identity):
        raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)
    descriptor = _open_receipt(directory_fd)
    try:
        observed = _fstat(descriptor)
        _require_receipt_stat(observed, ownership._context.identity.euid, ownership._gid, _RECEIPT_MODE)
        if _identity(observed) != ownership._receipt:
            raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)
        named = _stat_at(directory_fd, _RECEIPT_NAME)
        if named is None or _identity(named) != ownership._receipt:
            raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)
        content = _pread_bounded(descriptor, expires_at)
        if not hmac.compare_digest(content, _encode_receipt(ownership)):
            raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)
        _require_unchanged_receipt(directory_fd, descriptor, ownership, len(content))
        _check_deadline(expires_at)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def reconcile_scratch_ownership(
    parent_fd: int,
    token: bytes,
    context: ScratchReceiptContext,
    *,
    expires_at: float | None = None,
) -> ScratchReconciliation:
    """Recover historical ownership from one exact valid receipt without mutation."""
    name = scratch_name(token)
    if not isinstance(context, ScratchReceiptContext):
        raise ValueError("Scratch receipt context has an invalid type")
    if not _matches_current_identity(context.identity):
        return ScratchOwnershipUncertainty()
    directory_fd: int | None = None
    receipt_fd: int | None = None
    try:
        _check_deadline(expires_at)
        parent = os.fstat(parent_fd)
        named_directory = _stat_at(parent_fd, name)
        if named_directory is None:
            return ScratchOwnershipUncertainty()
        _require_initial_stat(named_directory, context.identity, directory=True, mode=_DIRECTORY_MODE)
        directory_fd = _open_directory(parent_fd, name)
        directory = os.fstat(directory_fd)
        _require_initial_stat(directory, context.identity, directory=True, mode=_DIRECTORY_MODE)
        if _identity(directory) != _identity(named_directory):
            return ScratchOwnershipUncertainty()
        receipt_fd = _open_receipt(directory_fd)
        receipt_stat = os.fstat(receipt_fd)
        _require_initial_stat(receipt_stat, context.identity, directory=False, mode=_RECEIPT_MODE)
        named_receipt = _stat_at(directory_fd, _RECEIPT_NAME)
        if named_receipt is None or _identity(named_receipt) != _identity(receipt_stat):
            return ScratchOwnershipUncertainty()
        content = _pread_bounded(receipt_fd, expires_at)
        ownership = _decode_receipt(content, token, context, _identity(receipt_stat))
        _require_unchanged_receipt(directory_fd, receipt_fd, ownership, len(content))
        if ownership._parent != _identity(parent) or ownership._directory != _identity(directory):
            return ScratchOwnershipUncertainty()
        uid = context.identity.euid
        _require_directory_stat(directory, uid, ownership._gid)
        _require_receipt_stat(receipt_stat, uid, ownership._gid, _RECEIPT_MODE)
        data = _stat_at(directory_fd, _DATA_NAME)
        names = _list_directory(directory_fd)
        expected_names = {_RECEIPT_NAME} if data is None else {_DATA_NAME, _RECEIPT_NAME}
        if names != expected_names:
            return ScratchOwnershipUncertainty()
        if data is not None:
            _require_data_stat(data, uid, ownership._gid)
            if _identity(data) != ownership._data or data.st_size > ownership._length:
                return ScratchOwnershipUncertainty()
        _check_deadline(expires_at)
        return ScratchHistoricalOwnership(ownership)
    except ScratchReceiptError as error:
        if error.kind is ScratchReceiptFailureKind.DEADLINE:
            raise
        return ScratchOwnershipUncertainty()
    except (OSError, ValueError):
        return ScratchOwnershipUncertainty()
    finally:
        try:
            if receipt_fd is not None:
                with suppress(OSError):
                    os.close(receipt_fd)
        finally:
            if directory_fd is not None:
                with suppress(OSError):
                    os.close(directory_fd)


def cleanup_owned_scratch(parent_fd: int, debt: ScratchCleanupDebt) -> ScratchReceiptFailureKind | None:
    """Remove only exact acquired objects, leaving the ownership receipt until last."""
    try:
        parent = _fstat(parent_fd)
        if debt._parent is not None and _identity(parent) != debt._parent:
            return ScratchReceiptFailureKind.CONFLICT
        directory_stat = _stat_at(parent_fd, debt._name)
        if directory_stat is None:
            return None
        if debt._directory is None or not matches_scratch_directory(
            directory_stat,
            debt._directory,
            debt._uid,
            debt._gid,
        ):
            return ScratchReceiptFailureKind.CONFLICT
        directory_fd = _open_directory(parent_fd, debt._name)
        try:
            if not matches_scratch_directory(
                _fstat(directory_fd),
                debt._directory,
                debt._uid,
                debt._gid,
            ):
                return ScratchReceiptFailureKind.CONFLICT
            names = _list_directory(directory_fd)
            if names - {_DATA_NAME, _RECEIPT_NAME}:
                return ScratchReceiptFailureKind.CONFLICT
            if _DATA_NAME in names:
                data = _stat_at(directory_fd, _DATA_NAME)
                if (
                    debt._object is None
                    or data is None
                    or not matches_scratch_data(data, debt._object, debt._uid, debt._gid)
                ):
                    return ScratchReceiptFailureKind.CONFLICT
                try:
                    os.unlink(_DATA_NAME, dir_fd=directory_fd)
                except OSError:
                    return ScratchReceiptFailureKind.IO
            names = _list_directory(directory_fd)
            if names - {_RECEIPT_NAME}:
                return ScratchReceiptFailureKind.CONFLICT
            if _RECEIPT_NAME in names:
                receipt = _stat_at(directory_fd, _RECEIPT_NAME)
                if (
                    debt._receipt is None
                    or receipt is None
                    or not matches_scratch_receipt(
                        receipt,
                        debt._receipt,
                        debt._uid,
                        debt._gid,
                        debt._receipt_modes,
                    )
                ):
                    return ScratchReceiptFailureKind.CONFLICT
                try:
                    os.unlink(_RECEIPT_NAME, dir_fd=directory_fd)
                except OSError:
                    return ScratchReceiptFailureKind.IO
            if _list_directory(directory_fd):
                return ScratchReceiptFailureKind.CONFLICT
        finally:
            with suppress(OSError):
                os.close(directory_fd)
        final = _stat_at(parent_fd, debt._name)
        if final is None:
            return None
        if not matches_scratch_directory(final, debt._directory, debt._uid, debt._gid):
            return ScratchReceiptFailureKind.CONFLICT
        try:
            os.rmdir(debt._name, dir_fd=parent_fd)
        except OSError:
            return ScratchReceiptFailureKind.IO
        return None
    except ScratchReceiptError as error:
        return error.kind


def _encode_receipt(ownership: ScratchOwnership) -> bytes:
    _validate_token(ownership._token)
    if not isinstance(ownership._context, ScratchReceiptContext):
        raise ValueError("Scratch receipt context has an invalid type")
    if ownership._length < 0 or ownership._length > _MAX_LENGTH:
        raise ValueError("Scratch length is outside the supported range")
    document = _receipt_document(ownership)
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > _MAX_RECEIPT_BYTES:
        raise ValueError("Scratch receipt exceeds its bound")
    return encoded


def _decode_receipt(
    content: bytes,
    token: bytes,
    context: ScratchReceiptContext,
    receipt: _Identity,
) -> ScratchOwnership:
    if not content or len(content) > _MAX_RECEIPT_BYTES or not content.isascii():
        raise ValueError("Invalid scratch receipt")

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate scratch receipt field")
            result[key] = value
        return result

    value = json.loads(content, object_pairs_hook=object_pairs)
    if type(value) is not dict or set(value) != {
        "artifact_gid",
        "context",
        "data",
        "directory",
        "length",
        "parent",
        "token",
        "version",
    }:
        raise ValueError("Invalid scratch receipt")
    if value["version"] != 1 or value["token"] != token.hex():
        raise ValueError("Invalid scratch receipt")
    context_value = value["context"]
    if type(context_value) is not dict or set(context_value) != {
        "egid",
        "euid",
        "groups_sha256",
        "operation",
    }:
        raise ValueError("Invalid scratch receipt")
    if (
        context_value["operation"] != context.operation.value
        or context_value["euid"] != context.identity.euid
        or context_value["egid"] != context.identity.egid
        or context_value["groups_sha256"] != _groups_digest(context.identity).hex()
    ):
        raise ValueError("Invalid scratch receipt")
    length = value["length"]
    if type(length) is not int or length < 0 or length > _MAX_LENGTH:
        raise ValueError("Invalid scratch receipt")
    gid = value["artifact_gid"]
    if type(gid) is not int or not 0 <= gid <= _MAX_IDENTITY_NUMBER:
        raise ValueError("Invalid scratch receipt")
    ownership = ScratchOwnership(
        bytes(token),
        context,
        _decode_identity(value["parent"]),
        _decode_identity(value["directory"]),
        _decode_identity(value["data"]),
        gid,
        length,
        receipt,
    )
    if not hmac.compare_digest(_encode_receipt(ownership), content):
        raise ValueError("Noncanonical scratch receipt")
    return ownership


def _receipt_document(ownership: ScratchOwnership) -> dict[str, object]:
    return {
        "artifact_gid": ownership._gid,
        "context": {
            "egid": ownership._context.identity.egid,
            "euid": ownership._context.identity.euid,
            "groups_sha256": _groups_digest(ownership._context.identity).hex(),
            "operation": ownership._context.operation.value,
        },
        "data": _encode_identity(ownership._data),
        "directory": _encode_identity(ownership._directory),
        "length": ownership._length,
        "parent": _encode_identity(ownership._parent),
        "token": ownership._token.hex(),
        "version": 1,
    }


def _groups_digest(identity: IdentityExpectation) -> bytes:
    digest = hashlib.sha256()
    digest.update(len(identity.groups).to_bytes(4, "big"))
    for group in identity.groups:
        digest.update(group.to_bytes(4, "big"))
    return digest.digest()


def _encode_identity(identity: _Identity) -> dict[str, int]:
    return {"device": identity.device, "inode": identity.inode}


def _decode_identity(value: object) -> _Identity:
    if type(value) is not dict or set(value) != {"device", "inode"}:
        raise ValueError("Invalid scratch identity")
    device = value["device"]
    inode = value["inode"]
    if (
        type(device) is not int
        or not 0 <= device <= _MAX_IDENTITY_NUMBER
        or type(inode) is not int
        or not 0 <= inode <= _MAX_IDENTITY_NUMBER
    ):
        raise ValueError("Invalid scratch identity")
    return _Identity(device, inode)


def _validate_token(token: bytes) -> None:
    if type(token) is not bytes or len(token) != _TOKEN_BYTES:
        raise ValueError("Scratch token must contain exactly 16 bytes")


def _matches_current_identity(expected: IdentityExpectation) -> bool:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return os.geteuid() == expected.euid and os.getegid() == expected.egid and groups == expected.groups


def _write_all(descriptor: int, content: bytes, expires_at: float | None) -> None:
    offset = 0
    while offset < len(content):
        _check_deadline(expires_at)
        try:
            written = os.write(descriptor, content[offset:])
        except OSError:
            raise ScratchReceiptError(ScratchReceiptFailureKind.IO) from None
        if written <= 0:
            raise ScratchReceiptError(ScratchReceiptFailureKind.IO)
        offset += written
    _check_deadline(expires_at)


def _pread_bounded(descriptor: int, expires_at: float | None) -> bytes:
    result = bytearray()
    while len(result) <= _MAX_RECEIPT_BYTES:
        _check_deadline(expires_at)
        try:
            block = os.pread(descriptor, _MAX_RECEIPT_BYTES + 1 - len(result), len(result))
        except OSError:
            raise ScratchReceiptError(ScratchReceiptFailureKind.IO) from None
        if not block:
            break
        result.extend(block)
    if not result or len(result) > _MAX_RECEIPT_BYTES:
        raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)
    _check_deadline(expires_at)
    return bytes(result)


def _open_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        kind = (
            ScratchReceiptFailureKind.UNSUPPORTED
            if error.errno in {errno.ELOOP, errno.ENOTDIR}
            else ScratchReceiptFailureKind.IO
        )
        raise ScratchReceiptError(kind) from None


def _open_receipt(directory_fd: int) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        return os.open(_RECEIPT_NAME, flags, dir_fd=directory_fd)
    except OSError as error:
        if error.errno == errno.ENOENT:
            kind = ScratchReceiptFailureKind.CONFLICT
        elif error.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
            kind = ScratchReceiptFailureKind.UNSUPPORTED
        else:
            kind = ScratchReceiptFailureKind.IO
        raise ScratchReceiptError(kind) from None


def _fstat(descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError:
        raise ScratchReceiptError(ScratchReceiptFailureKind.IO) from None


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise ScratchReceiptError(ScratchReceiptFailureKind.IO) from None


def _list_directory(directory_fd: int) -> set[str]:
    try:
        return set(os.listdir(directory_fd))
    except OSError:
        raise ScratchReceiptError(ScratchReceiptFailureKind.IO) from None


def _require_initial_stat(
    observed: os.stat_result,
    expected: IdentityExpectation,
    *,
    directory: bool,
    mode: int,
) -> None:
    if directory:
        supported = stat.S_ISDIR(observed.st_mode)
    else:
        supported = stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1
    if not supported:
        raise ScratchReceiptError(ScratchReceiptFailureKind.UNSUPPORTED)
    if observed.st_uid != expected.euid or stat.S_IMODE(observed.st_mode) != mode:
        raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)


def _require_directory_stat(observed: os.stat_result, uid: int, gid: int) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise ScratchReceiptError(ScratchReceiptFailureKind.UNSUPPORTED)
    if not matches_scratch_directory(observed, _identity(observed), uid, gid):
        raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)


def _require_data_stat(observed: os.stat_result, uid: int, gid: int) -> None:
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ScratchReceiptError(ScratchReceiptFailureKind.UNSUPPORTED)
    if not matches_scratch_data(observed, _identity(observed), uid, gid):
        raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)


def _require_receipt_stat(observed: os.stat_result, uid: int, gid: int, mode: int) -> None:
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ScratchReceiptError(ScratchReceiptFailureKind.UNSUPPORTED)
    if not matches_scratch_receipt(observed, _identity(observed), uid, gid, (mode,)):
        raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)


def _require_unchanged_receipt(
    directory_fd: int,
    descriptor: int,
    ownership: ScratchOwnership,
    length: int,
) -> None:
    observed = _fstat(descriptor)
    _require_receipt_stat(observed, ownership._context.identity.euid, ownership._gid, _RECEIPT_MODE)
    if _identity(observed) != ownership._receipt or observed.st_size != length:
        raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)
    named = _stat_at(directory_fd, _RECEIPT_NAME)
    if named is None or _identity(named) != ownership._receipt:
        raise ScratchReceiptError(ScratchReceiptFailureKind.CONFLICT)
    _require_receipt_stat(named, ownership._context.identity.euid, ownership._gid, _RECEIPT_MODE)


def matches_scratch_directory(observed: os.stat_result, identity: _Identity, uid: int, gid: int) -> bool:
    """Match one exact private scratch directory."""
    return (
        stat.S_ISDIR(observed.st_mode)
        and _identity(observed) == identity
        and observed.st_uid == uid
        and observed.st_gid == gid
        and stat.S_IMODE(observed.st_mode) == _DIRECTORY_MODE
    )


def matches_scratch_data(observed: os.stat_result, identity: _Identity, uid: int, gid: int) -> bool:
    """Match one exact private scratch data object."""
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and _identity(observed) == identity
        and observed.st_uid == uid
        and observed.st_gid == gid
        and stat.S_IMODE(observed.st_mode) == _DATA_MODE
    )


def matches_scratch_receipt(
    observed: os.stat_result,
    identity: _Identity,
    uid: int,
    gid: int,
    modes: tuple[int, ...],
) -> bool:
    """Match one exact private scratch receipt in an allowed lifecycle mode."""
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and _identity(observed) == identity
        and observed.st_uid == uid
        and observed.st_gid == gid
        and stat.S_IMODE(observed.st_mode) in modes
    )


def _identity(observed: os.stat_result) -> _Identity:
    return _Identity(observed.st_dev, observed.st_ino)


def _check_deadline(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise ScratchReceiptError(ScratchReceiptFailureKind.DEADLINE)
