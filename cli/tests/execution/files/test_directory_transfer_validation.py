"""Creation and destination identity validation before tree mutations."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from agentworks.errors import ConflictError, StateError, ValidationError
from agentworks.execution import _directory_transfer
from agentworks.execution import access as access_module
from agentworks.execution._local_file_io import LocalDestination
from agentworks.execution._local_file_io_types import _validate_windows_path
from agentworks.execution.files import (
    Create,
    DirectoryTransferControlFact,
    DirectoryTransferLimit,
    ExistingFile,
    FileFailureReason,
    directory_transfer_progress,
)
from agentworks.operations import OperationOwner
from tests.execution.files._file_access_support import bound_access as bound_access
from tests.execution.files._file_access_support import plan as plan
from tests.execution.files.test_directory_transfer import metadata

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real fixed helper proof requires Linux")


def test_destination_assignment_retires_only_replaced_paths_old_identity(bound_access):
    access, _, _, _ = bound_access
    old = ("fixture", 1, 1)
    current = ("fixture", 1, 2)
    seen = {old: PurePosixPath("a")}
    _directory_transfer._assign(access, seen, current, PurePosixPath("a"))
    assert seen == {current: PurePosixPath("a")}
    _directory_transfer._assign(access, seen, old, PurePosixPath("b"))
    assert seen == {current: PurePosixPath("a"), old: PurePosixPath("b")}
    with pytest.raises(ConflictError):
        _directory_transfer._assign(access, seen, current, PurePosixPath("b"))
    assert seen == {current: PurePosixPath("a"), old: PurePosixPath("b")}


def test_ordinary_upload_replace_then_create_accepts_recycled_old_identity(bound_access, tmp_path, monkeypatch):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"new-a")
    (source / "b").write_bytes(b"new-b")
    (root / "a").write_bytes(b"old-a")
    identities = iter((("fixture", 1, 0), ("fixture", 1, 1), ("fixture", 1, 2), ("fixture", 1, 1)))
    observed = []

    def identity(revision):
        observed.append(revision)
        return next(identities)

    monkeypatch.setattr(_directory_transfer, "_remote_key", identity)
    progress = access.upload_directory(
        PurePosixPath(root),
        source,
        limit=DirectoryTransferLimit(10),
        existing_files=ExistingFile.REPLACE,
        file_metadata=metadata(),
        directory_metadata=metadata(),
    )
    assert len(observed) == 4
    assert len(progress.entries) == 3 and progress.transferred_bytes == 10
    assert (root / "a").read_bytes() == b"new-a" and (root / "b").read_bytes() == b"new-b"
    assert not owner.list_pending_lifecycle_obligations()


def test_ordinary_download_replace_then_create_accepts_recycled_old_identity(bound_access, tmp_path, monkeypatch):
    access, root, owner, _ = bound_access
    (root / "a").write_bytes(b"new-a")
    (root / "b").write_bytes(b"new-b")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "a").write_bytes(b"old-a")
    admitted_a = []

    class RecycledIdentity(LocalDestination):
        def observe(self, deadline):
            entry = super().observe(deadline)
            if entry is not None and self._path.name in ("a", "b"):
                if self._path.name == "a":
                    admitted_a.append(entry.identity)
                    inode = len(admitted_a)
                else:
                    inode = 1
                return replace(entry, identity=replace(entry.identity, platform="fixture", device=1, inode=inode))
            return entry

    monkeypatch.setattr(_directory_transfer, "LocalDestination", RecycledIdentity)
    progress = access.download_directory(
        PurePosixPath(root), destination, limit=DirectoryTransferLimit(10), existing_files=ExistingFile.REPLACE
    )
    assert len(admitted_a) == 2
    assert len(progress.entries) == 3 and progress.transferred_bytes == 10
    assert (destination / "a").read_bytes() == b"new-a" and (destination / "b").read_bytes() == b"new-b"
    assert not owner.list_pending_lifecycle_obligations()


@pytest.mark.parametrize("spelling", ["max-path", "reserved", "stream", "trailing"])
def test_all_composed_windows_paths_are_validated_before_local_creation(bound_access, tmp_path, monkeypatch, spelling):
    access, root, owner, _ = bound_access
    (root / "nested").mkdir()
    name = {"max-path": "f" * 20, "reserved": "CON", "stream": "file:stream", "trailing": "file."}[spelling]
    (root / "nested" / name).write_bytes(b"unchanged")
    destination = tmp_path / "destination"
    windows_root = PureWindowsPath("C:\\" + ("r" * 240 if spelling == "max-path" else "root"))
    validated = []

    def validate(path):
        relative = path.relative_to(destination)
        validated.append(relative)
        _validate_windows_path(windows_root.joinpath(*relative.parts))

    def unexpected(*args, **kwargs):
        pytest.fail("invalid destination path admitted local creation or content transfer")

    monkeypatch.setattr(_directory_transfer, "validate_local_tree_destination", validate)
    monkeypatch.setattr(_directory_transfer, "LocalDestination", unexpected)
    monkeypatch.setattr(type(access), "download", unexpected)
    with pytest.raises(StateError) as stopped:
        access.download_directory(
            PurePosixPath(root), destination, limit=DirectoryTransferLimit(100), existing_files=ExistingFile.ERROR
        )
    assert stopped.value.details is not None and stopped.value.details.reason is FileFailureReason.UNSUPPORTED
    fact = stopped.value.__cause__
    assert isinstance(fact, DirectoryTransferControlFact)
    assert fact.relative_path == PurePosixPath("nested") / name
    progress = directory_transfer_progress(stopped.value)
    assert progress is not None and progress.entries == () and progress.transferred_bytes == 0
    assert validated[-1] == PurePosixPath("nested") / name
    assert not destination.exists()
    assert not access._operation.has_unfinished_local_file_call
    assert not owner.list_pending_lifecycle_obligations()
    assert (root / "nested" / name).read_bytes() == b"unchanged"


@pytest.mark.parametrize("operation", ["upload-file", "upload", "tree-file", "tree-directory", "create", "ensure"])
def test_known_invalid_metadata_is_rejected_before_any_borrow_or_local_io(
    bound_access, tmp_path, monkeypatch, operation
):
    access, root, _, _ = bound_access
    invalid_file = replace(metadata(), mode=0o1700)
    invalid_directory = replace(metadata(), mode=0o4700)
    target = PurePosixPath(root / "new")

    def unexpected(*args, **kwargs):
        pytest.fail("invalid creation metadata admitted a borrow or local I/O")

    monkeypatch.setattr(OperationOwner, "borrow", unexpected)
    monkeypatch.setattr(access_module, "LocalFileSource", unexpected)
    monkeypatch.setattr(_directory_transfer, "LocalTreeInventory", unexpected)
    with pytest.raises(ValidationError):
        if operation == "upload-file":
            access.upload_file(target, tmp_path / "source", condition=Create(), create_metadata=invalid_file)
        elif operation == "upload":

            class Source:
                read = unexpected

            access.upload(target, Source(), size=0, condition=Create(), create_metadata=invalid_file)
        elif operation in ("tree-file", "tree-directory"):
            access.upload_directory(
                target,
                tmp_path / "source",
                limit=DirectoryTransferLimit(0),
                existing_files=ExistingFile.ERROR,
                file_metadata=invalid_file if operation == "tree-file" else metadata(),
                directory_metadata=invalid_directory if operation == "tree-directory" else metadata(),
            )
        elif operation == "create":
            access.create_directory(target, metadata=invalid_directory)
        else:
            access.ensure_directory(target, metadata=invalid_directory)
    assert access._carrier.calls == 0
    assert not (root / "new").exists()


def test_supported_file_and_directory_modes_remain_admitted(bound_access, tmp_path):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"x")
    file_metadata = replace(metadata(), mode=0o777)
    directory_metadata = replace(metadata(), mode=0o1700)
    access.upload_file(
        PurePosixPath(root / "standalone"), source / "file", condition=Create(), create_metadata=file_metadata
    )
    access.create_directory(PurePosixPath(root / "single"), metadata=directory_metadata)
    access.ensure_directory(PurePosixPath(root / "single"), metadata=directory_metadata)
    result = access.upload_directory(
        PurePosixPath(root / "tree"),
        source,
        limit=DirectoryTransferLimit(1),
        existing_files=ExistingFile.ERROR,
        file_metadata=file_metadata,
        directory_metadata=directory_metadata,
    )
    assert result.transferred_bytes == 1
    assert (root / "standalone").stat().st_mode & 0o7777 == 0o777
    assert (root / "single").stat().st_mode & 0o7777 == 0o1700
    assert (root / "tree").stat().st_mode & 0o7777 == 0o1700
    assert (root / "tree" / "file").stat().st_mode & 0o7777 == 0o777
    assert not owner.list_pending_lifecycle_obligations()
