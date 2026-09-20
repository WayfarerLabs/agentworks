"""Real fixed-bundle checks for locked Linux file-object operations."""

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

from agentworks.execution._file_object_bundle import FIXED_LOADER
from agentworks.execution._file_object_exchange import (
    FileObjectCandidateResult,
    FileObjectObservationState,
    remove_file,
    stat_file,
)
from agentworks.execution._file_object_protocol import FileObjectFailureCode, parse_file_object_failure
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_stat import FileRevision
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader
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

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file-object helper requires Linux")


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


def _provision_lock_root(path: Path) -> Path:
    lock_root = path / "lock-root"
    lock_root.mkdir(mode=0o700)
    current = lock_root
    for component in ("var", "lib", "agentworks", "execution"):
        current = current / component
        current.mkdir(mode=0o700)
    lock = current / "files.lock"
    lock.touch(mode=0o444)
    lock.chmod(0o444)
    return lock_root


def _fixture_source(lock_root: Path) -> str:
    package = "_agw_file_object"
    return FIXED_LOADER + (
        "import contextlib,os\n"
        f"g=sys.modules[{(package + '._file_object_guest')!r}]\n"
        f"l=sys.modules[{(package + '._file_lock')!r}]\n"
        "@contextlib.contextmanager\n"
        "def fixture_lock(*,expires_at):\n"
        f" d=os.open({str(lock_root)!r},os.O_PATH|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)\n"
        " try:\n"
        f"  with l._file_lock_at_root(d,{os.geteuid()},expires_at=expires_at):yield\n"
        " finally:os.close(d)\n"
        "g.system_file_lock=fixture_lock\n"
        f"raise SystemExit(g.main(sys.argv[1]))\n"
    )


def _stat(
    root: Path,
    relative: str,
    plan: IdentityPlan,
    source: str,
    *,
    runtime: Path,
) -> tuple[LocalCarrier, FileObjectCandidateResult]:
    carrier = LocalCarrier()
    # The production request and argv shape stay intact; only fixed test source
    # redirects the fixed lock namespace to the owned fixture.
    with patch("agentworks.execution._file_object_exchange.FIXED_SOURCE", source):
        result = stat_file(
            carrier,
            trusted_root_path=str(root),
            relative_path=relative,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_path=str(runtime),
        )
    return carrier, result


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "system-3.11"])
def test_isolated_bundle_stats_mode_zero_file_through_execute_only_ancestry(
    tmp_path: Path, plan: IdentityPlan, runtime: Path
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    lock_root = _provision_lock_root(tmp_path)
    root = tmp_path / "execute-only"
    parent = root / "nested"
    parent.mkdir(parents=True)
    target = parent / "leaf"
    target.write_bytes(b"private-canary")
    target.chmod(0)
    root.chmod(0o111)
    parent.chmod(0o111)
    try:
        carrier, result = _stat(root, "nested/leaf", plan, _fixture_source(lock_root), runtime=runtime)
    finally:
        parent.chmod(0o700)
        root.chmod(0o700)

    assert carrier.calls == 1
    assert result.observation.state is FileObjectObservationState.PRESENT
    assert result.observation.object_kind is FileKind.REGULAR
    assert result.observation.revision is not None and result.observation.revision.digest is None
    assert result.observation.revision.stat.inode == target.stat().st_ino
    assert carrier.io is not None and carrier.io.sensitive
    assert str(root) not in " ".join(carrier.invocation.argv)  # type: ignore[union-attr]
    assert "private-canary" not in repr(result)


@pytest.mark.parametrize("include_digest", [False, True])
def test_remove_through_write_and_search_parent_without_read_permission(
    tmp_path: Path, plan: IdentityPlan, include_digest: bool
) -> None:
    lock_root = _provision_lock_root(tmp_path)
    root = tmp_path / "write-search"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"content")
    source = _fixture_source(lock_root)
    _, observed = _stat(root, "target", plan, source, runtime=Path(sys.executable))
    assert observed.observation.revision is not None
    expected = observed.observation.revision
    if include_digest:
        expected = FileRevision(expected.stat, hashlib.sha256(b"content").digest())
    root.chmod(0o300)
    carrier = LocalCarrier()
    try:
        with patch("agentworks.execution._file_object_exchange.FIXED_SOURCE", source):
            removed = remove_file(
                carrier,
                trusted_root_path=str(root),
                relative_path="target",
                expected_kind=FileKind.REGULAR,
                expected_revision=expected,
                plan=plan,
                deadline=Deadline.after(15),
                runtime_path=sys.executable,
            )
    finally:
        root.chmod(0o700)

    assert carrier.calls == 1
    assert removed.observation.state is FileObjectObservationState.CHANGED
    assert not target.exists()


def test_missing_root_parent_and_leaf_are_complete_absence_after_lock(tmp_path: Path, plan: IdentityPlan) -> None:
    lock_root = _provision_lock_root(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    source = _fixture_source(lock_root)

    cases = ((tmp_path / "missing-root", "leaf"), (root, "missing/leaf"), (root, "leaf"))
    for candidate_root, relative in cases:
        _, result = _stat(candidate_root, relative, plan, source, runtime=Path(sys.executable))
        assert result.observation.state is FileObjectObservationState.ABSENT


def test_nested_parent_control_interruption_closes_owned_root(
    tmp_path: Path, plan: IdentityPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution import _file_object_guest as guest
    from agentworks.execution._file_object_protocol import FileObjectOperation, FileObjectRequest

    root_fd = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    request = FileObjectRequest("0" * 32, FileObjectOperation.STAT, str(tmp_path), "nested/leaf", 1.0, plan.expected)
    monkeypatch.setattr(guest, "open_linux_root", lambda _path: root_fd)
    monkeypatch.setattr(guest, "open_linux_confined", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        guest._parent(request)
    with pytest.raises(OSError):
        os.fstat(root_fd)


@pytest.mark.parametrize("operation", ["STAT", "REMOVE"])
def test_root_absence_checks_guest_local_expiry_before_reporting_no_effect(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from agentworks.execution import _file_object_guest as guest
    from agentworks.execution._file_object_protocol import FileObjectOperation, FileObjectRequest
    from agentworks.execution._file_objects import FileKind, FileObjectFailureKind
    from agentworks.execution._file_stat import FileRevision, FileStat

    selected = FileObjectOperation[operation]
    revision = FileRevision(FileStat(1, 2, 0o100600, 1, os.geteuid(), os.getegid(), 0, 3, 4))
    request = FileObjectRequest(
        "0" * 32,
        selected,
        str(tmp_path / "missing"),
        "leaf",
        0.0,
        plan.expected,
        FileKind.REGULAR if selected is FileObjectOperation.REMOVE else None,
        revision if selected is FileObjectOperation.REMOVE else None,
    )
    monkeypatch.setattr(guest, "open_linux_root", lambda _path: None)
    monkeypatch.setattr("agentworks.execution._file_object_guest.time.monotonic", lambda: 2.0)

    with pytest.raises(guest._SafeFailure) as raised:
        guest._operate(request, 1.0)
    assert raised.value.failure.kind is FileObjectFailureKind.DEADLINE


def test_stat_reports_directory_and_socket_metadata_without_content(tmp_path: Path, plan: IdentityPlan) -> None:
    lock_root = _provision_lock_root(tmp_path)
    root = tmp_path / "objects"
    root.mkdir()
    (root / "directory").mkdir()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(root / "socket"))
    try:
        source = _fixture_source(lock_root)
        for name, kind in (("directory", FileKind.DIRECTORY), ("socket", FileKind.SOCKET)):
            _, result = _stat(root, name, plan, source, runtime=Path(sys.executable))
            assert result.observation.state is FileObjectObservationState.PRESENT
            assert result.observation.object_kind is kind
            assert result.observation.revision is not None and result.observation.revision.digest is None
    finally:
        listener.close()


@pytest.mark.parametrize(
    ("lock_kind", "failure_code"),
    [
        ("MISSING", FileObjectFailureCode.LOCK_MISSING),
        ("UNSAFE", FileObjectFailureCode.LOCK_UNSAFE),
    ],
)
def test_missing_and_unsafe_lock_are_complete_refusals_without_installation(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    lock_kind: str,
    failure_code: FileObjectFailureCode,
) -> None:
    from agentworks.execution import _file_object_guest as guest
    from agentworks.execution._file_lock import FileLockError, FileLockFailureKind

    def refusing_lock(*, expires_at: float | None):
        del expires_at
        raise FileLockError(FileLockFailureKind[lock_kind])

    monkeypatch.setattr(guest, "system_file_lock", refusing_lock)

    root = tmp_path / "root"
    root.mkdir()
    written = bytearray()
    from agentworks.execution._file_object_protocol import (
        FileObjectOperation,
        FileObjectRequest,
        encode_file_object_request,
    )

    prepared_input = encode_file_object_request(
        FileObjectRequest(
            "0" * 32,
            FileObjectOperation.STAT,
            str(root),
            "leaf",
            1.0,
            plan.expected,
        )
    )
    chunks = [prepared_input, b""]
    monkeypatch.setattr(os, "read", lambda _fd, _size: chunks.pop(0))
    monkeypatch.setattr(os, "write", lambda _fd, data: written.extend(data) or len(data))
    assert guest.main("0" * 32) == 0
    records: list[FileRecord] = []
    reader = FileRecordReader("0" * 32, records.append)
    reader.try_write(memoryview(written))
    reader.finish()
    assert reader.error is None
    assert [record.kind for record in records] == [FileRecordKind.FAILED, FileRecordKind.FINISHED]
    assert parse_file_object_failure(records[0].body).code is failure_code
    assert not tuple(root.iterdir())


def test_complete_proxmox_post_fits_provider_bound_and_returns_typed_outcome(
    tmp_path: Path, plan: IdentityPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
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
                **{"out-data": completed.stdout.decode("ascii"), "err-data": ""},
            )
            return {"pid": 42}
        assert suffix == "exec-status?pid=42" and body is None
        return status

    monkeypatch.setattr(carrier._wire, "request", request)
    result = stat_file(
        carrier,
        trusted_root_path=str(root),
        relative_path="leaf",
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=sys.executable,
    )

    assert len(FIXED_LOADER) < body_sizes[0] < 65_536
    assert result.dispatch is Dispatch.SENT
    assert result.observation.state is FileObjectObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.code in {
        FileObjectFailureCode.LOCK_MISSING,
        FileObjectFailureCode.LOCK_UNSAFE,
    }
