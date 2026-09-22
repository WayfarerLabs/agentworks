"""Bound file-operation view over one composition-owned operation."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from agentworks.errors import StateError, ValidationError

from . import _file_memory_read
from ._file_operation import FileOperation
from ._file_paths import normalized_relative_path, normalized_root
from ._file_result import (
    reduce_file_inventory,
    reduce_file_metadata,
    reduce_file_remove,
    reduce_file_stat,
)
from ._file_result_transfer import reduce_file_json, reduce_file_memory_read, reduce_file_upload
from ._helper_launcher import IdentityPlan
from ._json import serialize_json_source, validate_json_object
from ._runtime_prerequisite import RuntimeSelection
from .carrier import Deadline
from .files import (
    DirectoryEntry,
    DirectoryLimit,
    FileKind,
    FileMetadata,
    JsonObject,
    JsonStrategy,
    MutationResult,
    NewMetadata,
    ReadResult,
    Revision,
    UploadSource,
    WriteCondition,
    _file_revision_from_revision,
    _private_file_kind,
    _private_json_strategy,
    _private_write_condition,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .carrier import Carrier

_DEFAULT_JSON_MAX_BYTES = 64 * 1_024
_DEFAULT_JSON_MAX_DEPTH = 64
_MAX_UPLOAD_SIZE = (1 << 63) - 1

type _JsonFileStrategy = Literal["replace", "merge-overwrite", "merge-preserve", "skip-existing"]


class _BytesSource:
    """A private exact finite source for byte-value publication."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, limit: int, /) -> bytes:
        if self._offset == len(self._data):
            return b""
        end = min(self._offset + limit, len(self._data))
        result = self._data[self._offset : end]
        self._offset = end
        return result


class _UploadSourceAdapter:
    """Adapt the public source shape to the private upload carrier shape."""

    def __init__(self, source: UploadSource) -> None:
        self._source = source

    def try_read(self, limit: int) -> bytes | None:
        return self._source.read(limit)


class FileAccess:
    """One non-production, bound view of the supplied file-operation custody."""

    def __init__(
        self,
        operation: FileOperation,
        carrier: Carrier,
        *,
        trusted_root: PurePosixPath,
        runtime_selection: RuntimeSelection,
        ordinary_plan: IdentityPlan,
        elevated_plan: IdentityPlan | None,
        entity_kind: str,
        entity_name: str,
        deadline: Callable[[], Deadline],
    ) -> None:
        if type(operation) is not FileOperation:
            raise ValidationError("File access requires an acquired file operation")
        if type(trusted_root) is not PurePosixPath or not normalized_root(str(trusted_root)):
            raise ValidationError("File access requires one trusted normalized root")
        if type(runtime_selection) is not RuntimeSelection:
            raise ValidationError("File access requires an explicit runtime selection")
        if type(ordinary_plan) is not IdentityPlan or (
            elevated_plan is not None and type(elevated_plan) is not IdentityPlan
        ):
            raise ValidationError("File access requires bound identity plans")
        _validate_diagnostic_value(entity_kind, "kind")
        _validate_diagnostic_value(entity_name, "name")
        if not callable(deadline):
            raise ValidationError("File access requires a composition-owned deadline policy")

        self._operation = operation
        self._carrier = carrier
        self._trusted_root = trusted_root
        self._runtime_selection = runtime_selection
        self._ordinary_plan = ordinary_plan
        self._elevated_plan = elevated_plan
        self._entity_kind = entity_kind
        self._entity_name = entity_name
        self._deadline = deadline

    def read_file(self, path: PurePosixPath, *, max_bytes: int, sudo: bool = False) -> ReadResult | None:
        """Read one bounded regular-file snapshot."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValidationError("File read requires a positive byte bound")
        root, leaf, plan, deadline = self._request(path, sudo)
        outcome = _file_memory_read.read_file(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            max_bytes=max_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
            operation=self._operation,
        )
        return reduce_file_memory_read(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def stat(self, path: PurePosixPath, *, sudo: bool = False) -> FileMetadata | None:
        """Observe one supported filesystem object."""
        root, leaf, plan, deadline = self._request(path, sudo)
        outcome = self._operation.stat(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_stat(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def list_directory(
        self,
        path: PurePosixPath,
        *,
        limit: DirectoryLimit,
        sudo: bool = False,
    ) -> tuple[DirectoryEntry, ...]:
        """Inventory one directory within the caller's explicit limits."""
        if type(limit) is not DirectoryLimit:
            raise ValidationError("File inventory requires exact directory limits")
        root, leaf, plan, deadline = self._request(path, sudo)
        outcome = self._operation.list_directory(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            max_entries=limit.max_entries,
            max_depth=limit.max_depth,
            max_encoded_bytes=limit.max_encoded_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_inventory(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def write_file(
        self,
        path: PurePosixPath,
        data: bytes,
        *,
        condition: WriteCondition,
        create_metadata: NewMetadata,
        sudo: bool = False,
    ) -> MutationResult:
        """Publish one exact finite byte value under an explicit condition."""
        if type(data) is not bytes or type(create_metadata) is not NewMetadata:
            raise ValidationError("File publication requires exact bytes and creation metadata")
        return self.upload(
            path,
            _BytesSource(data),
            size=len(data),
            condition=condition,
            create_metadata=create_metadata,
            sudo=sudo,
        )

    def upload(
        self,
        path: PurePosixPath,
        source: UploadSource,
        *,
        size: int,
        condition: WriteCondition,
        create_metadata: NewMetadata,
        sudo: bool = False,
    ) -> MutationResult:
        """Publish one exact finite value from a caller-owned source."""
        root, leaf, plan, deadline = self._request(path, sudo)
        source = _validate_upload_source(source)
        if type(size) is not int or not 0 <= size <= _MAX_UPLOAD_SIZE:
            raise ValidationError("File upload size must be a nonnegative bounded integer")
        if type(create_metadata) is not NewMetadata:
            raise ValidationError("File upload requires exact creation metadata")
        private_condition = _private_write_condition(condition)
        outcome = self._operation.upload(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            source=_UploadSourceAdapter(source),
            size=size,
            condition=private_condition,
            create_metadata=create_metadata,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_upload(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def update_json(
        self,
        path: PurePosixPath,
        document: JsonObject,
        *,
        strategy: JsonStrategy,
        create: bool,
        create_metadata: NewMetadata,
        max_bytes: int = _DEFAULT_JSON_MAX_BYTES,
        max_depth: int = _DEFAULT_JSON_MAX_DEPTH,
        sudo: bool = False,
    ) -> MutationResult:
        """Validate and apply one bounded JSON-object update."""
        root, leaf, plan, deadline = self._request(path, sudo)
        if type(document) is not dict or type(create) is not bool or type(create_metadata) is not NewMetadata:
            raise ValidationError("JSON update requires an object, creation choice, and creation metadata")
        source = _json_source(document, max_bytes=max_bytes, max_depth=max_depth)
        outcome = self._operation.update_json(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            source=source,
            strategy=cast("_JsonFileStrategy", _private_json_strategy(strategy)),
            create=create,
            create_metadata=create_metadata,
            max_bytes=max_bytes,
            max_depth=max_depth,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_json(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def ensure_directory(
        self,
        path: PurePosixPath,
        *,
        metadata: NewMetadata,
        sudo: bool = False,
    ) -> MutationResult:
        """Create or converge exactly one directory and its metadata."""
        root, leaf, plan, deadline = self._request(path, sudo)
        if type(metadata) is not NewMetadata:
            raise ValidationError("Directory creation requires exact metadata")
        outcome = self._operation.ensure_directory(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            trusted_owner=metadata.owner,
            trusted_group=metadata.group,
            mode=metadata.mode,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_metadata(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def set_metadata(
        self,
        path: PurePosixPath,
        *,
        owner: str,
        group: str,
        mode: int,
        sudo: bool = False,
    ) -> MutationResult:
        """Converge ownership and mode for one existing supported object."""
        root, leaf, plan, deadline = self._request(path, sudo)
        metadata = NewMetadata(owner, group, mode)
        outcome = self._operation.set_metadata(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            trusted_owner=metadata.owner,
            trusted_group=metadata.group,
            mode=metadata.mode,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_metadata(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def remove(
        self,
        path: PurePosixPath,
        *,
        expected_kind: FileKind,
        expected: Revision,
        sudo: bool = False,
    ) -> MutationResult:
        """Conditionally remove one observed supported object."""
        root, leaf, plan, deadline = self._request(path, sudo)
        if type(expected) is not Revision:
            raise ValidationError("File removal requires an exact revision")
        outcome = self._operation.remove(
            self._carrier,
            trusted_root_path=root,
            relative_path=leaf,
            expected_kind=_private_file_kind(expected_kind),
            expected_revision=_file_revision_from_revision(expected),
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime_selection,
        )
        return reduce_file_remove(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)

    def _request(self, path: PurePosixPath, sudo: bool) -> tuple[str, str, IdentityPlan, Deadline]:
        plan = self._select_plan(sudo)
        root, leaf = self._decompose(path)
        deadline = self._deadline()
        if type(deadline) is not Deadline:
            raise ValidationError("File access deadline policy must return one deadline")
        return root, leaf, plan, deadline

    def _select_plan(self, sudo: bool) -> IdentityPlan:
        if type(sudo) is not bool:
            raise ValidationError("File operations require an explicit elevation choice")
        if not sudo:
            return self._ordinary_plan
        if self._elevated_plan is None:
            raise StateError("File elevation is unavailable for this bound access")
        return self._elevated_plan

    def _decompose(self, path: PurePosixPath) -> tuple[str, str]:
        if type(path) is not PurePosixPath or not normalized_root(str(path)):
            raise ValidationError("File operation requires one normalized absolute path")
        if path == PurePosixPath("/"):
            raise ValidationError("Filesystem root is not a supported file target")
        if path == self._trusted_root:
            parent = path.parent
            leaf = path.name
        else:
            try:
                relative = path.relative_to(self._trusted_root)
            except ValueError:
                raise ValidationError("File target is outside the trusted root") from None
            parent = self._trusted_root
            leaf = str(relative)
        if not normalized_root(str(parent)) or not normalized_relative_path(leaf):
            raise ValidationError("File operation requires one confined target")
        return str(parent), leaf


def _validate_diagnostic_value(value: object, label: str) -> None:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or any(character in value for character in "\\/\x00\r\n")
    ):
        raise ValidationError(f"File access requires a safe logical entity {label}")


def _validate_upload_source(source: object) -> UploadSource:
    try:
        reader = getattr(source, "read", None)
    except Exception:
        raise ValidationError("File upload requires a nonblocking byte source") from None
    if not callable(reader):
        raise ValidationError("File upload requires a nonblocking byte source")
    return cast("UploadSource", source)


def _json_source(document: JsonObject, *, max_bytes: int, max_depth: int) -> bytes:
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return serialize_json_source(validate_json_object(encoded, max_bytes=max_bytes, max_depth=max_depth))
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ValidationError("JSON update requires a bounded valid JSON object") from None
