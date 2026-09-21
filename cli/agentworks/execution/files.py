"""Frozen public values for bounded file operations."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._account_protocol import (
    FileOwnershipRequest as _FileOwnershipRequest,
)
from ._account_protocol import (
    FileOwnershipRequestError as _FileOwnershipRequestError,
)
from ._account_protocol import (
    encode_file_ownership_request as _encode_file_ownership_request,
)
from ._file_revision_wire import (
    FileRevisionWireError as _FileRevisionWireError,
)
from ._file_revision_wire import (
    decode_file_revision as _decode_file_revision,
)
from ._file_revision_wire import (
    encode_file_revision as _encode_file_revision,
)

if TYPE_CHECKING:
    from ._file_stat import FileRevision as _FileRevision

_MAX_REVISION_TOKEN_BYTES = 4_096
_OWNERSHIP_VALIDATION_NONCE = "0" * 32

__all__ = [
    "Change",
    "Create",
    "DirectoryEntry",
    "DirectoryLimit",
    "FileKind",
    "FileMetadata",
    "JsonObject",
    "JsonStrategy",
    "JsonValue",
    "Match",
    "MutationResult",
    "NewMetadata",
    "ReadResult",
    "Replace",
    "Revision",
    "WriteCondition",
]


type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class FileKind(StrEnum):
    """Closed filesystem-object kinds exposed by file operations."""

    REGULAR = "regular"
    DIRECTORY = "directory"
    SOCKET = "socket"


class JsonStrategy(StrEnum):
    """Closed JSON update behavior."""

    REPLACE = "replace"
    MERGE_OVERWRITE = "merge-overwrite"
    MERGE_PRESERVE = "merge-preserve"
    SKIP_EXISTING = "skip-existing"


class Change(StrEnum):
    """Whether a complete mutation changed its target."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"


class _RevisionTokenError(ValueError):
    pass


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _decode_revision_token(token: bytes) -> _FileRevision:
    failed = False
    value: object = None
    try:
        value = json.loads(token.decode("ascii"))
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        failed = True
    if failed:
        raise _RevisionTokenError
    revision: _FileRevision | None = None
    try:
        revision = _decode_file_revision(value)
    except _FileRevisionWireError:
        failed = True
    if failed or revision is None:
        raise _RevisionTokenError
    try:
        canonical = _canonical_json(_encode_file_revision(revision))
    except (TypeError, ValueError, RecursionError):
        raise _RevisionTokenError from None
    if canonical != token:
        raise _RevisionTokenError
    return revision


@dataclass(frozen=True, repr=False)
class Revision:
    """Opaque, versioned filesystem-observation evidence."""

    token: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.token) is not bytes or len(self.token) > _MAX_REVISION_TOKEN_BYTES:
            raise ValidationError("Revision requires a bounded canonical token")
        try:
            _decode_revision_token(self.token)
        except _RevisionTokenError:
            raise ValidationError("Revision requires a bounded canonical token") from None


def _revision_from_file_revision(revision: _FileRevision) -> Revision:
    """Convert one private filesystem fact into its opaque public token."""
    return Revision(_canonical_json(_encode_file_revision(revision)))


def _file_revision_from_revision(revision: Revision) -> _FileRevision:
    """Recover the validated private fact carried by one public revision."""
    return _decode_revision_token(revision.token)


def _kind_from_mode(mode: int) -> FileKind:
    if stat.S_ISREG(mode):
        return FileKind.REGULAR
    if stat.S_ISDIR(mode):
        return FileKind.DIRECTORY
    if stat.S_ISSOCK(mode):
        return FileKind.SOCKET
    raise ValidationError("Revision carries an unsupported file kind")


@dataclass(frozen=True)
class FileMetadata:
    """Public metadata projected from one matching revision."""

    kind: FileKind
    size: int
    mode: int
    uid: int
    gid: int
    modified_ns: int
    revision: Revision

    def __post_init__(self) -> None:
        if type(self.kind) is not FileKind or type(self.revision) is not Revision:
            raise ValidationError("File metadata requires closed kind and revision values")
        values = (self.size, self.mode, self.uid, self.gid, self.modified_ns)
        if any(type(value) is not int for value in values):
            raise ValidationError("File metadata numeric fields must be integers")
        observed = _file_revision_from_revision(self.revision).stat
        expected = (
            _kind_from_mode(observed.mode),
            observed.size,
            stat.S_IMODE(observed.mode),
            observed.uid,
            observed.gid,
            observed.modified_ns,
        )
        if (self.kind, *values) != expected:
            raise ValidationError("File metadata must agree with its revision")


@dataclass(frozen=True, repr=False)
class ReadResult:
    """One complete regular-file content snapshot and its metadata."""

    data: bytes = field(repr=False)
    metadata: FileMetadata

    def __post_init__(self) -> None:
        if type(self.data) is not bytes or type(self.metadata) is not FileMetadata:
            raise ValidationError("File read requires exact bytes and metadata values")
        revision = _file_revision_from_revision(self.metadata.revision)
        if (
            self.metadata.kind is not FileKind.REGULAR
            or len(self.data) != self.metadata.size
            or revision.digest is None
            or hashlib.sha256(self.data).digest() != revision.digest
        ):
            raise ValidationError("File read data must match one content-bound regular revision")


@dataclass(frozen=True)
class DirectoryLimit:
    """Caller-selected bounds for one complete directory inventory."""

    max_entries: int = 1_024
    max_depth: int = 1
    max_encoded_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if (
            type(self.max_entries) is not int
            or not 1 <= self.max_entries <= 4_096
            or type(self.max_depth) is not int
            or not 1 <= self.max_depth <= 8
            or type(self.max_encoded_bytes) is not int
            or not 1 <= self.max_encoded_bytes <= 4 * 1_048_576
        ):
            raise ValidationError("Directory limits must be positive and within supported maxima")


@dataclass(frozen=True)
class DirectoryEntry:
    """One relative path and matching metadata in a directory inventory."""

    relative_path: PurePosixPath
    metadata: FileMetadata

    def __post_init__(self) -> None:
        if type(self.relative_path) is not PurePosixPath or type(self.metadata) is not FileMetadata:
            raise ValidationError("Directory entries require exact path and metadata values")
        if (
            self.relative_path.is_absolute()
            or not self.relative_path.parts
            or any(part in {".", ".."} for part in self.relative_path.parts)
            or "\0" in str(self.relative_path)
        ):
            raise ValidationError("Directory entry paths must be nonempty normalized relative paths")


def _validate_ownership_names(owner: object, group: object) -> None:
    if type(owner) is not str or type(group) is not str:
        raise ValidationError("File owner and group must be supported account names")
    try:
        _encode_file_ownership_request(_FileOwnershipRequest(_OWNERSHIP_VALIDATION_NONCE, owner, group))
    except (TypeError, _FileOwnershipRequestError):
        raise ValidationError("File owner and group must be supported account names") from None


@dataclass(frozen=True)
class NewMetadata:
    """Requested ownership and mode for a newly created object."""

    owner: str
    group: str
    mode: int

    def __post_init__(self) -> None:
        _validate_ownership_names(self.owner, self.group)
        if type(self.mode) is not int or not 0 <= self.mode <= 0o7777:
            raise ValidationError("File mode must contain only supported mode bits")


@dataclass(frozen=True)
class Create:
    """Require the destination to be absent."""


@dataclass(frozen=True)
class Replace:
    """Require and unconditionally replace an existing regular file."""


@dataclass(frozen=True)
class Match:
    """Require an existing regular file to match one revision."""

    revision: Revision

    def __post_init__(self) -> None:
        if type(self.revision) is not Revision:
            raise ValidationError("Match requires an exact revision")


type WriteCondition = Create | Replace | Match


@dataclass(frozen=True)
class MutationResult:
    """The confirmed effect and optional resulting object revision."""

    change: Change
    revision: Revision | None

    def __post_init__(self) -> None:
        if type(self.change) is not Change or (self.revision is not None and type(self.revision) is not Revision):
            raise ValidationError("Mutation result requires closed change and revision values")
