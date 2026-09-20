"""Closed stdlib-only protocol for Linux file metadata operations."""

from __future__ import annotations

import base64
import binascii
import json
import math
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._file_metadata import (
    MetadataEffect,
    MetadataFailureKind,
    MetadataPhase,
    MetadataStep,
)
from ._file_paths import normalized_relative_path, normalized_root
from ._file_stat import FileRevision, FileStat
from ._file_wire import valid_nonce
from ._helper_identity import IdentityExpectation, decode_identity

MAX_REQUEST_BYTES = 32_768
MAX_PATH_BYTES = 4_096
_MAX_ID = 2**32 - 1
_MAX_STAT_VALUE = 2**64 - 1
_MIN_TIME_NS = -(2**63)
_MAX_TIME_NS = 2**63 - 1
_REQUEST_FIELDS = frozenset(
    {"gid", "identity", "mode", "nonce", "operation", "path", "remaining_seconds", "root", "uid", "version"}
)
_REVISION_FIELDS = frozenset(
    {"changed_ns", "device", "gid", "inode", "link_count", "mode", "modified_ns", "size", "uid", "version"}
)
_STEP_ORDER = {step: index for index, step in enumerate(MetadataStep)}


class FileMetadataOperation(StrEnum):
    SET_METADATA = "set_metadata"
    ENSURE_DIRECTORY = "ensure_directory"


class FileMetadataResultKind(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class FileMetadataFailureCode(StrEnum):
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
    METADATA = "metadata"


class FileMetadataRequestError(ValueError):
    """A request violated the closed schema without retaining its contents."""

    def __init__(self, failure: FileMetadataFailureCode) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FileMetadataControlError(ValueError):
    """A response body violated the closed schema without retaining its contents."""

    def __init__(self) -> None:
        super().__init__("invalid file metadata control body")


@dataclass(frozen=True, slots=True, repr=False)
class FileMetadataRequest:
    nonce: str
    operation: FileMetadataOperation
    root_path: str
    relative_path: str
    uid: int
    gid: int
    mode: int
    remaining_seconds: float | None
    identity: IdentityExpectation


@dataclass(frozen=True, slots=True, repr=False)
class FileMetadataResultControl:
    kind: FileMetadataResultKind
    revision: FileRevision


@dataclass(frozen=True, slots=True)
class FileMetadataFailureControl:
    code: FileMetadataFailureCode
    kind: MetadataFailureKind | None = None
    phase: MetadataPhase | None = None
    completed_steps: tuple[MetadataStep, ...] = ()
    attempted_step: MetadataStep | None = None

    @property
    def effect(self) -> MetadataEffect | None:
        if self.code is not FileMetadataFailureCode.METADATA:
            return None
        if self.attempted_step is not None:
            return MetadataEffect.UNCERTAIN
        if self.completed_steps:
            return MetadataEffect.PARTIAL
        return MetadataEffect.UNCHANGED


def _invalid_request() -> FileMetadataRequestError:
    return FileMetadataRequestError(FileMetadataFailureCode.INVALID_REQUEST)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _load_json(data: bytes, *, request: bool) -> dict[str, Any]:
    failed = False
    value: Any = None
    try:
        value = json.loads(data.decode("ascii"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        failed = True
    if failed or type(value) is not dict:
        if request:
            raise _invalid_request()
        raise FileMetadataControlError
    return value


def _decode_base64_text(value: object) -> str:
    failed = False
    decoded = b""
    encoded = b""
    if type(value) is not str:
        raise _invalid_request()
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error):
        failed = True
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
    failed = False
    identity: IdentityExpectation | None = None
    try:
        identity = decode_identity(value)
    except ValueError:
        failed = True
    if failed or identity is None:
        raise _invalid_request()
    return identity


def _bounded_integer(value: object, minimum: int, maximum: int, *, request: bool) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        if request:
            raise _invalid_request()
        raise FileMetadataControlError
    return value


def _encode_revision(revision: FileRevision) -> dict[str, object]:
    if revision.digest is not None:
        raise FileMetadataControlError
    observed = revision.stat
    return {
        "changed_ns": observed.changed_ns,
        "device": observed.device,
        "gid": observed.gid,
        "inode": observed.inode,
        "link_count": observed.link_count,
        "mode": observed.mode,
        "modified_ns": observed.modified_ns,
        "size": observed.size,
        "uid": observed.uid,
        "version": 1,
    }


def _decode_revision(value: object) -> FileRevision:
    if (
        type(value) is not dict
        or set(value) != _REVISION_FIELDS
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise FileMetadataControlError
    observed = FileStat(
        device=_bounded_integer(value["device"], 0, _MAX_STAT_VALUE, request=False),
        inode=_bounded_integer(value["inode"], 1, _MAX_STAT_VALUE, request=False),
        mode=_bounded_integer(value["mode"], 0, 0o177777, request=False),
        link_count=_bounded_integer(value["link_count"], 1, _MAX_STAT_VALUE, request=False),
        uid=_bounded_integer(value["uid"], 0, _MAX_ID, request=False),
        gid=_bounded_integer(value["gid"], 0, _MAX_ID, request=False),
        size=_bounded_integer(value["size"], 0, _MAX_STAT_VALUE, request=False),
        modified_ns=_bounded_integer(value["modified_ns"], _MIN_TIME_NS, _MAX_TIME_NS, request=False),
        changed_ns=_bounded_integer(value["changed_ns"], _MIN_TIME_NS, _MAX_TIME_NS, request=False),
    )
    if not (stat.S_ISREG(observed.mode) or stat.S_ISDIR(observed.mode)) or (
        stat.S_ISREG(observed.mode) and observed.link_count != 1
    ):
        raise FileMetadataControlError
    return FileRevision(observed)


def encode_file_metadata_request(request: FileMetadataRequest) -> bytes:
    """Encode trusted host values and enforce the guest's complete schema."""
    failed = False
    encoded = b""
    try:
        value = {
            "gid": request.gid,
            "identity": {
                "egid": request.identity.egid,
                "euid": request.identity.euid,
                "groups": list(request.identity.groups),
            },
            "mode": request.mode,
            "nonce": request.nonce,
            "operation": request.operation.value,
            "path": base64.b64encode(request.relative_path.encode("utf-8")).decode("ascii"),
            "remaining_seconds": request.remaining_seconds,
            "root": base64.b64encode(request.root_path.encode("utf-8")).decode("ascii"),
            "uid": request.uid,
            "version": 1,
        }
        encoded = _json_bytes(value)
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
        failed = True
    if failed:
        raise _invalid_request()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise FileMetadataRequestError(FileMetadataFailureCode.OVERSIZED_REQUEST)
    decode_file_metadata_request(encoded)
    return encoded


def decode_file_metadata_request(data: bytes) -> FileMetadataRequest:
    """Validate one untrusted canonical request before filesystem access."""
    if type(data) is not bytes:
        raise _invalid_request()
    if len(data) > MAX_REQUEST_BYTES:
        raise FileMetadataRequestError(FileMetadataFailureCode.OVERSIZED_REQUEST)
    value = _load_json(data, request=True)
    failed = False
    canonical = b""
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError, RecursionError):
        failed = True
    if failed or canonical != data or set(value) != _REQUEST_FIELDS:
        raise _invalid_request()
    failed = False
    operation = FileMetadataOperation.SET_METADATA
    try:
        operation = FileMetadataOperation(value["operation"])
    except (TypeError, ValueError):
        failed = True
    if failed or type(value["version"]) is not int or value["version"] != 1:
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
    return FileMetadataRequest(
        nonce=nonce,
        operation=operation,
        root_path=root_path,
        relative_path=relative_path,
        uid=_bounded_integer(value["uid"], 0, _MAX_ID, request=True),
        gid=_bounded_integer(value["gid"], 0, _MAX_ID, request=True),
        mode=_bounded_integer(value["mode"], 0, 0o7777, request=True),
        remaining_seconds=None if remaining is None else float(remaining),
        identity=_identity(value["identity"]),
    )


def empty_file_metadata_body() -> bytes:
    return b"{}"


def parse_empty_file_metadata_body(body: bytes) -> None:
    if body != empty_file_metadata_body():
        raise FileMetadataControlError


def encode_file_metadata_result(result: FileMetadataResultControl) -> bytes:
    return _json_bytes({"result": result.kind.value, "revision": _encode_revision(result.revision)})


def parse_file_metadata_result(body: bytes) -> FileMetadataResultControl:
    value = _load_json(body, request=False)
    if set(value) != {"result", "revision"}:
        raise FileMetadataControlError
    failed = False
    canonical = b""
    kind = FileMetadataResultKind.UNCHANGED
    try:
        canonical = _json_bytes(value)
        kind = FileMetadataResultKind(value["result"])
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise FileMetadataControlError
    return FileMetadataResultControl(kind, _decode_revision(value["revision"]))


def _metadata_failure_is_coherent(failure: FileMetadataFailureControl) -> bool:
    if failure.kind is None or failure.phase is None:
        return False
    completed = failure.completed_steps
    attempted = failure.attempted_step
    if len(set(completed)) != len(completed) or tuple(sorted(completed, key=_STEP_ORDER.__getitem__)) != completed:
        return False
    phase_step = {
        MetadataPhase.CREATION: MetadataStep.CREATION,
        MetadataPhase.OWNERSHIP: MetadataStep.OWNERSHIP,
        MetadataPhase.MODE: MetadataStep.MODE,
    }.get(failure.phase)
    if attempted is not None and attempted is not phase_step:
        return False
    if attempted in completed:
        return False
    if failure.phase is MetadataPhase.OBSERVATION and (completed or attempted is not None):
        return False
    if failure.phase is MetadataPhase.CREATION:
        return (completed == () and attempted in {None, MetadataStep.CREATION}) or (
            completed == (MetadataStep.CREATION,) and attempted is None
        )
    if failure.phase is MetadataPhase.OWNERSHIP and any(
        step not in {MetadataStep.CREATION, MetadataStep.OWNERSHIP} for step in completed
    ):
        return False
    if failure.phase is MetadataPhase.MODE and MetadataStep.MODE in completed:
        return False
    return failure.phase is not MetadataPhase.VERIFICATION or attempted is None


def encode_file_metadata_failure(failure: FileMetadataFailureControl) -> bytes:
    value: dict[str, object] = {"code": failure.code.value}
    if failure.code is FileMetadataFailureCode.METADATA:
        value.update(
            {
                "attempted_step": None if failure.attempted_step is None else failure.attempted_step.value,
                "completed_steps": [step.value for step in failure.completed_steps],
                "kind": None if failure.kind is None else failure.kind.value,
                "phase": None if failure.phase is None else failure.phase.value,
            }
        )
    return _json_bytes(value)


def parse_file_metadata_failure(body: bytes) -> FileMetadataFailureControl:
    value = _load_json(body, request=False)
    if "code" not in value:
        raise FileMetadataControlError
    failed = False
    canonical = b""
    code = FileMetadataFailureCode.INVALID_REQUEST
    try:
        canonical = _json_bytes(value)
        code = FileMetadataFailureCode(value["code"])
    except (TypeError, ValueError):
        failed = True
    if failed or canonical != body:
        raise FileMetadataControlError
    if code is not FileMetadataFailureCode.METADATA:
        if set(value) != {"code"}:
            raise FileMetadataControlError
        return FileMetadataFailureControl(code)
    if set(value) != {"attempted_step", "code", "completed_steps", "kind", "phase"}:
        raise FileMetadataControlError
    failed = False
    kind = MetadataFailureKind.IO
    phase = MetadataPhase.OBSERVATION
    completed: tuple[MetadataStep, ...] = ()
    attempted: MetadataStep | None = None
    try:
        kind = MetadataFailureKind(value["kind"])
        phase = MetadataPhase(value["phase"])
        if type(value["completed_steps"]) is not list:
            raise ValueError
        completed = tuple(MetadataStep(step) for step in value["completed_steps"])
        if value["attempted_step"] is not None:
            attempted = MetadataStep(value["attempted_step"])
    except (TypeError, ValueError):
        failed = True
    failure = FileMetadataFailureControl(code, kind, phase, completed, attempted)
    if failed or not _metadata_failure_is_coherent(failure):
        raise FileMetadataControlError
    return failure
