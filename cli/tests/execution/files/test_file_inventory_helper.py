"""Real fixed-bundle checks for Linux directory inventory."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agentworks.execution._file_inventory_bundle import FIXED_BUNDLE
from agentworks.execution._file_inventory_exchange import (
    FileInventoryCandidateResult,
    FileInventoryObservationState,
    list_directory,
)
from agentworks.execution._file_inventory_protocol import (
    MAX_ENCODED_BYTES,
    FileInventoryFailureCode,
    FileInventoryRequest,
    FileInventoryResultControl,
    encode_file_inventory_request,
    encode_file_inventory_result,
    parse_file_inventory_failure,
)
from agentworks.execution._file_objects import FileKind, revision_kind
from agentworks.execution._file_wire import (
    MAX_RECORD_BODY_BYTES,
    FileRecord,
    FileRecordKind,
    FileRecordReader,
    encode_file_record,
)
from agentworks.execution._helper_bundle import FixedFileHelperBundle
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
)
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the inventory helper requires Linux")


class LocalCarrier:
    def __init__(self) -> None:
        self.calls = 0
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        self.invocation = invocation
        self.io = io
        result = run_process(list(invocation.argv), io=io, deadline=deadline)
        completion = None
        if result.exit_status is not None:
            completion = (
                ExitStatus(signal=-result.exit_status)
                if result.exit_status < 0
                else ExitStatus(code=result.exit_status)
            )
        return CarrierReport(
            Dispatch.SENT if result.started else Dispatch.NOT_SENT,
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


def _list(
    root: Path,
    relative: str,
    plan: IdentityPlan,
    source: FixedFileHelperBundle,
    *,
    runtime: Path,
    max_entries: int = 64,
    max_depth: int = 3,
    max_encoded_bytes: int = 65_536,
) -> tuple[LocalCarrier, FileInventoryCandidateResult]:
    carrier = LocalCarrier()
    with patch("agentworks.execution._file_inventory_exchange.FIXED_BUNDLE", source):
        result = list_directory(
            carrier,
            trusted_root_path=str(root),
            relative_path=relative,
            max_entries=max_entries,
            max_depth=max_depth,
            max_encoded_bytes=max_encoded_bytes,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_path=str(runtime),
        )
    return carrier, result


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "python311"])
def test_isolated_bundle_lists_sorted_utf8_metadata_without_content_reads(
    tmp_path: Path, plan: IdentityPlan, runtime: Path
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    approved = tmp_path / "approved"
    target = approved / "target"
    nested = target / "nested"
    nested.mkdir(parents=True)
    private = target / "snowman-☃"
    private.write_bytes(b"private-content-canary")
    private.chmod(0)
    (nested / "child").write_bytes(b"child")
    try:
        carrier, result = _list(approved, "target", plan, FIXED_BUNDLE, runtime=runtime)
    finally:
        private.chmod(0o600)

    assert carrier.calls == 1
    assert result.observation.state is FileInventoryObservationState.PRESENT
    assert result.observation.entries is not None
    assert [entry.relative_path for entry in result.observation.entries] == [
        "nested",
        "nested/child",
        "snowman-☃",
    ]
    assert all(entry.revision.digest is None for entry in result.observation.entries)
    assert revision_kind(result.observation.entries[0].revision) is FileKind.DIRECTORY
    assert result.observation.entries[-1].revision.stat.size == len(b"private-content-canary")
    assert carrier.io is not None and carrier.io.sensitive
    assert carrier.invocation is not None and str(approved) not in " ".join(carrier.invocation.argv)
    assert "private-content-canary" not in repr(result)
    assert "snowman" not in repr(result)


def test_empty_directory_and_missing_target_are_distinct_complete_outcomes(tmp_path: Path, plan: IdentityPlan) -> None:
    source = FIXED_BUNDLE
    approved = tmp_path / "approved"
    (approved / "empty").mkdir(parents=True)

    _, empty = _list(approved, "empty", plan, source, runtime=Path(sys.executable))
    _, missing = _list(approved, "missing", plan, source, runtime=Path(sys.executable))
    _, missing_root = _list(tmp_path / "absent", "leaf", plan, source, runtime=Path(sys.executable))

    assert empty.observation.state is FileInventoryObservationState.PRESENT
    assert empty.observation.entries == ()
    assert missing.observation.state is FileInventoryObservationState.NOT_FOUND
    assert missing_root.observation.state is FileInventoryObservationState.NOT_FOUND


@pytest.mark.parametrize("object_kind", ["symlink", "hardlink", "fifo"])
def test_helper_refuses_links_and_special_entries(tmp_path: Path, plan: IdentityPlan, object_kind: str) -> None:
    source = FIXED_BUNDLE
    approved = tmp_path / "approved"
    target = approved / "target"
    target.mkdir(parents=True)
    entry = target / "entry"
    if object_kind == "symlink":
        entry.symlink_to(target / "missing")
    elif object_kind == "hardlink":
        entry.write_bytes(b"content")
        os.link(entry, target / "other")
    else:
        os.mkfifo(entry)

    _, result = _list(approved, "target", plan, source, runtime=Path(sys.executable))

    assert result.observation.state is FileInventoryObservationState.REFUSED
    assert result.observation.failure is FileInventoryFailureCode.UNSUPPORTED_OBJECT


def test_helper_observes_socket_metadata(tmp_path: Path, plan: IdentityPlan) -> None:
    source = FIXED_BUNDLE
    approved = tmp_path / "approved"
    target = approved / "target"
    target.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX)
    target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        listener.bind(f"/proc/self/fd/{target_fd}/listener")
        _, result = _list(approved, "target", plan, source, runtime=Path(sys.executable))
    finally:
        os.close(target_fd)
        listener.close()

    assert result.observation.entries is not None
    assert revision_kind(result.observation.entries[0].revision) is FileKind.SOCKET


def test_helper_refuses_target_mount_crossing(tmp_path: Path, plan: IdentityPlan) -> None:
    if not Path("/proc").is_dir():
        pytest.skip("procfs fixture is unavailable")
    source = FIXED_BUNDLE

    _, result = _list(Path("/"), "proc", plan, source, runtime=Path(sys.executable))

    assert result.observation.state is FileInventoryObservationState.REFUSED
    assert result.observation.failure is FileInventoryFailureCode.TARGET_REFUSED


def test_helper_enforces_requested_depth_entry_and_encoded_bounds(tmp_path: Path, plan: IdentityPlan) -> None:
    source = FIXED_BUNDLE
    approved = tmp_path / "approved"
    target = approved / "target"
    nested = target / "nested"
    nested.mkdir(parents=True)
    (nested / "child").write_bytes(b"content")

    _, shallow = _list(approved, "target", plan, source, runtime=Path(sys.executable), max_depth=1)
    _, entry_limited = _list(
        approved,
        "target",
        plan,
        source,
        runtime=Path(sys.executable),
        max_entries=1,
        max_depth=2,
    )
    _, byte_limited = _list(
        approved,
        "target",
        plan,
        source,
        runtime=Path(sys.executable),
        max_encoded_bytes=2,
    )

    assert shallow.observation.entries is not None
    assert [entry.relative_path for entry in shallow.observation.entries] == ["nested"]
    assert entry_limited.observation.failure is FileInventoryFailureCode.LIMIT
    assert byte_limited.observation.failure is FileInventoryFailureCode.LIMIT


def _prepared_request(plan: IdentityPlan, root: Path, *, nonce: str = "0" * 32) -> bytes:
    return encode_file_inventory_request(
        FileInventoryRequest(nonce, str(root), "target", 8, 1, 4096, plan.expected, 1.0)
    )


def _decode_records(nonce: str, data: bytes) -> list[FileRecord]:
    records: list[FileRecord] = []
    reader = FileRecordReader(nonce, records.append)
    reader.try_write(memoryview(data))
    reader.finish()
    assert reader.error is None
    return records


def test_identity_mismatch_precedes_target_io(
    tmp_path: Path, plan: IdentityPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution import _file_inventory_guest as guest

    prepared = _prepared_request(plan, tmp_path)
    reads = [prepared, b""]
    written = bytearray()
    monkeypatch.setattr(guest, "matches_current_identity", lambda _expected: False)
    monkeypatch.setattr(guest, "open_linux_root", lambda _path: pytest.fail("target was accessed"))
    monkeypatch.setattr(os, "read", lambda _fd, _size: reads.pop(0))
    monkeypatch.setattr(os, "write", lambda _fd, data: written.extend(data) or len(data))

    assert guest.main("0" * 32) == 0
    records = _decode_records("0" * 32, bytes(written))
    assert [record.kind for record in records] == [FileRecordKind.FAILED, FileRecordKind.FINISHED]
    assert parse_file_inventory_failure(records[0].body) is FileInventoryFailureCode.IDENTITY_MISMATCH


def test_owned_descriptors_close_on_control_interruption(
    tmp_path: Path, plan: IdentityPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution import _file_inventory_guest as guest

    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    target_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    request = FileInventoryRequest("0" * 32, str(tmp_path), "target", 8, 1, 4096, plan.expected, 1.0)
    monkeypatch.setattr(guest, "open_linux_root", lambda _path: root_fd)
    monkeypatch.setattr(guest, "open_linux_confined", lambda *_args: target_fd)
    monkeypatch.setattr(
        guest, "inventory_directory", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    with pytest.raises(KeyboardInterrupt):
        guest._snapshot(request, None)
    for descriptor in (root_fd, target_fd):
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_maximum_framed_candidate_fits_qemu_7_2_capture_as_local_sizing_evidence() -> None:
    nonce = "0" * 32
    digest = hashlib.sha256(b"x" * MAX_ENCODED_BYTES).digest()
    total = 0
    sequence = 0
    for offset in range(0, MAX_ENCODED_BYTES, MAX_RECORD_BODY_BYTES):
        size = min(MAX_RECORD_BODY_BYTES, MAX_ENCODED_BYTES - offset)
        total += len(encode_file_record(nonce, FileRecord(sequence, FileRecordKind.DATA, b"x" * size)))
        sequence += 1
    result = encode_file_inventory_result(FileInventoryResultControl(MAX_ENCODED_BYTES, digest))
    total += len(encode_file_record(nonce, FileRecord(sequence, FileRecordKind.RESULT, result)))
    total += len(encode_file_record(nonce, FileRecord(sequence + 1, FileRecordKind.FINISHED, b"{}")))

    # This is only local representation sizing against QEMU 7.2's captured-output
    # capacity. It is not native transport acceptance evidence.
    assert MAX_ENCODED_BYTES < total < 16 * 1024 * 1024


def test_complete_proxmox_envelope_fits_and_delivers_real_helper_response(
    tmp_path: Path, plan: IdentityPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FIXED_BUNDLE
    approved = tmp_path / "approved"
    target = approved / "target"
    target.mkdir(parents=True)
    (target / "alpha").write_bytes(b"content")
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.invalid", "node1", 101, "token", "synthetic"))
    body_sizes: list[int] = []
    status: dict[str, object] = {}

    def request(method: str, suffix: str, *, body: bytes | None = None, timeout: float | None) -> dict[str, object]:
        if method == "POST":
            assert body is not None and body.isascii()
            body_sizes.append(len(body))
            envelope = json.loads(body)
            completed = subprocess.run(
                envelope["command"],
                input=envelope["input-data"].encode("ascii"),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            status.update(
                exited=True,
                exitcode=completed.returncode,
                **{"out-data": completed.stdout.decode("ascii"), "err-data": completed.stderr.decode("ascii")},
            )
            return {"pid": 42}
        assert suffix == "exec-status?pid=42" and body is None
        return status

    monkeypatch.setattr(carrier._wire, "request", request)
    with patch("agentworks.execution._file_inventory_exchange.FIXED_BUNDLE", source):
        result = list_directory(
            carrier,
            trusted_root_path=str(approved),
            relative_path="target",
            max_entries=8,
            max_depth=1,
            max_encoded_bytes=4096,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_path=sys.executable,
        )

    assert len(FIXED_BUNDLE.prefix) < body_sizes[0] < 65_536
    assert result.dispatch is Dispatch.SENT
    assert result.observation.state is FileInventoryObservationState.PRESENT
    assert result.observation.entries is not None
    assert [entry.relative_path for entry in result.observation.entries] == ["alpha"]
