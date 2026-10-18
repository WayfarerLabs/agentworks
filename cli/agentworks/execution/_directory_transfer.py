"""Bounded tree copying composed from ordinary settled file calls."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Never

from agentworks.errors import PartialMutationError

from ._file_local_custody import LocalFileCall, local_failure, owned_local_call
from ._file_result import _details, _diagnostic, _raise_reason
from ._local_file_io import (
    LocalDestination,
    LocalTreeInventory,
    validate_local_tree_component,
    validate_local_tree_destination,
)
from .files import (
    Change,
    Create,
    DirectoryLimit,
    DirectoryTransferControlFact,
    DirectoryTransferEntry,
    DirectoryTransferLimit,
    DirectoryTransferProgress,
    ExistingFile,
    FileFailureReason,
    FileKind,
    FileOperationPhase,
    Replace,
    Revision,
    _file_revision_from_revision,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ._local_file_io import LocalTreeEntry
    from .access import FileAccess
    from .carrier import Deadline
    from .files import FileMetadata, NewMetadata


def _fail(access: FileAccess, reason: FileFailureReason, *, changed: bool = False) -> Never:
    if changed:
        phase = FileOperationPhase.PUBLICATION
        message, hint = _diagnostic(phase, reason, effect=Change.CHANGED)
        raise PartialMutationError(
            message,
            completed_steps=(),
            entity_kind=access._entity_kind,
            entity_name=access._entity_name,
            hint=hint,
            details=_details(phase, reason, effect=Change.CHANGED),
        ) from None
    _raise_reason(
        FileOperationPhase.INVENTORY, reason, entity_kind=access._entity_kind, entity_name=access._entity_name
    )


def _live(access: FileAccess, deadline: Deadline, *, changed: bool = False) -> None:
    if deadline.expired:
        _fail(access, FileFailureReason.DEADLINE, changed=changed)


def _local[T](access: FileAccess, deadline: Deadline, action: Callable[[LocalFileCall], T]) -> T:
    return owned_local_call(
        access._operation, deadline, action, entity_kind=access._entity_kind, entity_name=access._entity_name
    )


def _remote_key(revision: Revision) -> tuple[str, int, int | bytes]:
    observed = _file_revision_from_revision(revision).stat
    return ("remote", observed.device, observed.inode)


def _assign(
    access: FileAccess,
    seen: dict[tuple[str, int, int | bytes], PurePosixPath],
    key: tuple[str, int, int | bytes],
    relative: PurePosixPath,
    *,
    changed: bool = False,
) -> None:
    previous = seen.get(key)
    if previous is not None and previous != relative:
        _fail(access, FileFailureReason.CONFLICT, changed=changed)
    for assigned, path in tuple(seen.items()):
        if path == relative and assigned != key:
            del seen[assigned]
    seen[key] = relative


def _attach(
    error: BaseException,
    entries: list[DirectoryTransferEntry],
    relative: PurePosixPath,
    *,
    preserve_preflight: bool = False,
) -> Never:
    cause = error.__cause__
    if (
        preserve_preflight
        and not entries
        and relative == PurePosixPath(".")
        and type(cause) is DirectoryTransferControlFact
        and not cause.progress.entries
    ):
        # A source inventory already identified its attempted directory before
        # destination children began. Preserve that more precise location.
        raise error from cause
    try:
        progress = DirectoryTransferProgress(tuple(entries), sum(entry.transferred_bytes for entry in entries))
        fact = DirectoryTransferControlFact(progress, relative)
        fact.__cause__ = error.__cause__
    except BaseException:
        raise error from error.__cause__
    raise error from fact


def _local_destination(
    access: FileAccess,
    destination: Path,
    deadline: Deadline,
    relative: PurePosixPath,
    seen: dict[tuple[str, int, int | bytes], PurePosixPath],
    *,
    directory: bool,
    changed: bool = False,
) -> tuple[LocalTreeEntry | None, Change]:
    def observe(call: LocalFileCall) -> tuple[LocalTreeEntry | None, Change]:
        call.changed = changed
        local = LocalDestination(destination)
        call.resource = local
        entry = local.observe(deadline)
        if entry is not None:
            _assign(access, seen, entry.identity.object_key, relative, changed=changed)
        effect = Change.UNCHANGED
        if directory:
            call.phase = FileOperationPhase.CREATION
            effect = local.ensure_directory(deadline)
        identity = local.identity
        if directory:
            if identity is None:
                _fail(access, FileFailureReason.INVALID_RESPONSE, changed=local.changed)
            _assign(access, seen, identity.object_key, relative, changed=local.changed)
        return entry, effect

    return _local(access, deadline, observe)


def upload_directory(
    access: FileAccess,
    path: PurePosixPath,
    source: Path,
    *,
    deadline: Deadline,
    limit: DirectoryTransferLimit,
    existing_files: ExistingFile,
    file_metadata: NewMetadata,
    directory_metadata: NewMetadata,
    sudo: bool,
) -> DirectoryTransferProgress:
    entries: list[DirectoryTransferEntry] = []
    relative = PurePosixPath(".")
    seen: dict[tuple[str, int, int | bytes], PurePosixPath] = {}
    transferred = 0
    try:
        _live(access, deadline)

        def inventory(call: LocalFileCall) -> tuple[LocalTreeEntry, ...]:
            call.phase = FileOperationPhase.INVENTORY
            resource = LocalTreeInventory(source, max_entries=limit.max_entries, max_depth=limit.max_depth)
            call.resource = resource
            return resource.inventory(deadline)

        manifest = _local(access, deadline, inventory)
        for member in manifest:
            relative = member.relative_path
            _live(access, deadline)
            target = path / relative
            observed = access.stat(target, sudo=sudo)
            if observed is not None:
                _assign(access, seen, _remote_key(observed.revision), relative)
            size = 0
            if member.kind is FileKind.DIRECTORY:
                if observed is None:
                    result = access.create_directory(target, metadata=directory_metadata, sudo=sudo)
                    if result.revision is None:
                        _fail(access, FileFailureReason.INVALID_RESPONSE, changed=True)
                    _assign(access, seen, _remote_key(result.revision), relative, changed=True)
                    change = result.change
                elif observed.kind is FileKind.DIRECTORY:
                    change = Change.UNCHANGED
                else:
                    _fail(access, FileFailureReason.CONFLICT)
            else:
                if observed is not None and observed.kind is not FileKind.REGULAR:
                    _fail(access, FileFailureReason.CONFLICT)
                if observed is not None and existing_files is ExistingFile.ERROR:
                    _fail(access, FileFailureReason.CONFLICT)
                if observed is not None and existing_files is ExistingFile.PRESERVE:
                    change = Change.UNCHANGED
                else:
                    size = member.size
                    if size > limit.max_bytes - transferred:
                        _fail(access, FileFailureReason.LIMIT)
                    result = access._upload_file(
                        target,
                        source.joinpath(*relative.parts),
                        condition=Create() if observed is None else Replace(),
                        create_metadata=file_metadata,
                        sudo=sudo,
                        expected=member.identity,
                        max_bytes=limit.max_bytes - transferred,
                    )
                    if result.revision is None:
                        _fail(access, FileFailureReason.INVALID_RESPONSE, changed=True)
                    _assign(access, seen, _remote_key(result.revision), relative, changed=True)
                    change = result.change
            _live(access, deadline, changed=change is Change.CHANGED)
            entries.append(DirectoryTransferEntry(relative, member.kind, change, size))
            transferred += size
    except BaseException as error:
        _attach(error, entries, relative)
    return DirectoryTransferProgress(tuple(entries), transferred)


def _remote_manifest(
    access: FileAccess, path: PurePosixPath, limit: DirectoryTransferLimit, deadline: Deadline, sudo: bool
) -> tuple[tuple[PurePosixPath, FileMetadata], ...]:
    root = access.stat(path, sudo=sudo)
    if root is None or root.kind is not FileKind.DIRECTORY:
        _fail(access, FileFailureReason.CONFLICT)
    manifest = [(PurePosixPath("."), root)]
    pending = [PurePosixPath(".")]
    source_device = _file_revision_from_revision(root.revision).stat.device
    while pending:
        parent = pending.pop()
        try:
            _live(access, deadline)
            children = access.list_directory(path / parent, limit=DirectoryLimit(4_096, 1, 4 * 1_048_576), sudo=sudo)
            for child in sorted(children, key=lambda entry: entry.relative_path.parts):
                relative = parent / child.relative_path
                if len(child.relative_path.parts) != 1:
                    _fail(access, FileFailureReason.INVALID_RESPONSE)
                validate_local_tree_component(child.relative_path.name)
                if (
                    len(manifest) >= limit.max_entries
                    or len(relative.parts) > limit.max_depth
                    or len(str(relative).encode("utf-8")) > 4_096
                ):
                    _fail(access, FileFailureReason.LIMIT)
                metadata = child.metadata
                observed = _file_revision_from_revision(metadata.revision).stat
                if (
                    metadata.kind not in (FileKind.REGULAR, FileKind.DIRECTORY)
                    or observed.device != source_device
                    or (metadata.kind is FileKind.REGULAR and observed.link_count != 1)
                ):
                    _fail(access, FileFailureReason.UNSUPPORTED)
                manifest.append((relative, metadata))
                if metadata.kind is FileKind.DIRECTORY:
                    pending.append(relative)
        except OSError as error:
            try:
                local_failure(error, entity_kind=access._entity_kind, entity_name=access._entity_name)
            except BaseException as reduced:
                _attach(reduced, [], parent)
        except BaseException as error:
            _attach(error, [], parent)
    return tuple(sorted(manifest, key=lambda member: member[0].parts))


def download_directory(
    access: FileAccess,
    path: PurePosixPath,
    destination: Path,
    *,
    deadline: Deadline,
    limit: DirectoryTransferLimit,
    existing_files: ExistingFile,
    sudo: bool,
) -> DirectoryTransferProgress:
    entries: list[DirectoryTransferEntry] = []
    relative = PurePosixPath(".")
    seen: dict[tuple[str, int, int | bytes], PurePosixPath] = {}
    transferred = 0
    try:
        _live(access, deadline)
        validate_local_tree_destination(destination)
        manifest = _remote_manifest(access, path, limit, deadline, sudo)
        for relative, _ in manifest:
            _live(access, deadline)
            validate_local_tree_destination(destination.joinpath(*relative.parts))
        for relative, source in manifest:
            _live(access, deadline)
            local = destination.joinpath(*relative.parts)
            observed, change = _local_destination(
                access, local, deadline, relative, seen, directory=source.kind is FileKind.DIRECTORY
            )
            size = 0
            if source.kind is FileKind.REGULAR:
                if observed is not None and observed.kind is not FileKind.REGULAR:
                    _fail(access, FileFailureReason.CONFLICT)
                if observed is not None and existing_files is ExistingFile.ERROR:
                    _fail(access, FileFailureReason.CONFLICT)
                if observed is not None and existing_files is ExistingFile.PRESERVE:
                    change = Change.UNCHANGED
                else:
                    result = access.download(
                        path / relative,
                        local,
                        local_condition=Create() if observed is None else Replace(),
                        max_bytes=limit.max_bytes - transferred,
                        sudo=sudo,
                    )
                    size = result.size
                    published, _ = _local_destination(
                        access, local, deadline, relative, seen, directory=False, changed=True
                    )
                    if published is None or published.kind is not FileKind.REGULAR:
                        _fail(access, FileFailureReason.INVALID_RESPONSE, changed=True)
                    change = Change.CHANGED
            _live(access, deadline, changed=change is Change.CHANGED)
            entries.append(DirectoryTransferEntry(relative, source.kind, change, size))
            transferred += size
    except OSError as error:
        try:
            local_failure(error, entity_kind=access._entity_kind, entity_name=access._entity_name)
        except BaseException as reduced:
            _attach(reduced, entries, relative, preserve_preflight=True)
    except BaseException as error:
        _attach(error, entries, relative, preserve_preflight=True)
    return DirectoryTransferProgress(tuple(entries), transferred)
