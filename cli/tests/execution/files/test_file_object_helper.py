"""Real fixed-bundle checks for Linux file-object operations."""

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

from agentworks.execution._file_object_bundle import FIXED_BUNDLE
from agentworks.execution._file_object_exchange import (
    FileObjectCandidateResult,
    FileObjectObservationState,
    remove_file,
    stat_file,
)
from agentworks.execution._file_object_protocol import FileObjectFailureCode
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_stat import FileRevision
from agentworks.execution._helper_bundle import FixedFileHelperBundle
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
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
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file-object helper requires Linux")


class LocalCarrier:
    def __init__(self) -> None:
        self.calls = 0
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        del invocation, io

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
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


def _stat(
    root: Path,
    relative: str,
    plan: IdentityPlan,
    source: FixedFileHelperBundle,
    *,
    runtime: Path,
) -> tuple[LocalCarrier, FileObjectCandidateResult]:
    carrier = LocalCarrier()
    with patch("agentworks.execution._file_object_exchange.FIXED_BUNDLE", source):
        result = stat_file(
            carrier,
            trusted_root_path=str(root),
            relative_path=relative,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(str(runtime)),
        )
    return carrier, result


def test_missing_runtime_yields_no_object_observation(tmp_path: Path, plan: IdentityPlan) -> None:
    carrier, result = _stat(
        tmp_path,
        "missing",
        plan,
        FIXED_BUNDLE,
        runtime=Path("/missing/agentworks-python"),
    )

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
    assert result.observation is None


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "system-3.11"])
def test_isolated_bundle_stats_mode_zero_file_through_execute_only_ancestry(
    tmp_path: Path, plan: IdentityPlan, runtime: Path
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    root = tmp_path / "execute-only"
    parent = root / "nested"
    parent.mkdir(parents=True)
    target = parent / "leaf"
    target.write_bytes(b"private-canary")
    target.chmod(0)
    root.chmod(0o111)
    parent.chmod(0o111)
    try:
        carrier, result = _stat(root, "nested/leaf", plan, FIXED_BUNDLE, runtime=runtime)
    finally:
        parent.chmod(0o700)
        root.chmod(0o700)

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileObjectObservationState.PRESENT
    assert result_observation.object_kind is FileKind.REGULAR
    result_revision = result_observation.revision
    assert result_revision is not None
    assert result_observation.revision is not None and result_revision.digest is None
    assert result_revision.stat.inode == target.stat().st_ino
    assert carrier.io is not None and carrier.io.sensitive
    assert str(root) not in " ".join(carrier.invocation.argv)  # type: ignore[union-attr]
    assert "private-canary" not in repr(result)


@pytest.mark.parametrize("include_digest", [False, True])
def test_remove_through_write_and_search_parent_without_read_permission(
    tmp_path: Path, plan: IdentityPlan, include_digest: bool
) -> None:
    root = tmp_path / "write-search"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"content")
    source = FIXED_BUNDLE
    _, observed = _stat(root, "target", plan, source, runtime=Path(sys.executable))
    observed_observation = observed.observation
    assert observed_observation is not None
    observed_revision = observed_observation.revision
    assert observed_revision is not None
    expected = observed_revision
    if include_digest:
        expected = FileRevision(expected.stat, hashlib.sha256(b"content").digest())
    root.chmod(0o300)
    carrier = LocalCarrier()
    try:
        with patch("agentworks.execution._file_object_exchange.FIXED_BUNDLE", source):
            removed = remove_file(
                carrier,
                trusted_root_path=str(root),
                relative_path="target",
                expected_kind=FileKind.REGULAR,
                expected_revision=expected,
                plan=plan,
                deadline=Deadline.after(15),
                runtime_selection=runtime_selection(sys.executable),
            )
    finally:
        root.chmod(0o700)

    assert carrier.calls == 1
    removed_observation = removed.observation
    assert removed_observation is not None
    assert removed_observation.state is FileObjectObservationState.CHANGED
    assert not target.exists()


def test_missing_root_parent_and_leaf_are_complete_absence(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = FIXED_BUNDLE

    cases = ((tmp_path / "missing-root", "leaf"), (root, "missing/leaf"), (root, "leaf"))
    for candidate_root, relative in cases:
        _, result = _stat(candidate_root, relative, plan, source, runtime=Path(sys.executable))
        result_observation = result.observation
        assert result_observation is not None
        assert result_observation.state is FileObjectObservationState.ABSENT


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
    root = tmp_path / "objects"
    root.mkdir()
    (root / "directory").mkdir()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(root / "socket"))
    try:
        source = FIXED_BUNDLE
        for name, kind in (("directory", FileKind.DIRECTORY), ("socket", FileKind.SOCKET)):
            _, result = _stat(root, name, plan, source, runtime=Path(sys.executable))
            result_observation = result.observation
            assert result_observation is not None
            assert result_observation.state is FileObjectObservationState.PRESENT
            assert result_observation.object_kind is kind
            result_revision = result_observation.revision
            assert result_revision is not None
            assert result_observation.revision is not None and result_revision.digest is None
    finally:
        listener.close()


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
    mismatched_plan = IdentityPlan(
        IdentityExpectation((plan.expected.euid + 1) % (2**32), plan.expected.egid, plan.expected.groups),
        IdentityMode.DIRECT,
    )
    result = stat_file(
        carrier,
        trusted_root_path=str(root),
        relative_path="leaf",
        plan=mismatched_plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )

    assert len(FIXED_BUNDLE.prefix) < body_sizes[0] < 65_536
    assert result.dispatch is Dispatch.SENT
    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileObjectObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_failure.code is FileObjectFailureCode.IDENTITY_MISMATCH
