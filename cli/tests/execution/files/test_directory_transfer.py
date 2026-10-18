"""Real ordinary child calls composing bounded local and remote trees."""

from __future__ import annotations

import os
import sys
from pathlib import PurePosixPath

import pytest

from agentworks.errors import AgentworksError, ConflictError, LimitExceededError, PartialMutationError, ValidationError
from agentworks.execution import _directory_transfer
from agentworks.execution._local_file_io import LocalDestination
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import (
    Change,
    Create,
    DirectoryTransferControlFact,
    DirectoryTransferEntry,
    DirectoryTransferLimit,
    DirectoryTransferProgress,
    ExistingFile,
    FileKind,
    NewMetadata,
    directory_transfer_progress,
)
from tests.execution.files._file_access_support import bound_access as bound_access
from tests.execution.files._file_access_support import plan as plan
from tests.execution.files.test_file_access import RecordingCarrier

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real fixed helper proof requires Linux")


def metadata() -> NewMetadata:
    import grp
    import pwd

    return NewMetadata(pwd.getpwuid(os.geteuid()).pw_name, grp.getgrgid(os.getegid()).gr_name, 0o700)


@pytest.mark.parametrize("bound", [-1, True, 1 << 63])
def test_transfer_byte_limit_boundary(bound):
    with pytest.raises(ValidationError):
        DirectoryTransferLimit(bound)


def test_progress_enforces_root_order_totals_and_kind():
    root = DirectoryTransferEntry(PurePosixPath("."), FileKind.DIRECTORY, Change.UNCHANGED, 0)
    child = DirectoryTransferEntry(PurePosixPath("child"), FileKind.REGULAR, Change.CHANGED, 3)
    progress = DirectoryTransferProgress((root, child), 3)
    assert progress.transferred_bytes == 3
    with pytest.raises(ValidationError):
        DirectoryTransferProgress((child,), 3)
    with pytest.raises(ValidationError):
        DirectoryTransferProgress((root, child), 4)
    with pytest.raises(ValidationError):
        DirectoryTransferEntry(PurePosixPath("."), FileKind.REGULAR, Change.CHANGED, 1)


def test_upload_path_and_create_only_directory(bound_access, tmp_path):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.write_bytes(b"source data")
    result = access.upload_file(PurePosixPath(root / "file"), source, condition=Create(), create_metadata=metadata())
    assert result.change is Change.CHANGED
    assert (root / "file").read_bytes() == b"source data"
    directory = root / "directory"
    access.create_directory(PurePosixPath(directory), metadata=metadata())
    directory.chmod(0o755)
    with pytest.raises(ConflictError):
        access.create_directory(PurePosixPath(directory), metadata=metadata())
    assert directory.stat().st_mode & 0o777 == 0o755
    assert not access._operation.has_unfinished_local_file_call
    assert not owner.list_pending_lifecycle_obligations()


def test_upload_tree_preserves_existing_directory_metadata_and_zero_budget(bound_access, tmp_path):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty").mkdir()
    (source / "a").write_bytes(b"ignored source")
    (source / "empty-file").touch()
    (root / "a").write_bytes(b"destination")
    root.chmod(0o755)
    before = root.stat()
    result = access.upload_directory(
        PurePosixPath(root),
        source,
        limit=DirectoryTransferLimit(0),
        existing_files=ExistingFile.PRESERVE,
        file_metadata=metadata(),
        directory_metadata=metadata(),
    )
    assert result.transferred_bytes == 0
    assert [str(entry.relative_path) for entry in result.entries] == [".", "a", "empty", "empty-file"]
    assert (root / "a").read_bytes() == b"destination"
    assert root.stat().st_mode == before.st_mode
    assert root.stat().st_uid == before.st_uid
    assert not owner.list_pending_lifecycle_obligations()


def test_upload_stops_at_first_conflict_and_keeps_prior_progress(bound_access, tmp_path):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    for name in ("a", "b", "c"):
        (source / name).write_bytes(name.encode())
    (root / "b").write_bytes(b"existing")
    with pytest.raises(ConflictError) as stopped:
        access.upload_directory(
            PurePosixPath(root),
            source,
            limit=DirectoryTransferLimit(100),
            existing_files=ExistingFile.ERROR,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
    progress = directory_transfer_progress(stopped.value)
    assert progress is not None
    assert [str(entry.relative_path) for entry in progress.entries] == [".", "a"]
    assert progress.transferred_bytes == 1
    assert isinstance(stopped.value.__cause__, DirectoryTransferControlFact)
    assert stopped.value.__cause__.relative_path == PurePosixPath("b")
    assert not (root / "c").exists()


@pytest.mark.parametrize("kind", ["link", "hardlink", "depth", "entries"])
def test_upload_manifest_refusal_precedes_destination_mutation(bound_access, tmp_path, kind):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"a")
    if kind == "link":
        (source / "b").symlink_to(source / "a")
    elif kind == "hardlink":
        os.link(source / "a", source / "b")
    elif kind == "depth":
        (source / "b").mkdir()
        (source / "b" / "c").touch()
    limit = DirectoryTransferLimit(100, max_entries=1 if kind == "entries" else 100, max_depth=1)
    with pytest.raises(AgentworksError) as stopped:
        access.upload_directory(
            PurePosixPath(root / "new"),
            source,
            limit=limit,
            existing_files=ExistingFile.REPLACE,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
    assert not (root / "new").exists()
    progress = directory_transfer_progress(stopped.value)
    assert progress is not None and progress.entries == ()


def test_download_tree_and_preserve_with_zero_budget(bound_access, tmp_path):
    access, root, owner, _ = bound_access
    (root / "directory").mkdir()
    (root / "directory" / "caf\u00e9").write_bytes(b"payload")
    (root / "empty").touch()
    target = tmp_path / "download"
    result = access.download_directory(
        PurePosixPath(root), target, limit=DirectoryTransferLimit(7), existing_files=ExistingFile.ERROR
    )
    assert result.transferred_bytes == 7
    assert (target / "directory" / "caf\u00e9").read_bytes() == b"payload"
    assert target.stat().st_mode & 0o777 == 0o700
    preserved = access.download_directory(
        PurePosixPath(root), target, limit=DirectoryTransferLimit(0), existing_files=ExistingFile.PRESERVE
    )
    assert preserved.transferred_bytes == 0
    assert all(entry.change is Change.UNCHANGED for entry in preserved.entries)
    assert not owner.list_pending_lifecycle_obligations()


def test_download_visits_depth_boundary_before_mutation(bound_access, tmp_path):
    access, root, _, _ = bound_access
    (root / "dir").mkdir()
    (root / "dir" / "child").touch()
    target = tmp_path / "download"
    with pytest.raises(LimitExceededError):
        access.download_directory(
            PurePosixPath(root),
            target,
            limit=DirectoryTransferLimit(100, max_depth=1),
            existing_files=ExistingFile.ERROR,
        )
    assert not target.exists()


def test_more_than_128_children_use_one_deadline_without_history_exhaustion(bound_access, tmp_path):
    access, root, owner, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    for index in range(130):
        (source / f"{index:03d}").touch()
    selected = Deadline.after(180)
    calls = []

    def select():
        calls.append(selected)
        return selected

    access._deadline = select
    carrier = RecordingCarrier()
    access._carrier = carrier
    progress = access.upload_directory(
        PurePosixPath(root),
        source,
        limit=DirectoryTransferLimit(0),
        existing_files=ExistingFile.ERROR,
        file_metadata=metadata(),
        directory_metadata=metadata(),
    )
    assert len(progress.entries) == 131
    assert calls == [selected]
    assert carrier.deadlines and all(deadline is selected for deadline in carrier.deadlines)
    assert not owner.list_pending_lifecycle_obligations()


def test_download_identity_failure_after_publication_keeps_changed_current_child(bound_access, tmp_path, monkeypatch):
    access, root, _, _ = bound_access
    (root / "a").write_bytes(b"published")
    target = tmp_path / "destination"
    seen = []

    class FailedPublicationObservation(LocalDestination):
        def observe(self, deadline):
            if self._path.name == "a":
                seen.append(True)
                if len(seen) == 2:
                    raise OSError("private post-publication probe failed")
            return super().observe(deadline)

    monkeypatch.setattr(_directory_transfer, "LocalDestination", FailedPublicationObservation)
    with pytest.raises(PartialMutationError) as stopped:
        access.download_directory(
            PurePosixPath(root), target, limit=DirectoryTransferLimit(9), existing_files=ExistingFile.ERROR
        )
    assert (target / "a").read_bytes() == b"published"
    assert stopped.value.details is not None and stopped.value.details.effect is Change.CHANGED
    progress = directory_transfer_progress(stopped.value)
    assert progress is not None
    assert [str(entry.relative_path) for entry in progress.entries] == ["."]
    assert progress.transferred_bytes == 0


def test_upload_destination_alias_refused_before_second_preservation(bound_access, tmp_path, monkeypatch):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    for name in ("A", "a"):
        (source / name).touch()
        (root / name).write_bytes(b"existing")
    ordinary_stat = type(access).stat
    observations = []

    def aliased_stat(self, path, *, sudo=False):
        result = ordinary_stat(self, path, sudo=sudo)
        if path.name == "A":
            observations.append(result)
        return observations[0] if path.name == "a" else result

    monkeypatch.setattr(type(access), "stat", aliased_stat)
    with pytest.raises(ConflictError) as stopped:
        access.upload_directory(
            PurePosixPath(root),
            source,
            limit=DirectoryTransferLimit(0),
            existing_files=ExistingFile.PRESERVE,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
    progress = directory_transfer_progress(stopped.value)
    assert progress is not None
    assert [str(entry.relative_path) for entry in progress.entries] == [".", "A"]


def test_expired_directory_budget_stops_before_inventory(bound_access, tmp_path, monkeypatch):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    access._deadline = lambda: Deadline.after(0)

    def unexpected(*args, **kwargs):
        pytest.fail("expired invocation attempted inventory")

    monkeypatch.setattr(_directory_transfer, "LocalTreeInventory", unexpected)
    with pytest.raises(AgentworksError) as stopped:
        access.upload_directory(
            PurePosixPath(root),
            source,
            limit=DirectoryTransferLimit(0),
            existing_files=ExistingFile.ERROR,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
    progress = directory_transfer_progress(stopped.value)
    assert progress is not None and progress.entries == ()


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_empty_tree_accepts_root_only_depth_zero_and_zero_bytes(bound_access, tmp_path, direction):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    if direction == "upload":
        progress = access.upload_directory(
            PurePosixPath(root / "new"),
            source,
            limit=DirectoryTransferLimit(0, max_entries=1, max_depth=0),
            existing_files=ExistingFile.ERROR,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
        assert (root / "new").is_dir()
    else:
        progress = access.download_directory(
            PurePosixPath(root),
            tmp_path / "new",
            limit=DirectoryTransferLimit(0, max_entries=1, max_depth=0),
            existing_files=ExistingFile.ERROR,
        )
        assert (tmp_path / "new").is_dir()
    assert len(progress.entries) == 1
    assert progress.entries[0].relative_path == PurePosixPath(".")
    assert progress.transferred_bytes == 0


def test_replacement_merges_without_pruning_and_byte_limit_excludes_failed_child(bound_access, tmp_path):
    access, root, _, _ = bound_access
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"new")
    (source / "b").write_bytes(b"too large")
    (root / "a").write_bytes(b"old")
    (root / "extra").write_bytes(b"unmentioned")
    (root / "a").chmod(0o640)
    with pytest.raises(LimitExceededError) as stopped:
        access.upload_directory(
            PurePosixPath(root),
            source,
            limit=DirectoryTransferLimit(3),
            existing_files=ExistingFile.REPLACE,
            file_metadata=metadata(),
            directory_metadata=metadata(),
        )
    assert (root / "a").read_bytes() == b"new"
    assert (root / "a").stat().st_mode & 0o777 == 0o640
    assert (root / "extra").read_bytes() == b"unmentioned"
    assert not (root / "b").exists()
    progress = directory_transfer_progress(stopped.value)
    assert progress is not None and progress.transferred_bytes == 3
    assert [str(entry.relative_path) for entry in progress.entries] == [".", "a"]


@pytest.mark.parametrize("control", [False, True])
def test_nested_inventory_failure_keeps_attempted_parent_and_original_cause(
    bound_access, tmp_path, monkeypatch, control
):
    access, root, owner, _ = bound_access
    nested = root / "outer" / "inner"
    nested.mkdir(parents=True)
    (nested / "file").write_bytes(b"untransferred")
    destination = tmp_path / "destination"
    cause = RuntimeError("original cause")
    original = KeyboardInterrupt() if control else ConflictError("source inventory failed")
    original.__cause__ = cause
    ordinary_inventory = type(access).list_directory
    attempted = []

    def inventory(self, path, *, limit, sudo=False):
        attempted.append(path)
        if path == PurePosixPath(nested):
            raise original
        return ordinary_inventory(self, path, limit=limit, sudo=sudo)

    monkeypatch.setattr(type(access), "list_directory", inventory)
    with pytest.raises(KeyboardInterrupt if control else ConflictError) as stopped:
        access.download_directory(
            PurePosixPath(root), destination, limit=DirectoryTransferLimit(100), existing_files=ExistingFile.ERROR
        )
    assert stopped.value is original
    fact = stopped.value.__cause__
    assert isinstance(fact, DirectoryTransferControlFact)
    assert fact.relative_path == PurePosixPath("outer/inner")
    assert fact.__cause__ is cause
    assert fact.progress.entries == () and fact.progress.transferred_bytes == 0
    assert directory_transfer_progress(stopped.value) is fact.progress
    assert attempted == [PurePosixPath(root), PurePosixPath(root / "outer"), PurePosixPath(nested)]
    assert not destination.exists()
    assert not owner.list_pending_lifecycle_obligations()


def test_nested_inventory_local_oserror_is_reduced_before_location_attachment(bound_access, tmp_path, monkeypatch):
    access, root, _, _ = bound_access
    (root / "nested").mkdir()
    destination = tmp_path / "destination"
    ordinary_inventory = type(access).list_directory

    def inventory(self, path, *, limit, sudo=False):
        if path == PurePosixPath(root / "nested"):
            raise FileExistsError("private-path-and-filesystem-text")
        return ordinary_inventory(self, path, limit=limit, sudo=sudo)

    monkeypatch.setattr(type(access), "list_directory", inventory)
    with pytest.raises(ConflictError) as stopped:
        access.download_directory(
            PurePosixPath(root), destination, limit=DirectoryTransferLimit(0), existing_files=ExistingFile.ERROR
        )
    fact = stopped.value.__cause__
    assert isinstance(fact, DirectoryTransferControlFact)
    assert fact.relative_path == PurePosixPath("nested")
    assert fact.progress.entries == ()
    assert fact.__cause__ is None
    assert "private-path-and-filesystem-text" not in str(stopped.value)
    assert not destination.exists()


@pytest.mark.parametrize("earlier_completed", [False, True])
def test_preflight_does_not_adopt_progress_or_location_from_an_earlier_exception_cause(
    bound_access, tmp_path, monkeypatch, earlier_completed
):
    access, root, _, _ = bound_access
    earlier = DirectoryTransferControlFact(
        DirectoryTransferProgress(
            (DirectoryTransferEntry(PurePosixPath("."), FileKind.DIRECTORY, Change.CHANGED, 0),)
            if earlier_completed
            else (),
            0,
        ),
        PurePosixPath("earlier"),
    )
    original = ConflictError("source inventory failed")
    original.__cause__ = earlier

    def inventory(*args, **kwargs):
        raise original

    monkeypatch.setattr(type(access), "list_directory", inventory)
    destination = tmp_path / "destination"
    with pytest.raises(ConflictError) as stopped:
        access.download_directory(
            PurePosixPath(root), destination, limit=DirectoryTransferLimit(0), existing_files=ExistingFile.ERROR
        )
    assert stopped.value is original
    fact = stopped.value.__cause__
    assert isinstance(fact, DirectoryTransferControlFact)
    assert fact.relative_path == PurePosixPath(".") and fact.progress.entries == ()
    assert fact.__cause__ is earlier
    assert not destination.exists()
