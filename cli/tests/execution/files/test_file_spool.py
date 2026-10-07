"""Bounded private scratch snapshots from held regular sources."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentworks.execution._file_snapshot as snapshot_module
import agentworks.execution._file_spool as spool_module
import agentworks.execution._scratch as scratch_module
from agentworks.execution._file_spool import (
    SpoolSnapshot,
    SpoolSnapshotError,
    SpoolSnapshotFailureKind,
    spool_snapshot,
)
from agentworks.execution._file_stat import FileStat
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._scratch import (
    ReadyScratchReference,
    ScratchFailureKind,
    ScratchPhase,
    ScratchReference,
    ScratchTransferError,
    cleanup_scratch,
    iter_ready_scratch,
    ready_scratch_contract,
    reconcile_scratch_ownership,
)
from agentworks.execution._scratch_receipt import (
    ScratchHistoricalOwnership,
    ScratchOperation,
    ScratchOwnershipUncertainty,
    ScratchReceiptContext,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux confined spool snapshots")

_TOKEN = bytes(range(16))


def _identity() -> IdentityExpectation:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityExpectation(os.geteuid(), os.getegid(), groups)


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _directories(tmp_path: Path) -> tuple[Path, Path, int, int]:
    source_root = tmp_path / "source"
    scratch_root = tmp_path / "scratch"
    source_root.mkdir()
    scratch_root.mkdir()
    return source_root, scratch_root, _open_directory(source_root), _open_directory(scratch_root)


def _ready_bytes(parent_fd: int, snapshot: SpoolSnapshot) -> bytes:
    return b"".join(iter_ready_scratch(parent_fd, snapshot.ready))


def _exception_details(error: BaseException) -> str:
    details: list[str] = []
    current: BaseException | None = error
    while current is not None:
        details.extend((repr(current), repr(current.args), repr(current.__dict__)))
        current = current.__cause__ or current.__context__
    return " ".join(details)


@pytest.mark.parametrize(
    "content",
    [b"", os.urandom(3 * scratch_module._MAX_CHUNK_BYTES + 17)],
    ids=["empty", "multichunk"],
)
def test_spool_returns_ready_copy_and_content_bound_source_revision(tmp_path: Path, content: bytes) -> None:
    source_root, _scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    target = source_root / "file"
    target.write_bytes(content)
    try:
        result = spool_snapshot(source_fd, "file", scratch_fd, max(1, len(content)), _TOKEN, _identity())
        assert isinstance(result, SpoolSnapshot)
        assert isinstance(result.ready, ReadyScratchReference)
        assert result.source.digest == hashlib.sha256(content).digest()
        assert result.source.stat.inode == target.stat().st_ino
        assert ready_scratch_contract(result.ready) == (len(content), hashlib.sha256(content).digest())
        assert _ready_bytes(scratch_fd, result) == content
        assert repr(content) not in repr(result)
        with pytest.raises(FrozenInstanceError):
            result.source = result.source  # type: ignore[misc]
        cleanup_scratch(scratch_fd, result.ready)
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_ready_copy_and_revision_remain_private_after_public_source_changes(tmp_path: Path) -> None:
    source_root, _scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    original = b"stable private snapshot"
    target = source_root / "file"
    target.write_bytes(original)
    try:
        result = spool_snapshot(source_fd, "file", scratch_fd, len(original), _TOKEN, _identity())
        assert result is not None
        saved_revision = result.source
        saved_ready = result.ready
        target.write_bytes(b"changed public source")

        assert result.source == saved_revision
        assert result.ready == saved_ready
        assert result.source.digest == hashlib.sha256(original).digest()
        assert _ready_bytes(scratch_fd, result) == original
        cleanup_scratch(scratch_fd, result.ready)
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_snapshot_receipt_recovers_lost_return_for_cleanup_only(tmp_path: Path) -> None:
    source_root, _scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    (source_root / "file").write_bytes(b"receipt-bound snapshot")
    token = os.urandom(16)
    identity = _identity()
    snapshot_context = ScratchReceiptContext(ScratchOperation.SNAPSHOT, identity)
    try:
        assert spool_snapshot(source_fd, "file", scratch_fd, 1024, token, identity) is not None

        historical = reconcile_scratch_ownership(scratch_fd, token, snapshot_context)
        assert isinstance(historical, ScratchHistoricalOwnership)
        stage_context = ScratchReceiptContext(ScratchOperation.STAGE, identity)
        assert isinstance(
            reconcile_scratch_ownership(scratch_fd, token, stage_context),
            ScratchOwnershipUncertainty,
        )
        with pytest.raises(ValueError):
            ready_scratch_contract(historical)  # type: ignore[arg-type]
        cleanup_scratch(scratch_fd, historical)
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_initial_absence_and_declared_oversize_create_no_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    (source_root / "large").write_bytes(b"too large")
    monkeypatch.setattr(spool_module, "begin_scratch", lambda *_args, **_kwargs: pytest.fail("scratch created"))
    monkeypatch.setattr(snapshot_module, "_read", lambda *_args: pytest.fail("source content read"))
    try:
        assert spool_snapshot(source_fd, "missing", scratch_fd, 1, _TOKEN, _identity()) is None
        with pytest.raises(SpoolSnapshotError) as raised:
            spool_snapshot(source_fd, "large", scratch_fd, 3, _TOKEN, _identity())
        assert raised.value.kind is SpoolSnapshotFailureKind.LIMIT
        assert raised.value.cleanup_debt is None
        assert not tuple(scratch_root.iterdir())
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


@pytest.mark.parametrize("expiry_event", ["lookup", "close"])
def test_initial_absence_checks_expiry_after_lookup_and_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry_event: str,
) -> None:
    source_root, scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    clock = [0.0]
    relative_path = "missing"
    if expiry_event == "lookup":
        original_path_stat = snapshot_module._path_snapshot_stat

        def stat_then_expire(parent_fd: int, leaf_name: str) -> FileStat | None:
            result = original_path_stat(parent_fd, leaf_name)
            clock[0] = 10.0
            return result

        monkeypatch.setattr(snapshot_module, "_path_snapshot_stat", stat_then_expire)
    else:
        (source_root / "nested").mkdir()
        relative_path = "nested/missing"
        original_close = snapshot_module._close

        def close_then_expire(descriptor: int) -> None:
            original_close(descriptor)
            clock[0] = 10.0

        monkeypatch.setattr(snapshot_module, "_close", close_then_expire)
    monkeypatch.setattr(snapshot_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    try:
        with pytest.raises(SpoolSnapshotError) as raised:
            spool_snapshot(source_fd, relative_path, scratch_fd, 1, _TOKEN, _identity(), expires_at=5.0)
        assert raised.value.kind is SpoolSnapshotFailureKind.DEADLINE
        assert not tuple(scratch_root.iterdir())
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_short_scratch_writes_complete_with_bounded_source_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, _scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    content = os.urandom(4 * scratch_module._MAX_CHUNK_BYTES + 31)
    (source_root / "file").write_bytes(content)
    original_read = snapshot_module._read
    original_pwrite = os.pwrite
    read_sizes: list[int] = []
    write_sizes: list[int] = []

    def recording_read(descriptor: int, size: int) -> bytes:
        read_sizes.append(size)
        return original_read(descriptor, size)

    def short_pwrite(descriptor: int, data: bytes, offset: int) -> int:
        write_sizes.append(len(data))
        return original_pwrite(descriptor, data[: max(1, len(data) // 2)], offset)

    monkeypatch.setattr(snapshot_module, "_read", recording_read)
    monkeypatch.setattr(os, "pwrite", short_pwrite)
    try:
        result = spool_snapshot(source_fd, "file", scratch_fd, len(content), _TOKEN, _identity())
        assert result is not None and _ready_bytes(scratch_fd, result) == content
        assert read_sizes and max(read_sizes) <= scratch_module._MAX_CHUNK_BYTES
        assert write_sizes and max(write_sizes) <= scratch_module._MAX_CHUNK_BYTES
        cleanup_scratch(scratch_fd, result.ready)
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


@pytest.mark.parametrize("mutation", ["growth", "shrink", "replacement", "metadata"])
def test_source_changes_during_copy_are_conflicts_without_partial_ready_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    source_root, scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    content = os.urandom(2 * scratch_module._MAX_CHUNK_BYTES + 7)
    target = source_root / "file"
    replacement = source_root / "replacement"
    target.write_bytes(content)
    replacement.write_bytes(content)
    original_write = vars(spool_module)["write_scratch_chunk"]
    changed = False

    def write_then_mutate(*args: object, **kwargs: object) -> None:
        nonlocal changed
        original_write(*args, **kwargs)  # type: ignore[arg-type]
        if changed:
            return
        changed = True
        if mutation == "growth":
            with target.open("ab") as stream:
                stream.write(b"growth")
        elif mutation == "shrink":
            target.write_bytes(content[: scratch_module._MAX_CHUNK_BYTES])
        elif mutation == "replacement":
            os.replace(replacement, target)
        else:
            target.chmod(stat.S_IMODE(target.stat().st_mode) ^ stat.S_IXUSR)

    monkeypatch.setattr(spool_module, "write_scratch_chunk", write_then_mutate)
    try:
        with pytest.raises(SpoolSnapshotError) as raised:
            spool_snapshot(source_fd, "file", scratch_fd, len(content) + 16, _TOKEN, _identity())
        assert raised.value.kind is SpoolSnapshotFailureKind.CONFLICT
        assert raised.value.cleanup_debt is None
        assert not tuple(scratch_root.iterdir())
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


@pytest.mark.parametrize("object_kind", ["symlink", "hardlink"])
def test_source_links_are_refused_without_staging(tmp_path: Path, object_kind: str) -> None:
    source_root, scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    target = source_root / "file"
    target.write_bytes(b"content")
    if object_kind == "symlink":
        link = source_root / "link"
        link.symlink_to(target)
        relative_path = "link"
    else:
        os.link(target, source_root / "other")
        relative_path = "file"
    try:
        with pytest.raises(SpoolSnapshotError) as raised:
            spool_snapshot(source_fd, relative_path, scratch_fd, 1024, _TOKEN, _identity())
        assert raised.value.kind is SpoolSnapshotFailureKind.UNSUPPORTED_OBJECT
        assert not tuple(scratch_root.iterdir())
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_descendant_mount_is_refused_without_staging(tmp_path: Path) -> None:
    if not Path("/proc/version").is_file():
        pytest.skip("procfs fixture is unavailable")
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    source_fd = _open_directory(Path("/"))
    scratch_fd = _open_directory(scratch_root)
    try:
        with pytest.raises(SpoolSnapshotError) as raised:
            spool_snapshot(source_fd, "proc/version", scratch_fd, 4096, _TOKEN, _identity())
        assert raised.value.kind is SpoolSnapshotFailureKind.UNSUPPORTED_OBJECT
        assert not tuple(scratch_root.iterdir())
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_midcopy_expiry_runs_exact_cleanup_outside_expired_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    content = os.urandom(2 * scratch_module._MAX_CHUNK_BYTES)
    (source_root / "file").write_bytes(content)
    clock = [0.0]
    original_write = vars(spool_module)["write_scratch_chunk"]

    def write_then_expire(*args: object, **kwargs: object) -> None:
        original_write(*args, **kwargs)  # type: ignore[arg-type]
        clock[0] = 10.0

    monkeypatch.setattr(spool_module, "write_scratch_chunk", write_then_expire)
    monkeypatch.setattr(snapshot_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(scratch_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    try:
        with pytest.raises(SpoolSnapshotError) as raised:
            spool_snapshot(source_fd, "file", scratch_fd, len(content), _TOKEN, _identity(), expires_at=5.0)
        assert raised.value.kind is SpoolSnapshotFailureKind.DEADLINE
        assert raised.value.cleanup_debt is None
        assert not tuple(scratch_root.iterdir())
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_cleanup_failure_preserves_primary_closed_failure_and_exact_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, _scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    content = os.urandom(2 * scratch_module._MAX_CHUNK_BYTES)
    target = source_root / "private-source-name"
    replacement = source_root / "replacement"
    target.write_bytes(content)
    replacement.write_bytes(content)
    original_write = vars(spool_module)["write_scratch_chunk"]
    real_cleanup = vars(spool_module)["cleanup_scratch"]
    changed = False

    def replace_after_write(*args: object, **kwargs: object) -> None:
        nonlocal changed
        original_write(*args, **kwargs)  # type: ignore[arg-type]
        if not changed:
            changed = True
            os.replace(replacement, target)

    def refuse_cleanup(parent_fd: int, owned: object) -> None:
        del parent_fd
        debt = scratch_module._cleanup_debt(owned)  # type: ignore[arg-type]
        raise ScratchTransferError(ScratchFailureKind.IO, ScratchPhase.CLEANUP, cleanup_debt=debt)

    monkeypatch.setattr(spool_module, "write_scratch_chunk", replace_after_write)
    monkeypatch.setattr(spool_module, "cleanup_scratch", refuse_cleanup)
    try:
        with pytest.raises(SpoolSnapshotError) as raised:
            spool_snapshot(source_fd, target.name, scratch_fd, len(content), _TOKEN, _identity())
        error = raised.value
        assert error.kind is SpoolSnapshotFailureKind.CONFLICT
        assert error.cleanup_debt is not None
        details = _exception_details(error)
        assert target.name not in details
        assert repr(content) not in details
        real_cleanup(scratch_fd, error.cleanup_debt)
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_control_interruption_preserves_exact_cleanup_debt_as_closed_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, _scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    (source_root / "file").write_bytes(b"content")
    real_cleanup = vars(spool_module)["cleanup_scratch"]

    def interrupt_write(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    def refuse_cleanup(parent_fd: int, owned: object) -> None:
        del parent_fd
        debt = scratch_module._cleanup_debt(owned)  # type: ignore[arg-type]
        raise ScratchTransferError(ScratchFailureKind.IO, ScratchPhase.CLEANUP, cleanup_debt=debt)

    monkeypatch.setattr(spool_module, "write_scratch_chunk", interrupt_write)
    monkeypatch.setattr(spool_module, "cleanup_scratch", refuse_cleanup)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            spool_snapshot(source_fd, "file", scratch_fd, 1024, _TOKEN, _identity())
        cause = raised.value.__cause__
        assert isinstance(cause, SpoolSnapshotError)
        assert cause.kind is SpoolSnapshotFailureKind.IO
        assert cause.cleanup_debt is not None
        real_cleanup(scratch_fd, cause.cleanup_debt)
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


def test_begin_debt_survives_source_close_interruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root, _scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    (source_root / "file").write_bytes(b"content")
    created: ScratchReference | None = None
    original_close = snapshot_module._close

    def failed_begin(
        parent_fd: int,
        expected_length: int,
        token: bytes,
        context: ScratchReceiptContext,
        *,
        expires_at: float | None = None,
    ) -> ScratchReference:
        nonlocal created
        created = scratch_module.begin_scratch(
            parent_fd,
            expected_length,
            token,
            context,
            expires_at=expires_at,
        )
        debt = scratch_module._cleanup_debt(created)
        raise ScratchTransferError(ScratchFailureKind.IO, ScratchPhase.BEGIN, cleanup_debt=debt)

    def interrupting_close(descriptor: int) -> None:
        original_close(descriptor)
        raise KeyboardInterrupt

    monkeypatch.setattr(spool_module, "begin_scratch", failed_begin)
    monkeypatch.setattr(snapshot_module, "_close", interrupting_close)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            spool_snapshot(source_fd, "file", scratch_fd, 1024, _TOKEN, _identity())
        cause = raised.value.__cause__
        assert isinstance(cause, SpoolSnapshotError)
        assert cause.kind is SpoolSnapshotFailureKind.IO
        assert created is not None
        assert cause.cleanup_debt == scratch_module._cleanup_debt(created)
        cleanup_scratch(scratch_fd, cause.cleanup_debt)
        created = None
    finally:
        if created is not None:
            cleanup_scratch(scratch_fd, created)
        os.close(scratch_fd)
        os.close(source_fd)


def test_scratch_is_verified_before_final_source_revalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root, _scratch_root, source_fd, scratch_fd = _directories(tmp_path)
    (source_root / "file").write_bytes(b"ordered")
    events: list[str] = []
    original_scratch_verify = vars(spool_module)["verify_scratch"]
    original_source_verify = snapshot_module._HeldRegularFile.verify

    def scratch_verify(*args: object, **kwargs: object) -> ReadyScratchReference:
        events.append("scratch")
        return original_scratch_verify(*args, **kwargs)  # type: ignore[arg-type,no-any-return]

    def source_verify(
        held: snapshot_module._HeldRegularFile,
        *,
        expires_at: float | None,
    ) -> None:
        events.append("source")
        original_source_verify(held, expires_at=expires_at)

    monkeypatch.setattr(spool_module, "verify_scratch", scratch_verify)
    monkeypatch.setattr(snapshot_module._HeldRegularFile, "verify", source_verify)
    try:
        result = spool_snapshot(source_fd, "file", scratch_fd, 1024, _TOKEN, _identity())
        assert result is not None and events == ["scratch", "source"]
        cleanup_scratch(scratch_fd, result.ready)
    finally:
        os.close(scratch_fd)
        os.close(source_fd)


@pytest.mark.parametrize(
    "runtime",
    [Path(sys.executable), Path("/usr/bin/python3.11")],
    ids=["current", "system-3.11"],
)
def test_fixed_modules_spool_under_current_and_python311(tmp_path: Path, runtime: Path) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    source_root = tmp_path / "source"
    scratch_root = tmp_path / "scratch"
    source_root.mkdir()
    scratch_root.mkdir()
    content = os.urandom(2 * scratch_module._MAX_CHUNK_BYTES + 11)
    (source_root / "file").write_bytes(content)
    package = "_agw_spool_test"
    loader = build_helper_modules(
        package,
        (
            "_file_stat",
            "_file_paths",
            "_file_snapshot",
            "_helper_identity",
            "_scratch_receipt",
            "_scratch",
            "_file_spool",
        ),
    )
    source = loader + (
        "import hashlib,os\n"
        f"s=sys.modules[{(package + '._file_spool')!r}]\n"
        f"x=sys.modules[{(package + '._scratch')!r}]\n"
        f"i=sys.modules[{(package + '._helper_identity')!r}]\n"
        f"r=os.open({str(source_root)!r},os.O_RDONLY|os.O_DIRECTORY)\n"
        f"p=os.open({str(scratch_root)!r},os.O_RDONLY|os.O_DIRECTORY)\n"
        f"expected=open({str(source_root / 'file')!r},'rb').read()\n"
        "identity=i.IdentityExpectation(os.geteuid(),os.getegid(),"
        "tuple(sorted(set(os.getgroups())|{os.getegid()})))\n"
        "try:\n"
        f" q=s.spool_snapshot(r,'file',p,{len(content)},bytes(range(16)),identity)\n"
        " assert q is not None\n"
        " assert q.source.digest==hashlib.sha256(expected).digest()\n"
        " assert b''.join(x.iter_ready_scratch(p,q.ready))==expected\n"
        " x.cleanup_scratch(p,q.ready)\n"
        "finally:os.close(p);os.close(r)\n"
    )

    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", source],
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout == completed.stderr == b""
    assert not tuple(scratch_root.iterdir())
