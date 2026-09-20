"""Closed stdlib-only request and response protocol for bounded file reads."""

from __future__ import annotations

import base64
import binascii
import json
import math
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._file_paths import normalized_relative_path, normalized_root
from ._file_stat import FileStat
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity

MAX_REQUEST_BYTES = 32_768
_MAX_ID = 2**32 - 1
_MAX_STAT_VALUE = 2**64 - 1
_MIN_TIME_NS = -(2**63)
_MAX_TIME_NS = 2**63 - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_REQUEST_FIELDS = frozenset(
    {"identity", "max_bytes", "nonce", "operation", "path", "remaining_seconds", "root", "version"}
)
_RESULT_FIELDS = frozenset(
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
    }
)


class FileReadFailure(StrEnum):
    """Closed helper failures that reveal no request or filesystem values."""

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
    UNSUPPORTED_OBJECT = "unsupported_object"
    LIMIT = "limit"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    IO = "io"


class FileReadRequestError(ValueError):
    """A request violated the closed schema without retaining its contents."""

    def __init__(self, failure: FileReadFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FileReadControlError(ValueError):
    """A response body violated the closed schema without retaining its contents."""

    def __init__(self) -> None:
        super().__init__("invalid file-read control body")


@dataclass(frozen=True, slots=True, repr=False)
class FileReadRequest:
    nonce: str
    root_path: str
    relative_path: str
    max_bytes: int
    identity: IdentityExpectation
    remaining_seconds: float | None


@dataclass(frozen=True, slots=True, repr=False)
class FileReadResultControl:
    digest: bytes
    metadata: FileStat


def _invalid_request() -> FileReadRequestError:
    return FileReadRequestError(FileReadFailure.INVALID_REQUEST)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


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
    if failed or base64.b64encode(decoded) != encoded:
        raise _invalid_request()
    failed = False
    text = ""
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        failed = True
    if failed:
        raise _invalid_request()
    return text


def _identity(value: object) -> IdentityExpectation:
    failed = False
    identity: IdentityExpectation | None = None
    try:
        identity = decode_identity(value)
    except ValueError:
        failed = True
    if failed or identity is None:
        raise _invalid_request()
    return identity


def _remaining_seconds(value: object) -> float | None:
    if value is None:
        return None
    if type(value) is not float or not math.isfinite(value) or value < 0:
        raise _invalid_request()
    return value


def encode_file_read_request(request: FileReadRequest) -> bytes:
    """Encode trusted host values and enforce the guest's complete boundary schema."""
    value = {
        "identity": {
            "egid": request.identity.egid,
            "euid": request.identity.euid,
            "groups": list(request.identity.groups),
        },
        "max_bytes": request.max_bytes,
        "nonce": request.nonce,
        "operation": "read",
        "path": base64.b64encode(request.relative_path.encode("utf-8")).decode("ascii"),
        "remaining_seconds": request.remaining_seconds,
        "root": base64.b64encode(request.root_path.encode("utf-8")).decode("ascii"),
        "version": 1,
    }
    failed = False
    encoded = b""
    try:
        encoded = _json_bytes(value)
    except ValueError:
        failed = True
    if failed:
        raise _invalid_request()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise FileReadRequestError(FileReadFailure.OVERSIZED_REQUEST)
    decode_file_read_request(encoded)
    return encoded


def decode_file_read_request(data: bytes) -> FileReadRequest:
    """Validate one untrusted canonical request from the carrier input boundary."""
    if type(data) is not bytes:
        raise _invalid_request()
    if len(data) > MAX_REQUEST_BYTES:
        raise FileReadRequestError(FileReadFailure.OVERSIZED_REQUEST)
    failed = False
    value: Any = None
    try:
        value = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        failed = True
    if failed or type(value) is not dict or set(value) != _REQUEST_FIELDS or _json_bytes(value) != data:
        raise _invalid_request()
    nonce = value["nonce"]
    maximum = value["max_bytes"]
    if (
        value["version"] != 1
        or type(value["version"]) is not int
        or value["operation"] != "read"
        or not valid_nonce(nonce)
        or type(maximum) is not int
        or maximum <= 0
    ):
        raise _invalid_request()
    root_path = _decode_base64_text(value["root"])
    relative_path = _decode_base64_text(value["path"])
    if not normalized_root(root_path) or not normalized_relative_path(relative_path):
        raise _invalid_request()
    return FileReadRequest(
        nonce=nonce,
        root_path=root_path,
        relative_path=relative_path,
        max_bytes=maximum,
        identity=_identity(value["identity"]),
        remaining_seconds=_remaining_seconds(value["remaining_seconds"]),
    )


def empty_file_read_body() -> bytes:
    return b"{}"


def parse_empty_file_read_body(body: bytes) -> None:
    if body != empty_file_read_body():
        raise FileReadControlError


def encode_file_read_failure(failure: FileReadFailure) -> bytes:
    return _json_bytes({"code": failure.value})


def parse_file_read_failure(body: bytes) -> FileReadFailure:
    failed = False
    value: Any = None
    try:
        value = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        failed = True
    if failed or type(value) is not dict or set(value) != {"code"} or _json_bytes(value) != body:
        raise FileReadControlError
    failed = False
    failure = FileReadFailure.INVALID_REQUEST
    try:
        failure = FileReadFailure(value["code"])
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise FileReadControlError
    return failure


def encode_file_read_result(result: FileReadResultControl) -> bytes:
    metadata = result.metadata
    return _json_bytes(
        {
            "changed_ns": metadata.changed_ns,
            "device": metadata.device,
            "digest": result.digest.hex(),
            "gid": metadata.gid,
            "inode": metadata.inode,
            "link_count": metadata.link_count,
            "mode": metadata.mode,
            "modified_ns": metadata.modified_ns,
            "size": metadata.size,
            "uid": metadata.uid,
        }
    )


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise FileReadControlError
    return value


def parse_file_read_result(body: bytes, max_bytes: int) -> FileReadResultControl:
    failed = False
    value: Any = None
    try:
        value = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        failed = True
    if failed or type(value) is not dict or set(value) != _RESULT_FIELDS or _json_bytes(value) != body:
        raise FileReadControlError
    digest_text = value["digest"]
    if (
        type(digest_text) is not str
        or len(digest_text) != 64
        or any(character not in _LOWER_HEX for character in digest_text)
    ):
        raise FileReadControlError
    size = _bounded_integer(value["size"], 0, max_bytes)
    metadata = FileStat(
        device=_bounded_integer(value["device"], 0, _MAX_STAT_VALUE),
        inode=_bounded_integer(value["inode"], 1, _MAX_STAT_VALUE),
        mode=_bounded_integer(value["mode"], 0, 0o177777),
        link_count=_bounded_integer(value["link_count"], 1, _MAX_STAT_VALUE),
        uid=_bounded_integer(value["uid"], 0, _MAX_ID),
        gid=_bounded_integer(value["gid"], 0, _MAX_ID),
        size=size,
        modified_ns=_bounded_integer(value["modified_ns"], _MIN_TIME_NS, _MAX_TIME_NS),
        changed_ns=_bounded_integer(value["changed_ns"], _MIN_TIME_NS, _MAX_TIME_NS),
    )
    if metadata.link_count != 1 or not stat.S_ISREG(metadata.mode):
        raise FileReadControlError
    return FileReadResultControl(bytes.fromhex(digest_text), metadata)
