"""Closed stdlib-only request and response protocol for bounded file reads."""

from __future__ import annotations

import base64
import binascii
import json
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ._file_stat import FileStat

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_REQUEST_BYTES = 32_768
MAX_RECORD_BODY_BYTES = 4_096
MAX_RECORD_BYTES = 8_192
_MAX_SEQUENCE = 2**63 - 1
_MAX_ID = 2**32 - 1
_MAX_STAT_VALUE = 2**64 - 1
_MIN_TIME_NS = -(2**63)
_MAX_TIME_NS = 2**63 - 1
_MARKER = b"AGWF1"
_LOWER_HEX = frozenset("0123456789abcdef")
_REQUEST_FIELDS = frozenset({"identity", "max_bytes", "nonce", "operation", "path", "root", "version"})
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
    ROOT_REFUSED = "root_refused"
    UNSUPPORTED_OBJECT = "unsupported_object"
    LIMIT = "limit"
    CONFLICT = "conflict"
    IO = "io"


class FileReadRecordKind(StrEnum):
    DATA = "DATA"
    RESULT = "RESULT"
    ABSENT = "ABSENT"
    FAILED = "FAILED"
    FINISHED = "FINISHED"


class FileReadWireError(StrEnum):
    MALFORMED = "malformed"
    OVERSIZED = "oversized"
    NONCE = "nonce"
    SEQUENCE = "sequence"
    TRUNCATED = "truncated"
    CALLBACK = "callback"


class FileReadRequestError(ValueError):
    """A request violated the closed schema without retaining its contents."""

    def __init__(self, failure: FileReadFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FileReadControlError(ValueError):
    """A response body violated the closed schema without retaining its contents."""

    def __init__(self) -> None:
        super().__init__("invalid file-read control body")


@dataclass(frozen=True, slots=True)
class FileReadIdentity:
    euid: int
    egid: int
    groups: tuple[int, ...]


@dataclass(frozen=True, slots=True, repr=False)
class FileReadRequest:
    nonce: str
    root_path: str
    relative_path: str
    max_bytes: int
    identity: FileReadIdentity


@dataclass(frozen=True, slots=True)
class FileReadRecord:
    sequence: int
    kind: FileReadRecordKind
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class FileReadResultControl:
    digest: bytes
    metadata: FileStat


def _invalid_request() -> FileReadRequestError:
    return FileReadRequestError(FileReadFailure.INVALID_REQUEST)


def _valid_nonce(value: object) -> bool:
    return type(value) is str and len(value) == 32 and all(character in _LOWER_HEX for character in value)


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


def _identity(value: object) -> FileReadIdentity:
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
        or any(type(group) is not int or not 0 <= group <= _MAX_ID for group in groups)
        or groups != sorted(set(groups))
        or egid not in groups
    ):
        raise _invalid_request()
    return FileReadIdentity(euid, egid, tuple(groups))


def _normalized_root(path: str) -> bool:
    if path == "/":
        return True
    if not path.startswith("/") or "\x00" in path or path == "//" or (path != "/" and path.endswith("/")):
        return False
    components = path.split("/")[1:]
    return all(component not in ("", ".", "..") for component in components)


def _normalized_leaf(path: str) -> bool:
    return (
        bool(path)
        and not path.startswith("/")
        and "\x00" not in path
        and all(component not in ("", ".", "..") for component in path.split("/"))
    )


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
        or not _valid_nonce(nonce)
        or type(maximum) is not int
        or maximum <= 0
    ):
        raise _invalid_request()
    root_path = _decode_base64_text(value["root"])
    relative_path = _decode_base64_text(value["path"])
    if not _normalized_root(root_path) or not _normalized_leaf(relative_path):
        raise _invalid_request()
    return FileReadRequest(
        nonce=nonce,
        root_path=root_path,
        relative_path=relative_path,
        max_bytes=maximum,
        identity=_identity(value["identity"]),
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


def _parse_uint(token: bytes, maximum: int) -> int | None:
    if (
        not token
        or (len(token) > 1 and token[0] == ord("0"))
        or any(not ord("0") <= byte <= ord("9") for byte in token)
    ):
        return None
    maximum_token = str(maximum).encode("ascii")
    if len(token) > len(maximum_token) or (len(token) == len(maximum_token) and token > maximum_token):
        return None
    return int(token)


def encode_file_read_record(nonce: str, record: FileReadRecord) -> bytes:
    if not _valid_nonce(nonce) or not 0 <= record.sequence <= _MAX_SEQUENCE or len(record.body) > MAX_RECORD_BODY_BYTES:
        raise ValueError("file-read record is outside the version-one bounds")
    encoded_body = base64.b64encode(record.body)
    return (
        b" ".join(
            (
                _MARKER,
                nonce.encode("ascii"),
                str(record.sequence).encode("ascii"),
                record.kind.value.encode("ascii"),
                str(len(record.body)).encode("ascii"),
                encoded_body,
            )
        )
        + b"\n"
    )


class FileReadRecordReader:
    """Strictly consume one nonce's records without retaining rejected bytes."""

    def __init__(self, nonce: str, on_record: Callable[[FileReadRecord], None]) -> None:
        if not _valid_nonce(nonce):
            raise ValueError("file-read nonce must be 32 lowercase hexadecimal characters")
        self._nonce = nonce.encode("ascii")
        self._on_record = on_record
        self._record = bytearray()
        self._next_sequence = 0
        self._error: FileReadWireError | None = None
        self._finished = False

    @property
    def error(self) -> FileReadWireError | None:
        return self._error

    def try_write(self, data: memoryview) -> int:
        """Consume at most one maximum record while applying sink backpressure."""
        consumed = min(len(data), MAX_RECORD_BYTES)
        if self._finished or self._error is not None:
            return consumed
        for byte in data[:consumed]:
            if len(self._record) == MAX_RECORD_BYTES:
                self._fail(FileReadWireError.OVERSIZED)
                break
            self._record.append(byte)
            if byte == ord("\n"):
                record = bytes(self._record)
                self._record.clear()
                self._accept_record(record)
                if self._error is not None:
                    break
        return consumed

    def finish(self) -> None:
        if not self._finished and self._error is None and self._record:
            self._fail(FileReadWireError.TRUNCATED)
        self._finished = True

    def abort(self) -> None:
        """Forget borrowed stream bytes when the attempt cannot return a result."""
        self._record.clear()
        self._finished = True

    def _accept_record(self, record: bytes) -> None:
        fields = record[:-1].split(b" ")
        if len(fields) != 6 or fields[0] != _MARKER:
            self._fail(FileReadWireError.MALFORMED)
            return
        if fields[1] != self._nonce:
            self._fail(FileReadWireError.NONCE)
            return
        sequence = _parse_uint(fields[2], _MAX_SEQUENCE)
        decoded_length = _parse_uint(fields[4], MAX_RECORD_BODY_BYTES)
        failed = False
        body = b""
        try:
            kind = FileReadRecordKind(fields[3].decode("ascii"))
            body = base64.b64decode(fields[5], validate=True)
        except (UnicodeDecodeError, ValueError, binascii.Error):
            failed = True
            kind = FileReadRecordKind.FINISHED
        if (
            failed
            or sequence is None
            or decoded_length is None
            or len(body) != decoded_length
            or base64.b64encode(body) != fields[5]
        ):
            self._fail(FileReadWireError.MALFORMED)
            return
        if sequence != self._next_sequence:
            self._fail(FileReadWireError.SEQUENCE)
            return
        self._next_sequence += 1
        try:
            self._on_record(FileReadRecord(sequence, kind, body))
        except Exception:
            self._fail(FileReadWireError.CALLBACK)
            raise

    def _fail(self, error: FileReadWireError) -> None:
        if self._error is None:
            self._error = error
        self._record.clear()
