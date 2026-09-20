"""Closed stdlib-only protocol for Linux file-object operations."""

from __future__ import annotations

import base64
import binascii
import json
import math
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._file_objects import FileKind, FileObjectFailureKind, FileObjectPhase
from ._file_paths import normalized_relative_path, normalized_root
from ._file_stat import FileRevision, FileStat
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation

MAX_REQUEST_BYTES = 32_768
MAX_PATH_BYTES = 4_096
_MAX_ID = 2**32 - 1
_MAX_STAT_VALUE = 2**64 - 1
_MIN_TIME_NS = -(2**63)
_MAX_TIME_NS = 2**63 - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_COMMON_REQUEST_FIELDS = frozenset({"identity", "nonce", "operation", "path", "remaining_seconds", "root", "version"})
_REVISION_FIELDS = frozenset(
    {
        "changed_ns",
        "device",
        "digest",
        "gid",
        "inode",
        "link_count",
        "mode",
        "modified_ns",
        "size",
        "uid",
        "version",
    }
)


class FileObjectOperation(StrEnum):
    STAT = "stat"
    REMOVE = "remove"


class FileObjectResultKind(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class FileObjectFailureCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    OVERSIZED_REQUEST = "oversized_request"
    NONCE_MISMATCH = "nonce_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    LOCK_UNSUPPORTED = "lock_unsupported"
    LOCK_MISSING = "lock_missing"
    LOCK_UNSAFE = "lock_unsafe"
    LOCK_CONFLICT = "lock_conflict"
    LOCK_DEADLINE = "lock_deadline"
    LOCK_IO = "lock_io"
    ROOT_REFUSED = "root_refused"
    PARENT_REFUSED = "parent_refused"
    OBJECT = "object"


class FileObjectRequestError(ValueError):
    """A request violated the closed schema without retaining its contents."""

    def __init__(self, failure: FileObjectFailureCode) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FileObjectControlError(ValueError):
    """A response body violated the closed schema without retaining its contents."""

    def __init__(self) -> None:
        super().__init__("invalid file-object control body")


@dataclass(frozen=True, slots=True, repr=False)
class FileObjectRequest:
    nonce: str
    operation: FileObjectOperation
    root_path: str
    relative_path: str
    remaining_seconds: float | None
    identity: IdentityExpectation
    expected_kind: FileKind | None = None
    expected_revision: FileRevision | None = None


@dataclass(frozen=True, slots=True, repr=False)
class FileObjectResultControl:
    kind: FileObjectResultKind
    object_kind: FileKind | None = None
    revision: FileRevision | None = None


@dataclass(frozen=True, slots=True)
class FileObjectFailureControl:
    code: FileObjectFailureCode
    kind: FileObjectFailureKind | None = None
    phase: FileObjectPhase | None = None


def _invalid_request() -> FileObjectRequestError:
    return FileObjectRequestError(FileObjectFailureCode.INVALID_REQUEST)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _load_json(data: bytes, *, request: bool) -> Any:
    failed = False
    value: Any = None
    try:
        value = json.loads(data.decode("ascii"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        failed = True
    if failed or type(value) is not dict:
        if request:
            raise _invalid_request()
        raise FileObjectControlError
    return value


def _decode_base64_text(value: object) -> str:
    failed = False
    decoded = b""
    if type(value) is not str:
        raise _invalid_request()
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error):
        failed = True
        encoded = b""
    if failed or len(decoded) > MAX_PATH_BYTES or base64.b64encode(decoded) != encoded:
        raise _invalid_request()
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid_request() from None


def _identity(value: object) -> IdentityExpectation:
    if type(value) is not dict or set(value) != {"egid", "euid", "groups"}:
        raise _invalid_request()
    euid = value["euid"]
    egid = value["egid"]
    groups = value["groups"]
    if (
        type(euid) is not int
        or not 0 <= euid <= _MAX_ID
        or type(egid) is not int
        or not 0 <= egid <= _MAX_ID
        or type(groups) is not list
        or not groups
        or len(groups) > 65_536
        or any(type(group) is not int or not 0 <= group <= _MAX_ID for group in groups)
        or groups != sorted(set(groups))
        or egid not in groups
    ):
        raise _invalid_request()
    return IdentityExpectation(euid, egid, tuple(groups))


def _bounded_integer(value: object, minimum: int, maximum: int, *, request: bool) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        if request:
            raise _invalid_request()
        raise FileObjectControlError
    return value


def _kind_for_mode(mode: int, *, request: bool) -> FileKind:
    if stat.S_ISREG(mode):
        return FileKind.REGULAR
    if stat.S_ISDIR(mode):
        return FileKind.DIRECTORY
    if stat.S_ISSOCK(mode):
        return FileKind.SOCKET
    if request:
        raise _invalid_request()
    raise FileObjectControlError


def _encode_revision(revision: FileRevision) -> dict[str, object]:
    observed = revision.stat
    return {
        "changed_ns": observed.changed_ns,
        "device": observed.device,
        "digest": None if revision.digest is None else revision.digest.hex(),
        "gid": observed.gid,
        "inode": observed.inode,
        "link_count": observed.link_count,
        "mode": observed.mode,
        "modified_ns": observed.modified_ns,
        "size": observed.size,
        "uid": observed.uid,
        "version": 1,
    }


def _decode_revision(value: object, *, request: bool) -> FileRevision:
    if (
        type(value) is not dict
        or set(value) != _REVISION_FIELDS
        or value["version"] != 1
        or type(value["version"]) is not int
    ):
        if request:
            raise _invalid_request()
        raise FileObjectControlError
    digest_value = value["digest"]
    if digest_value is not None and (
        type(digest_value) is not str
        or len(digest_value) != 64
        or any(character not in _LOWER_HEX for character in digest_value)
    ):
        if request:
            raise _invalid_request()
        raise FileObjectControlError
    observed = FileStat(
        device=_bounded_integer(value["device"], 0, _MAX_STAT_VALUE, request=request),
        inode=_bounded_integer(value["inode"], 1, _MAX_STAT_VALUE, request=request),
        mode=_bounded_integer(value["mode"], 0, 0o177777, request=request),
        link_count=_bounded_integer(value["link_count"], 1, _MAX_STAT_VALUE, request=request),
        uid=_bounded_integer(value["uid"], 0, _MAX_ID, request=request),
        gid=_bounded_integer(value["gid"], 0, _MAX_ID, request=request),
        size=_bounded_integer(value["size"], 0, _MAX_STAT_VALUE, request=request),
        modified_ns=_bounded_integer(value["modified_ns"], _MIN_TIME_NS, _MAX_TIME_NS, request=request),
        changed_ns=_bounded_integer(value["changed_ns"], _MIN_TIME_NS, _MAX_TIME_NS, request=request),
    )
    kind = _kind_for_mode(observed.mode, request=request)
    if (kind is FileKind.REGULAR and observed.link_count != 1) or (
        digest_value is not None and kind is not FileKind.REGULAR
    ):
        if request:
            raise _invalid_request()
        raise FileObjectControlError
    return FileRevision(observed, None if digest_value is None else bytes.fromhex(digest_value))


def encode_file_object_request(request: FileObjectRequest) -> bytes:
    """Encode trusted host values and enforce the guest's complete schema."""
    value: dict[str, object] = {
        "identity": {
            "egid": request.identity.egid,
            "euid": request.identity.euid,
            "groups": list(request.identity.groups),
        },
        "nonce": request.nonce,
        "operation": request.operation.value,
        "path": base64.b64encode(request.relative_path.encode("utf-8")).decode("ascii"),
        "remaining_seconds": request.remaining_seconds,
        "root": base64.b64encode(request.root_path.encode("utf-8")).decode("ascii"),
        "version": 1,
    }
    if request.operation is FileObjectOperation.REMOVE:
        value["expected_kind"] = None if request.expected_kind is None else request.expected_kind.value
        value["expected_revision"] = (
            None if request.expected_revision is None else _encode_revision(request.expected_revision)
        )
    try:
        encoded = _json_bytes(value)
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
        raise _invalid_request() from None
    if len(encoded) > MAX_REQUEST_BYTES:
        raise FileObjectRequestError(FileObjectFailureCode.OVERSIZED_REQUEST)
    decode_file_object_request(encoded)
    return encoded


def decode_file_object_request(data: bytes) -> FileObjectRequest:
    """Validate one untrusted canonical request before filesystem access."""
    if type(data) is not bytes:
        raise _invalid_request()
    if len(data) > MAX_REQUEST_BYTES:
        raise FileObjectRequestError(FileObjectFailureCode.OVERSIZED_REQUEST)
    value = _load_json(data, request=True)
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError):
        raise _invalid_request() from None
    if canonical != data:
        raise _invalid_request()
    try:
        operation = FileObjectOperation(value.get("operation"))
    except (TypeError, ValueError):
        raise _invalid_request() from None
    fields = _COMMON_REQUEST_FIELDS
    if operation is FileObjectOperation.REMOVE:
        fields |= {"expected_kind", "expected_revision"}
    if set(value) != fields or value["version"] != 1 or type(value["version"]) is not int:
        raise _invalid_request()
    nonce = value["nonce"]
    remaining = value["remaining_seconds"]
    if not valid_nonce(nonce) or (
        remaining is not None and (type(remaining) is not float or not math.isfinite(remaining) or remaining < 0)
    ):
        raise _invalid_request()
    root_path = _decode_base64_text(value["root"])
    relative_path = _decode_base64_text(value["path"])
    if not normalized_root(root_path) or not normalized_relative_path(relative_path):
        raise _invalid_request()
    expected_kind = None
    expected_revision = None
    if operation is FileObjectOperation.REMOVE:
        try:
            expected_kind = FileKind(value["expected_kind"])
        except (TypeError, ValueError):
            raise _invalid_request() from None
        expected_revision = _decode_revision(value["expected_revision"], request=True)
        if _kind_for_mode(expected_revision.stat.mode, request=True) is not expected_kind:
            raise _invalid_request()
    return FileObjectRequest(
        nonce=nonce,
        operation=operation,
        root_path=root_path,
        relative_path=relative_path,
        remaining_seconds=None if remaining is None else float(remaining),
        identity=_identity(value["identity"]),
        expected_kind=expected_kind,
        expected_revision=expected_revision,
    )


def empty_file_object_body() -> bytes:
    return b"{}"


def parse_empty_file_object_body(body: bytes) -> None:
    if body != empty_file_object_body():
        raise FileObjectControlError


def encode_file_object_result(result: FileObjectResultControl) -> bytes:
    value: dict[str, object] = {"result": result.kind.value}
    if result.kind is FileObjectResultKind.PRESENT:
        value["kind"] = None if result.object_kind is None else result.object_kind.value
        value["revision"] = None if result.revision is None else _encode_revision(result.revision)
    return _json_bytes(value)


def parse_file_object_result(body: bytes, operation: FileObjectOperation) -> FileObjectResultControl:
    value = _load_json(body, request=False)
    try:
        canonical = _json_bytes(value)
        result_kind = FileObjectResultKind(value.get("result"))
    except (TypeError, ValueError):
        raise FileObjectControlError from None
    if canonical != body:
        raise FileObjectControlError
    if result_kind is FileObjectResultKind.PRESENT:
        if operation is not FileObjectOperation.STAT or set(value) != {"kind", "result", "revision"}:
            raise FileObjectControlError
        try:
            object_kind = FileKind(value["kind"])
        except (TypeError, ValueError):
            raise FileObjectControlError from None
        revision = _decode_revision(value["revision"], request=False)
        if revision.digest is not None or _kind_for_mode(revision.stat.mode, request=False) is not object_kind:
            raise FileObjectControlError
        return FileObjectResultControl(result_kind, object_kind, revision)
    if set(value) != {"result"}:
        raise FileObjectControlError
    if operation is FileObjectOperation.STAT and result_kind is not FileObjectResultKind.ABSENT:
        raise FileObjectControlError
    if operation is FileObjectOperation.REMOVE and result_kind not in {
        FileObjectResultKind.CHANGED,
        FileObjectResultKind.UNCHANGED,
    }:
        raise FileObjectControlError
    return FileObjectResultControl(result_kind)


def encode_file_object_failure(failure: FileObjectFailureControl) -> bytes:
    value: dict[str, object] = {"code": failure.code.value}
    if failure.code is FileObjectFailureCode.OBJECT:
        value["kind"] = None if failure.kind is None else failure.kind.value
        value["phase"] = None if failure.phase is None else failure.phase.value
    return _json_bytes(value)


def parse_file_object_failure(body: bytes) -> FileObjectFailureControl:
    value = _load_json(body, request=False)
    try:
        canonical = _json_bytes(value)
        code = FileObjectFailureCode(value.get("code"))
    except (TypeError, ValueError):
        raise FileObjectControlError from None
    if canonical != body:
        raise FileObjectControlError
    if code is not FileObjectFailureCode.OBJECT:
        if set(value) != {"code"}:
            raise FileObjectControlError
        return FileObjectFailureControl(code)
    if set(value) != {"code", "kind", "phase"}:
        raise FileObjectControlError
    try:
        kind = FileObjectFailureKind(value["kind"])
        phase = FileObjectPhase(value["phase"])
    except (TypeError, ValueError):
        raise FileObjectControlError from None
    return FileObjectFailureControl(code, kind, phase)
