"""Standalone execution of the complete fixed file-helper module bundle."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution._helper_bundle import build_helper_modules

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file helper")

_PACKAGE = "_agw_file"
_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_paths",
    "_file_snapshot",
    "_scratch_receipt",
    "_scratch",
    "_file_lock",
    "_file_publication",
    "_file_wire",
    "_file_read_protocol",
)
_DISPATCHER = """
import hashlib, os, secrets, stat, sys
from _agw_file._file_publication import Create, CreateMetadata, Match, ScratchFileSource, publish_file
from _agw_file._file_snapshot import read_revision
from _agw_file._scratch import begin_scratch, cleanup_scratch, verify_scratch, write_scratch_chunk
from _agw_file._scratch_receipt import ScratchOperation, current_receipt_context

parent_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
metadata = CreateMetadata(os.getuid(), os.getgid(), 0o640)
try:
    first = publish_file(parent_fd, "target", b"first", condition=Create(), create_metadata=metadata)
    assert read_revision(parent_fd, "target", include_digest=True) == first
    content = b"second"
    reference = begin_scratch(
        parent_fd, len(content), secrets.token_bytes(16), current_receipt_context(ScratchOperation.STAGE)
    )
    write_scratch_chunk(parent_fd, reference, 0, content[:3], hashlib.sha256(content[:3]).digest())
    write_scratch_chunk(parent_fd, reference, 3, content[3:], hashlib.sha256(content[3:]).digest())
    ready = verify_scratch(parent_fd, reference, hashlib.sha256(content).digest())
    second = publish_file(
        parent_fd,
        "target",
        ScratchFileSource(parent_fd, ready),
        condition=Match(first),
        create_metadata=metadata,
    )
    cleanup_scratch(parent_fd, ready)
    assert read_revision(parent_fd, "target", include_digest=True) == second
    assert first.stat.inode != second.stat.inode
    assert second.digest == hashlib.sha256(b"second").digest()
    assert stat.S_IMODE(second.stat.mode) == 0o640
finally:
    os.close(parent_fd)
"""
_FIXED_SOURCE = build_helper_modules(_PACKAGE, _MODULE_NAMES) + _DISPATCHER


@pytest.mark.parametrize(
    "interpreter",
    [Path(sys.executable), Path("/usr/bin/python3.11")],
    ids=["current", "distribution-3.11"],
)
def test_full_file_helper_bundle_publishes_and_revises_without_agentworks(
    tmp_path: Path,
    interpreter: Path,
) -> None:
    if not interpreter.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {interpreter}")

    completed = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", _FIXED_SOURCE, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout == b""
    assert completed.stderr == b""
    target = tmp_path / "target"
    assert target.read_bytes() == b"second"
    assert target.stat().st_mode & 0o777 == 0o640
