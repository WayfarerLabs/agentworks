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
    "_scratch",
    "_file_lock",
    "_file_publication",
    "_file_read_protocol",
)
_DISPATCHER = """
import hashlib, os, stat, sys
from _agw_file._file_publication import Create, CreateMetadata, Match, publish_file
from _agw_file._file_snapshot import read_revision

parent_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
metadata = CreateMetadata(os.getuid(), os.getgid(), 0o640)
try:
    first = publish_file(parent_fd, "target", b"first", condition=Create(), create_metadata=metadata)
    assert read_revision(parent_fd, "target", include_digest=True) == first
    second = publish_file(
        parent_fd,
        "target",
        b"second",
        condition=Match(first),
        create_metadata=metadata,
    )
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
