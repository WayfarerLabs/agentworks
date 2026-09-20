"""Closed stdlib-only protocol for one bounded directory inventory."""

from __future__ import annotations

import base64
import binascii
import json
import math
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._file_inventory import FileInventoryEntry, encode_inventory
from ._file_objects import FileKind
from ._file_paths import normalized_relative_path, normalized_root
from ._file_stat import FileRevision, FileStat
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity

MAX_REQUEST_BYTES = 32_768
MAX_PATH_BYTES = 4_096
MAX_ENTRIES = 4_096
MAX_DEPTH = 8
MAX_ENCODED_BYTES = 4 * 1024 * 1024
_MAX_ID = 2**32 - 1
_MAX_STAT_VALUE = 2**64 - 1
_MIN_TIME_NS = -(2**63)
_MAX_TIME_NS = 2**63 - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_REQUEST_FIELDS = frozenset(
    {
        "identity",
        "max_depth",
        "max_encoded_bytes",
        "max_entries",
        "nonce",
        "operation",
        "path",
        "remaining_seconds",
        "root",
        "version",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "changed_ns",
        "device",
        "gid",
        "inode",
        "link_count",
        "mode",
        "modified_ns",
        "relative_path",
        "size",
        "uid",
    }
)


class FileInventoryFailureCode(StrEnum):
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
    TARGET_REFUSED = "target_refused"
    UNSUPPORTED_OBJECT = "unsupported_object"
    LIMIT = "limit"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    IO = "io"


class FileInventoryRequestError(ValueError):
    """A request violated the closed schema without retaining its contents."""

    def __init__(self, failure: FileInventoryFailureCode) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FileInventoryControlError(ValueError):
    """A response body violated the closed schema without retaining its contents."""

    def __init__(self) -> None:
        super().__init__("invalid file-inventory control body")


@dataclass(frozen=True, slots=True, repr=False)
class FileInventoryRequest:
    nonce: str
    root_path: str
    relative_path: str
    max_entries: int
    max_depth: int
    max_encoded_bytes: int
    identity: IdentityExpectation
    remaining_seconds: float | None


@dataclass(frozen=True, slots=True, repr=False)
class FileInventoryResultControl:
    length: int
    digest: bytes


def _invalid_request() -> FileInventoryRequestError:
    return FileInventoryRequestError(FileInventoryFailureCode.INVALID_REQUEST)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _load_json(data: bytes, *, request: bool) -> Any:
    failed = False
    value: Any = None
    try:
        value = json.loads(data.decode("ascii" if request else "utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        failed = True
    if failed:
        if request:
            raise _invalid_request()
        raise FileInventoryControlError
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
    try:
        return decode_identity(value)
    except ValueError:
        raise _invalid_request() from None


def _bounded_integer(value: object, minimum: int, maximum: int, *, request: bool) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        if request:
            raise _invalid_request()
        raise FileInventoryControlError
    return value


def encode_file_inventory_request(request: FileInventoryRequest) -> bytes:
    """Encode trusted host values and enforce the guest's complete schema."""
    value = {
        "identity": {
            "egid": request.identity.egid,
            "euid": request.identity.euid,
            "groups": list(request.identity.groups),
        },
        "max_depth": request.max_depth,
        "max_encoded_bytes": request.max_encoded_bytes,
        "max_entries": request.max_entries,
        "nonce": request.nonce,
        "operation": "list",
        "path": base64.b64encode(request.relative_path.encode("utf-8")).decode("ascii"),
        "remaining_seconds": request.remaining_seconds,
        "root": base64.b64encode(request.root_path.encode("utf-8")).decode("ascii"),
        "version": 1,
    }
    failed = False
    encoded = b""
    try:
        encoded = _json_bytes(value)
    except (TypeError, UnicodeEncodeError, ValueError):
        failed = True
    if failed:
        raise _invalid_request()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise FileInventoryRequestError(FileInventoryFailureCode.OVERSIZED_REQUEST)
    decode_file_inventory_request(encoded)
    return encoded


def decode_file_inventory_request(data: bytes) -> FileInventoryRequest:
    """Validate one untrusted canonical request before filesystem access."""
    if type(data) is not bytes:
        raise _invalid_request()
    if len(data) > MAX_REQUEST_BYTES:
        raise FileInventoryRequestError(FileInventoryFailureCode.OVERSIZED_REQUEST)
    value = _load_json(data, request=True)
    failed = False
    canonical = b""
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError):
        failed = True
    if failed or type(value) is not dict or set(value) != _REQUEST_FIELDS or canonical != data:
        raise _invalid_request()
    nonce = value["nonce"]
    remaining = value["remaining_seconds"]
    if (
        value["version"] != 1
        or type(value["version"]) is not int
        or value["operation"] != "list"
        or not valid_nonce(nonce)
        or (remaining is not None and (type(remaining) is not float or not math.isfinite(remaining) or remaining < 0))
    ):
        raise _invalid_request()
    maximum_entries = _bounded_integer(value["max_entries"], 1, MAX_ENTRIES, request=True)
    maximum_depth = _bounded_integer(value["max_depth"], 1, MAX_DEPTH, request=True)
    maximum_encoded = _bounded_integer(value["max_encoded_bytes"], 1, MAX_ENCODED_BYTES, request=True)
    root_path = _decode_base64_text(value["root"])
    relative_path = _decode_base64_text(value["path"])
    if not normalized_root(root_path) or not normalized_relative_path(relative_path):
        raise _invalid_request()
    return FileInventoryRequest(
        nonce,
        root_path,
        relative_path,
        maximum_entries,
        maximum_depth,
        maximum_encoded,
        _identity(value["identity"]),
        None if remaining is None else float(remaining),
    )


def empty_file_inventory_body() -> bytes:
    return b"{}"


def parse_empty_file_inventory_body(body: bytes) -> None:
    if body != empty_file_inventory_body():
        raise FileInventoryControlError


def encode_file_inventory_failure(failure: FileInventoryFailureCode) -> bytes:
    return _json_bytes({"code": failure.value})


def parse_file_inventory_failure(body: bytes) -> FileInventoryFailureCode:
    value = _load_json(body, request=False)
    failed = False
    canonical = b""
    failure = FileInventoryFailureCode.INVALID_REQUEST
    try:
        canonical = _json_bytes(value)
        failure = FileInventoryFailureCode(value.get("code"))
    except (AttributeError, TypeError, ValueError):
        failed = True
    if failed or type(value) is not dict or set(value) != {"code"} or canonical != body:
        raise FileInventoryControlError
    return failure


def encode_file_inventory_result(result: FileInventoryResultControl) -> bytes:
    return _json_bytes({"digest": result.digest.hex(), "length": result.length})


def parse_file_inventory_result(body: bytes, max_encoded_bytes: int) -> FileInventoryResultControl:
    value = _load_json(body, request=False)
    failed = False
    canonical = b""
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError):
        failed = True
    if failed or type(value) is not dict or set(value) != {"digest", "length"} or canonical != body:
        raise FileInventoryControlError
    digest_text = value["digest"]
    if (
        type(digest_text) is not str
        or len(digest_text) != 64
        or any(character not in _LOWER_HEX for character in digest_text)
    ):
        raise FileInventoryControlError
    length = _bounded_integer(value["length"], 2, max_encoded_bytes, request=False)
    return FileInventoryResultControl(length, bytes.fromhex(digest_text))


def parse_file_inventory_entries(
    body: bytes,
    *,
    max_entries: int,
    max_depth: int,
    max_encoded_bytes: int,
) -> tuple[FileInventoryEntry, ...]:
    """Validate the exact canonical inventory snapshot into closed typed entries."""
    if len(body) > max_encoded_bytes:
        raise FileInventoryControlError
    value = _load_json(body, request=False)
    if type(value) is not list or len(value) > max_entries:
        raise FileInventoryControlError
    entries: list[FileInventoryEntry] = []
    previous_path: bytes | None = None
    for record in value:
        if type(record) is not dict or set(record) != _ENTRY_FIELDS:
            raise FileInventoryControlError
        relative_path = record["relative_path"]
        if type(relative_path) is not str or not normalized_relative_path(relative_path):
            raise FileInventoryControlError
        failed = False
        encoded_path = b""
        try:
            encoded_path = relative_path.encode("utf-8")
        except UnicodeEncodeError:
            failed = True
        if failed:
            raise FileInventoryControlError
        components = relative_path.split("/")
        if (
            len(encoded_path) > MAX_PATH_BYTES
            or len(components) > max_depth
            or any(len(component.encode("utf-8")) > 255 for component in components)
            or (previous_path is not None and encoded_path <= previous_path)
        ):
            raise FileInventoryControlError
        observed = FileStat(
            device=_bounded_integer(record["device"], 0, _MAX_STAT_VALUE, request=False),
            inode=_bounded_integer(record["inode"], 1, _MAX_STAT_VALUE, request=False),
            mode=_bounded_integer(record["mode"], 0, 0o177777, request=False),
            link_count=_bounded_integer(record["link_count"], 1, _MAX_STAT_VALUE, request=False),
            uid=_bounded_integer(record["uid"], 0, _MAX_ID, request=False),
            gid=_bounded_integer(record["gid"], 0, _MAX_ID, request=False),
            size=_bounded_integer(record["size"], 0, _MAX_STAT_VALUE, request=False),
            modified_ns=_bounded_integer(record["modified_ns"], _MIN_TIME_NS, _MAX_TIME_NS, request=False),
            changed_ns=_bounded_integer(record["changed_ns"], _MIN_TIME_NS, _MAX_TIME_NS, request=False),
        )
        kind = _kind_for_mode(observed.mode)
        if kind is FileKind.REGULAR and observed.link_count != 1:
            raise FileInventoryControlError
        entries.append(FileInventoryEntry(relative_path, FileRevision(observed)))
        previous_path = encoded_path
    result = tuple(entries)
    if encode_inventory(result) != body:
        raise FileInventoryControlError
    return result


def _kind_for_mode(mode: int) -> FileKind:
    if stat.S_ISREG(mode):
        return FileKind.REGULAR
    if stat.S_ISDIR(mode):
        return FileKind.DIRECTORY
    if stat.S_ISSOCK(mode):
        return FileKind.SOCKET
    raise FileInventoryControlError
