"""Public file-value boundaries and private revision conversion."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution.files import (
    Change,
    Create,
    DirectoryEntry,
    DirectoryLimit,
    FileKind,
    FileMetadata,
    Match,
    MutationResult,
    NewMetadata,
    ReadResult,
    Replace,
    Revision,
    _file_revision_from_revision,
    _revision_from_file_revision,
)


def _private_revision(
    kind: FileKind = FileKind.REGULAR,
    *,
    size: int = 7,
    digest: bytes | None = None,
) -> FileRevision:
    mode = {
        FileKind.REGULAR: stat.S_IFREG | 0o6754,
        FileKind.DIRECTORY: stat.S_IFDIR | 0o3750,
        FileKind.SOCKET: stat.S_IFSOCK | 0o770,
    }[kind]
    links = 1 if kind is not FileKind.DIRECTORY else 2
    return FileRevision(FileStat(11, 29, mode, links, 1001, 1002, size, 30, 31), digest)


def _metadata(revision: FileRevision) -> FileMetadata:
    observed = revision.stat
    kind = (
        FileKind.REGULAR
        if stat.S_ISREG(observed.mode)
        else FileKind.DIRECTORY
        if stat.S_ISDIR(observed.mode)
        else FileKind.SOCKET
    )
    return FileMetadata(
        kind,
        observed.size,
        stat.S_IMODE(observed.mode),
        observed.uid,
        observed.gid,
        observed.modified_ns,
        _revision_from_file_revision(revision),
    )


@pytest.mark.parametrize(
    "private",
    [
        _private_revision(digest=b"d" * 32),
        _private_revision(),
        _private_revision(FileKind.DIRECTORY),
        _private_revision(FileKind.SOCKET),
    ],
)
def test_revision_round_trip_preserves_kind_digest_and_raw_mode(private: FileRevision) -> None:
    public = _revision_from_file_revision(private)
    assert _file_revision_from_revision(public) == private
    assert _file_revision_from_revision(public).stat.mode == private.stat.mode
    assert public.token.decode() not in repr(public)


def test_revision_refuses_noncanonical_or_malformed_tokens() -> None:
    valid = _revision_from_file_revision(_private_revision()).token
    fields = json.loads(valid)
    reordered = json.dumps(dict(reversed(tuple(fields.items()))), separators=(",", ":")).encode("ascii")
    malformed = [
        b"not-json",
        b" " + valid,
        valid + b" ",
        reordered,
        valid.replace(b'"version":1', b'"version":2'),
        valid.replace(b"{", b'{"extra":0,', 1),
        valid.replace(b'{"changed_ns":', b'{"changed_ns":0,"changed_ns":', 1),
        b"x" * 4_097,
    ]
    for token in malformed:
        with pytest.raises(ValidationError):
            Revision(token)


def test_revision_reuses_private_unsafe_fact_validation() -> None:
    regular = json.loads(_revision_from_file_revision(_private_revision()).token)
    directory = json.loads(_revision_from_file_revision(_private_revision(FileKind.DIRECTORY)).token)
    regular["mode"] = stat.S_IFLNK | 0o777
    directory["digest"] = "00" * 32

    for value in (regular, directory):
        token = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
        with pytest.raises(ValidationError):
            Revision(token)


def test_revision_requires_exact_bytes() -> None:
    class TokenBytes(bytes):
        pass

    token = _revision_from_file_revision(_private_revision()).token
    for value in (bytearray(token), TokenBytes(token)):
        with pytest.raises(ValidationError):
            Revision(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", list(FileKind))
def test_metadata_projects_exact_revision_evidence(kind: FileKind) -> None:
    private = _private_revision(kind)
    metadata = _metadata(private)
    assert metadata.kind is kind
    assert metadata.mode == stat.S_IMODE(private.stat.mode)
    assert _file_revision_from_revision(metadata.revision) == private


def test_metadata_rejects_every_mismatched_public_field() -> None:
    metadata = _metadata(_private_revision())
    mismatches = [
        {"kind": FileKind.DIRECTORY},
        {"size": metadata.size + 1},
        {"mode": metadata.mode ^ 1},
        {"uid": metadata.uid + 1},
        {"gid": metadata.gid + 1},
        {"modified_ns": metadata.modified_ns + 1},
    ]
    for changes in mismatches:
        with pytest.raises(ValidationError):
            replace(metadata, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "regular"},
        {"size": True},
        {"mode": 0.0},
        {"uid": "1001"},
        {"gid": None},
        {"modified_ns": False},
        {"revision": b"revision"},
    ],
)
def test_metadata_refuses_invalid_untyped_values(changes: dict[str, object]) -> None:
    metadata = _metadata(_private_revision())
    with pytest.raises(ValidationError):
        replace(metadata, **changes)


def test_read_result_requires_matching_content_bound_regular_revision() -> None:
    data = b"payload"
    private = _private_revision(size=len(data), digest=hashlib.sha256(data).digest())
    metadata = _metadata(private)
    result = ReadResult(data, metadata)
    assert result.data == data
    assert data.decode() not in repr(result)

    invalid = [
        (b"payloae", metadata),
        (data[:-1], metadata),
        (data, _metadata(_private_revision(size=len(data)))),
        (data, _metadata(_private_revision(FileKind.DIRECTORY, size=len(data)))),
    ]
    for invalid_data, invalid_metadata in invalid:
        with pytest.raises(ValidationError):
            ReadResult(invalid_data, invalid_metadata)


def test_read_result_rejects_falsey_bytes_subclass() -> None:
    class FalseyBytes(bytes):
        def __bool__(self) -> bool:
            return False

    data = b"payload"
    metadata = _metadata(_private_revision(size=len(data), digest=hashlib.sha256(data).digest()))
    with pytest.raises(ValidationError):
        ReadResult(FalseyBytes(data), metadata)


def test_directory_limits_default_to_reviewed_bounds_and_enforce_maxima() -> None:
    assert DirectoryLimit() == DirectoryLimit(1_024, 1, 1_048_576)
    assert DirectoryLimit(4_096, 8, 4 * 1_048_576).max_depth == 8

    for changes in (
        {"max_entries": 0},
        {"max_entries": 4_097},
        {"max_depth": 0},
        {"max_depth": 9},
        {"max_encoded_bytes": 0},
        {"max_encoded_bytes": 4 * 1_048_576 + 1},
        {"max_entries": True},
    ):
        with pytest.raises(ValidationError):
            DirectoryLimit(**changes)


def test_directory_entry_requires_relative_posix_path() -> None:
    metadata = _metadata(_private_revision())
    entry = DirectoryEntry(PurePosixPath("child/item"), metadata)
    assert entry.relative_path == PurePosixPath("child/item")

    for path in (
        PurePosixPath("/absolute"),
        PurePosixPath("."),
        PurePosixPath(".."),
        PurePosixPath("child/../sibling"),
        PurePosixPath("nul\0name"),
    ):
        with pytest.raises(ValidationError):
            DirectoryEntry(path, metadata)
    with pytest.raises(ValidationError):
        DirectoryEntry("child", metadata)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("owner", "group", "mode"),
    [
        ("", "group", 0o600),
        ("owner", "", 0o600),
        ("owner\0name", "group", 0o600),
        ("owner", "group\0name", 0o600),
        ("owner", "group", -1),
        ("owner", "group", 0o10000),
        ("owner", "group", True),
        ("x" * 32_768, "group", 0o600),
    ],
)
def test_new_metadata_validates_account_names_and_structural_mode(owner: object, group: object, mode: object) -> None:
    with pytest.raises(ValidationError):
        NewMetadata(owner, group, mode)  # type: ignore[arg-type]


def test_new_metadata_accepts_full_structural_mode_without_operation_policy() -> None:
    assert NewMetadata("owner", "group", 0o7777).mode == 0o7777


def test_new_metadata_rejects_account_name_subclasses() -> None:
    class AccountName(str):
        pass

    with pytest.raises(ValidationError):
        NewMetadata(AccountName("owner"), "group", 0o600)


def test_write_conditions_and_mutation_results_keep_closed_revision_children() -> None:
    digest = b"private-digest-canary-32-bytes!!"
    assert len(digest) == 32
    revision = _revision_from_file_revision(_private_revision(digest=digest))
    assert isinstance(Create(), Create)
    assert isinstance(Replace(), Replace)
    assert Match(revision).revision is revision
    assert MutationResult(Change.CHANGED, revision).revision is revision
    assert digest.hex() not in repr(MutationResult(Change.CHANGED, revision))
    assert MutationResult(Change.CHANGED, None).revision is None
    assert MutationResult(Change.UNCHANGED, None).change is Change.UNCHANGED

    with pytest.raises(ValidationError):
        Match(b"revision")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        MutationResult("changed", revision)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        MutationResult(Change.CHANGED, b"revision")  # type: ignore[arg-type]


def test_public_containers_reject_dataclass_extensions_with_diagnostic_fields() -> None:
    revision = _revision_from_file_revision(_private_revision())

    @dataclass(frozen=True)
    class ExtendedRevision(Revision):
        provider_diagnostic: bytes = b"revision-canary"

    extended_revision = ExtendedRevision(revision.token)
    assert revision.token not in repr(extended_revision).encode()
    for construct in (
        lambda: replace(_metadata(_private_revision()), revision=extended_revision),
        lambda: Match(extended_revision),
        lambda: MutationResult(Change.CHANGED, extended_revision),
    ):
        with pytest.raises(ValidationError) as caught:
            construct()
        assert "revision-canary" not in repr(caught.value)

    @dataclass(frozen=True)
    class ExtendedMetadata(FileMetadata):
        provider_diagnostic: bytes = b"metadata-canary"

    ordinary = _metadata(_private_revision())
    extended_metadata = ExtendedMetadata(
        ordinary.kind,
        ordinary.size,
        ordinary.mode,
        ordinary.uid,
        ordinary.gid,
        ordinary.modified_ns,
        ordinary.revision,
    )
    for metadata_construct in (
        lambda: DirectoryEntry(PurePosixPath("child"), extended_metadata),
        lambda: ReadResult(b"", extended_metadata),
    ):
        with pytest.raises(ValidationError) as caught:
            metadata_construct()
        assert "metadata-canary" not in repr(caught.value)


@pytest.mark.windows
def test_public_file_values_import_without_os_helpers_or_retirement_modules() -> None:
    script = r"""
import importlib.abc
import sys

blocked = (
    "agentworks.execution._file_objects",
    "agentworks.transports",
    "agentworks.ssh",
    "agentworks.remote_exec",
    "agentworks.harness_setup.runner",
    "agentworks.native_files",
    "agentworks.plugins.proxmox.transport",
    "pwd",
    "grp",
)
class Blocked(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise ImportError("blocked module loaded: " + fullname)

sys.meta_path.insert(0, Blocked())
from agentworks.execution.files import FileKind, Revision
assert FileKind.REGULAR.value == "regular"
assert Revision is not None
assert not any(name in sys.modules for name in blocked)
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
