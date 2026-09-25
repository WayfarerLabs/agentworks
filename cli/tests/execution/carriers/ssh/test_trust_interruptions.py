"""Trust maintenance retains control-flow signals and bounds source snapshots."""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from agentworks.errors import StateError
from agentworks.execution.carriers.ssh import _trust_files as files
from agentworks.execution.carriers.ssh.trust import (
    SSHTrustFiles,
    import_trust,
    refresh_trust,
    resolve_trust,
    trust_status,
)

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("stage", ["initial-block", "recovery-block", "interrupted-publication"])
def test_interruption_survives_failed_blocking_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException], stage: str
) -> None:
    root = tmp_path.resolve()
    source = root / "source"
    source.write_bytes(b"original policy")
    sources = SSHTrustFiles((source,))
    bundle = import_trust(root / "bundle", sources=sources, authority="fixture")
    before = trust_status(bundle)
    write_state = files.write_state
    control = interruption()
    blocks = 0

    def write(directory: Path, state: dict[str, object]) -> None:
        nonlocal blocks
        if state["blocked"]:
            blocks += 1
            if stage == "initial-block" or blocks == 2 and stage == "recovery-block":
                raise control
            if blocks == 2 and stage == "interrupted-publication":
                raise OSError("recovery write failed")
        write_state(directory, state)
        if not state["blocked"]:
            if stage == "interrupted-publication":
                raise control
            raise OSError("uncertain active publication")

    monkeypatch.setattr(files, "write_state", write)
    with pytest.raises(interruption) as raised:
        refresh_trust(bundle, sources=sources, authority=before.authority, expected_generation=before.generation)
    assert raised.value is control
    # A bounded authored note preserves the need for external quiescence,
    # without asserting on its prose or replacing the control-flow exception.
    assert raised.value.__notes__
    current = trust_status(bundle)
    assert not current.blocked
    if stage == "initial-block":
        assert current == before
    else:
        assert current.generation != before.generation
    # Resolving proves the interruption unwound the admission lock. These
    # cases cannot claim a block: composition must act on the attached note.
    resolve_trust(bundle)


@pytest.mark.parametrize("operation", ["copy", "hash"])
@pytest.mark.parametrize("mutation", ["append", "truncate"])
def test_snapshot_read_has_a_fixed_byte_budget_and_refuses_changed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, mutation: str
) -> None:
    source = tmp_path.resolve() / "source"
    # More than one chunk exercises early EOF as well as continuous growth.
    original = b"x" * (256 * 1024 + 1)
    source.write_bytes(original)
    reads: list[int] = []
    read_file = files.read_file

    class ChangingReader(io.BufferedReader):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None and size >= 0
            reads.append(size)
            # A broken unbounded read loop fails promptly instead of hanging
            # the suite as this source grows after every read.
            if len(reads) > 2:
                pytest.fail("Read exceeded the original snapshot byte budget")
            content = super().read(size)
            with source.open("ab" if mutation == "append" else "wb") as writer:
                writer.write(b"y" * (256 * 1024) if mutation == "append" else b"")
            return content

    @contextmanager
    def changing_file(path: Path, *, owned: bool = False) -> Iterator[BinaryIO]:
        if path == source:
            with ChangingReader(io.FileIO(path, "rb")) as reader:
                yield reader
        else:
            with read_file(path, owned=owned) as reader:
                yield reader

    monkeypatch.setattr(files, "read_file", changing_file)
    with pytest.raises(StateError):
        if operation == "copy":
            files.copy_file(source, tmp_path.resolve() / "copy")
        else:
            files.file_hash(source, owned=False)
    assert sum(reads) <= len(original)
    assert source.stat().st_size != len(original)


def test_successful_snapshot_digest_matches_all_bytes(tmp_path: Path) -> None:
    import hashlib

    source = tmp_path.resolve() / "source"
    content = os.urandom(256 * 1024 + 17)
    source.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    destination = tmp_path.resolve() / "copy"
    assert files.copy_file(source, destination) == expected
    assert files.file_hash(destination) == expected
    assert destination.read_bytes() == content


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("change", [None, "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"])
def test_snapshot_path_comparison_respects_platform_timestamp_meaning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, change: str | None
) -> None:
    source = tmp_path.resolve() / "source"
    source.write_bytes(b"stable policy")
    with source.open("rb") as reader:
        before = os.fstat(reader.fileno())
        attributes = {name: getattr(before, name) for name in ("st_mtime_ns", "st_ctime_ns")}
        sequence = list(before)
        if change in {"st_dev", "st_ino", "st_size"}:
            index = {"st_dev": 2, "st_ino": 1, "st_size": 6}[change]
            sequence[index] += 1
        elif change is not None:
            attributes[change] += 1
        current = os.stat_result(sequence, attributes)
        monkeypatch.setattr(Path, "lstat", lambda self: current)
        monkeypatch.setattr(files, "sys", SimpleNamespace(platform=platform))
        if change is None or (platform == "win32" and change == "st_ctime_ns"):
            files._verify_snapshot(reader, source, before)
        else:
            with pytest.raises(StateError):
                files._verify_snapshot(reader, source, before)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_snapshot_refuses_descriptor_change_time_even_when_path_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    source = tmp_path.resolve() / "source"
    source.write_bytes(b"stable policy")
    with source.open("rb") as reader:
        before = os.fstat(reader.fileno())
        after = os.stat_result(before, {"st_mtime_ns": before.st_mtime_ns, "st_ctime_ns": before.st_ctime_ns + 1})
        monkeypatch.setattr(os, "fstat", lambda descriptor: after)
        monkeypatch.setattr(Path, "lstat", lambda self: before)
        monkeypatch.setattr(files, "sys", SimpleNamespace(platform=platform))
        with pytest.raises(StateError):
            files._verify_snapshot(reader, source, before)


@pytest.mark.skipif(os.name == "nt", reason="Windows capture handles refuse replacement before snapshot validation")
@pytest.mark.parametrize("operation", ["copy", "hash"])
def test_snapshot_refuses_replaced_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    source = tmp_path.resolve() / "source"
    source.write_bytes(b"original policy")
    replacement = source.with_name("replacement")
    replacement.write_bytes(b"replacement key")
    chunks = files._snapshot_chunks

    def replace_after_read(reader: BinaryIO, remaining: int) -> Iterator[bytes]:
        yield from chunks(reader, remaining)
        os.replace(replacement, source)

    monkeypatch.setattr(files, "_snapshot_chunks", replace_after_read)
    with pytest.raises(StateError):
        if operation == "copy":
            files.copy_file(source, source.with_name("copy"))
        else:
            files.file_hash(source, owned=False)
    assert source.read_bytes() == b"replacement key"
