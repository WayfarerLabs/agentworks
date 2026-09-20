"""Standalone execution of the complete fixed file-helper module bundle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.execution._file_stage_protocol import FileStageBeginRequest, encode_file_stage_request
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan, build_helper_argv
from agentworks.execution.carrier import PreparedInvocation
from agentworks.execution.carriers.ssh.connection import SSHConnection, build_ssh_argv

_LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="Linux file helper")

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

_SNAPSHOT_PACKAGE = "_agw_snapshot"
_SNAPSHOT_MODULE_NAMES = (
    "_helper_identity",
    "_file_stat",
    "_file_paths",
    "_file_snapshot",
    "_scratch_receipt",
    "_scratch",
    "_file_spool",
    "_scratch_root",
    "_file_lock",
    "_file_wire",
    "_scratch_wire",
)
_SNAPSHOT_DISPATCHER = f"""
import os, sys

identity_module = sys.modules[{(_SNAPSHOT_PACKAGE + "._helper_identity")!r}]
root_module = sys.modules[{(_SNAPSHOT_PACKAGE + "._scratch_root")!r}]
scratch_module = sys.modules[{(_SNAPSHOT_PACKAGE + "._scratch")!r}]
spool_module = sys.modules[{(_SNAPSHOT_PACKAGE + "._file_spool")!r}]
root_module._LINUX_SCRATCH_ROOT = sys.argv[2]
root_module._EXPECTED_OWNER_UID = os.geteuid()
source_fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
scratch_fd = root_module.open_scratch_root(expires_at=None)
identity = identity_module.IdentityExpectation(
    os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {{os.getegid()}}))
)
try:
    result = spool_module.spool_snapshot(
        source_fd, "payload", scratch_fd, 49183, bytes(range(16)), identity
    )
    assert result is not None
    with open(sys.argv[1] + "/payload", "rb") as expected:
        assert b"".join(scratch_module.iter_ready_scratch(scratch_fd, result.ready)) == expected.read()
    scratch_module.cleanup_scratch(scratch_fd, result.ready)
finally:
    os.close(scratch_fd)
    os.close(source_fd)
"""
_SNAPSHOT_SOURCE = build_helper_modules(_SNAPSHOT_PACKAGE, _SNAPSHOT_MODULE_NAMES) + _SNAPSHOT_DISPATCHER

_SURROGATE_PACKAGE = "_agw_snapshot_surrogate"
_SURROGATE_MODULE_NAMES = _SNAPSHOT_MODULE_NAMES + ("_file_stage_protocol", "_file_stage_guest")
_SURROGATE_LOADER = build_helper_modules(_SURROGATE_PACKAGE, _SURROGATE_MODULE_NAMES)
_SURROGATE_SOURCE = _SURROGATE_LOADER + (
    f"raise SystemExit(sys.modules[{(_SURROGATE_PACKAGE + '._file_stage_guest')!r}].main(sys.argv[1]))\n"
)


@_LINUX_ONLY
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


@_LINUX_ONLY
@pytest.mark.parametrize(
    "interpreter",
    [Path(sys.executable), Path("/usr/bin/python3.11")],
    ids=["current", "distribution-3.11"],
)
def test_snapshot_module_bundle_spools_from_read_only_source_into_fixed_scratch(
    tmp_path: Path,
    interpreter: Path,
) -> None:
    if not interpreter.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {interpreter}")
    if os.geteuid() == 0:
        pytest.skip("read-only source authority must be tested without root bypass")
    source_root = tmp_path / "source"
    scratch_root = tmp_path / "scratch"
    source_root.mkdir()
    scratch_root.mkdir()
    scratch_root.chmod(0o1777)
    content = b"private-snapshot-canary-" + os.urandom(49_159)
    (source_root / "payload").write_bytes(content)
    source_before = tuple(source_root.iterdir())
    source_root.chmod(0o555)
    retained = scratch_root / "retained"
    retained.write_bytes(b"unrelated")
    scratch_before = tuple(scratch_root.iterdir())
    fixed_source = _SNAPSHOT_SOURCE
    try:
        completed = subprocess.run(
            [str(interpreter), "-I", "-S", "-B", "-c", fixed_source, str(source_root), str(scratch_root)],
            cwd=tmp_path,
            capture_output=True,
            timeout=20,
            check=False,
        )
    finally:
        source_root.chmod(0o700)

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert completed.stdout == completed.stderr == b""
    assert content not in fixed_source.encode()
    assert str(source_root) not in fixed_source
    assert str(scratch_root) not in fixed_source
    assert fixed_source == build_helper_modules(_SNAPSHOT_PACKAGE, _SNAPSHOT_MODULE_NAMES) + _SNAPSHOT_DISPATCHER
    assert tuple(source_root.iterdir()) == source_before
    assert (source_root / "payload").read_bytes() == content
    assert tuple(scratch_root.iterdir()) == scratch_before
    assert retained.read_bytes() == b"unrelated"


def test_snapshot_surrogate_retains_windows_and_qga_delivery_headroom() -> None:
    identity = IdentityExpectation(1001, 1001, (1001,))
    plan = IdentityPlan(identity, IdentityMode.DEMOTE)
    request = encode_file_stage_request(
        FileStageBeginRequest(
            "0" * 32,
            "/approved/source",
            "nested/payload-delivery-canary",
            bytes(range(16)),
            1 << 30,
            identity,
            60.0,
        )
    )
    invocation = PreparedInvocation(
        build_helper_argv(
            plan,
            runtime_path="/usr/bin/python3",
            fixed_source=_SURROGATE_SOURCE,
            nonce="0" * 32,
        )
    )
    native_root = Path(Path.cwd().anchor)
    connection = SSHConnection(
        "host.example",
        "agent",
        native_root / "keys" / "identity",
        native_root / "keys" / "known-hosts",
    )
    ssh_argv = build_ssh_argv(connection, invocation)
    windows_command = subprocess.list2cmdline(ssh_argv)
    qga_body = json.dumps({"command": invocation.argv, "input-data": request.decode("ascii")}).encode("ascii")

    assert request.decode("ascii") not in _SURROGATE_SOURCE
    assert len(_SURROGATE_LOADER) < len(_SURROGATE_SOURCE) < len(ssh_argv[-1]) < len(windows_command) < 32_767
    assert len(_SURROGATE_SOURCE) < len(qga_body) < 65_536
