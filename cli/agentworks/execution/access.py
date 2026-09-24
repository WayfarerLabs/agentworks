"""Bound file-operation view over one composition-owned operation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from agentworks.errors import StateError, ValidationError

from . import _file_memory_read
from ._diagnostic_values import validate_logical_entity_value
from ._execution_operation import ExecutionOperation
from ._execution_result import check_owned_inline_result, reduce_owned_inline_result
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
from ._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
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
from .models import Command, Input, Lifetime, Output, Script
from .profiles import Protection

if TYPE_CHECKING:
    from collections.abc import Callable

    from .carrier import Carrier
    from .result import ExecutionResult

_DEFAULT_JSON_MAX_BYTES = 64 * 1_024
_DEFAULT_JSON_MAX_DEPTH = 64
_MAX_UPLOAD_SIZE = (1 << 63) - 1
_DEFAULT_INPUT = Input.eof()
_DEFAULT_OUTPUT = Output.capture()

type _JsonFileStrategy = Literal["replace", "merge-overwrite", "merge-preserve", "skip-existing"]


class ExecutionAccess:
    """Private bound view for one foreground DIRECT operation."""

    def __init__(
        self,
        operation: ExecutionOperation,
        carrier: Carrier,
        *,
        runtime_selection: RuntimeSelection,
        ordinary_plan: IdentityPlan,
        elevated_plan: IdentityPlan | None,
        entity_kind: str,
        entity_name: str,
        deadline: Callable[[], Deadline],
    ) -> None:
        if type(operation) is not ExecutionOperation:
            raise ValidationError("Execution access requires an acquired execution operation")
        if type(runtime_selection) is not RuntimeSelection:
            raise ValidationError("Execution access requires an explicit runtime selection")
        if type(ordinary_plan) is not IdentityPlan or (
            elevated_plan is not None and type(elevated_plan) is not IdentityPlan
        ):
            raise ValidationError("Execution access requires bound identity plans")
        validate_logical_entity_value(entity_kind, "kind", subject="Execution access")
        validate_logical_entity_value(entity_name, "name", subject="Execution access")
        if not callable(deadline):
            raise ValidationError("Execution access requires a composition-owned deadline policy")
        self._operation = operation
        self._carrier = carrier
        self._runtime_selection = runtime_selection
        self._ordinary_plan = ordinary_plan
        self._elevated_plan = elevated_plan
        self._entity_kind = entity_kind
        self._entity_name = entity_name
        self._deadline = deadline

    def run(
        self,
        request: Command | Script,
        *,
        profile: Protection,
        lifetime: Lifetime = Lifetime.OPERATION,
        sudo: bool = False,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: Input = _DEFAULT_INPUT,
        output: Output = _DEFAULT_OUTPUT,
        sensitive: bool = False,
        deadline: Deadline | None = None,
        check: bool = False,
    ) -> ExecutionResult:
        """Run one supported inline candidate under existing operation custody."""
        if type(profile) is not Protection or type(lifetime) is not Lifetime:
            raise ValidationError("Foreground execution requires explicit profile and lifetime values")
        if profile is not Protection.DIRECT or lifetime is not Lifetime.OPERATION:
            raise StateError("Requested execution profile or lifetime is unavailable")
        if self._runtime_selection.target_os is not RuntimeTargetOS.LINUX:
            raise StateError("Foreground execution is unavailable on this runtime")
        if type(request) not in {Command, Script} or (
            type(request) is Script and (request.login or request.interactive)
        ):
            raise ValidationError("Foreground execution requires a noninteractive command or script")
        if type(stdin) is not Input or type(output) is not Output:
            raise ValidationError("Foreground execution requires finite input and bounded output")
        if output.max_bytes is not None and output.max_bytes > 4_096:
            raise ValidationError("Foreground capture cannot exceed 4096 bytes")
        if type(sudo) is not bool or type(sensitive) is not bool or type(check) is not bool:
            raise ValidationError("Foreground execution flags must be booleans")
        if sudo and self._elevated_plan is None:
            raise StateError("Execution elevation is unavailable for this bound access")
        if env is not None and not isinstance(env, Mapping):
            raise ValidationError("Foreground environment must be a string mapping")
        if cwd is not None and type(cwd) is not str:
            raise ValidationError("Foreground working directory must be text")
        selected_deadline = self._deadline() if deadline is None else deadline
        if type(selected_deadline) is not Deadline or selected_deadline.expired:
            raise ValidationError("Foreground execution requires a live deadline")
        plan = self._elevated_plan if sudo else self._ordinary_plan
        assert plan is not None
        effective_sensitive = sensitive or stdin.is_sensitive

        outcome = self._operation.run_inline(
            self._carrier,
            request,
            plan=plan,
            deadline=selected_deadline,
            runtime_selection=self._runtime_selection,
            stdin=stdin.data,
            env=env,
            cwd=cwd,
            capture_limit=output.max_bytes,
            sensitive=effective_sensitive,
        )
        if check:
            return check_owned_inline_result(outcome, entity_kind=self._entity_kind, entity_name=self._entity_name)
        return reduce_owned_inline_result(outcome)


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
        validate_logical_entity_value(entity_kind, "kind", subject="File access")
        validate_logical_entity_value(entity_name, "name", subject="File access")
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
