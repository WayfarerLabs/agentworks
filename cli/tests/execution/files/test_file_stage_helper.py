"""End-to-end checks for private stage creation and bounded chunks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agentworks.execution._file_stage_bundle import FIXED_LOADER, FIXED_SOURCE
from agentworks.execution._file_stage_exchange import FileStageObservationState, stage_begin, stage_chunk
from agentworks.execution._file_stage_protocol import MAX_STAGE_CHUNK_BYTES, FileStageFailureCode
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan, build_helper_argv
from agentworks.execution._scratch import ScratchFailureKind
from agentworks.execution._scratch_receipt import scratch_name
from agentworks.execution.carrier import Deadline, Dispatch, PreparedInvocation
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from agentworks.execution.carriers.ssh.connection import SSHConnection, build_ssh_argv
from tests.execution.files._file_stage_support import LocalCarrier, fixed_lock_source, install_fixed_lock_bundle

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the private stage helper candidate requires Linux")

_TOKEN = bytes(range(16))


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


@pytest.fixture(autouse=True)
def fixed_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    return install_fixed_lock_bundle(tmp_path / "lock-root", monkeypatch)


def _begin(root: Path, path: str, length: int, plan: IdentityPlan, *, runtime: str = sys.executable):
    carrier = LocalCarrier()
    result = stage_begin(
        carrier,
        trusted_root_path=str(root),
        relative_path=path,
        token=_TOKEN,
        expected_length=length,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=runtime,
    )
    return carrier, result


def _chunk(root: Path, path: str, reference, offset: int, data: bytes, plan: IdentityPlan):
    carrier = LocalCarrier()
    result = stage_chunk(
        carrier,
        trusted_root_path=str(root),
        relative_path=path,
        token=_TOKEN,
        reference=reference,
        offset=offset,
        data=data,
        chunk_digest=hashlib.sha256(data).digest(),
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=sys.executable,
    )
    return carrier, result


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "system-3.11"])
def test_nested_stage_uses_one_sensitive_attempt_and_exact_private_modes(
    tmp_path: Path,
    plan: IdentityPlan,
    runtime: Path,
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    root = tmp_path / "approved"
    parent = root / "nested"
    parent.mkdir(parents=True)
    payload = b"literal-private-payload"

    begin_carrier, begun = _begin(root, "nested/destination", len(payload), plan, runtime=str(runtime))

    assert begun.observation.state is FileStageObservationState.CREATED
    reference = begun.observation.reference
    assert reference is not None
    scratch = parent / scratch_name(_TOKEN)
    data_path = scratch / "data"
    receipt_path = scratch / "receipt"
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
    assert stat.S_IMODE(data_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400
    assert not (root / "destination").exists() and not (parent / "destination").exists()
    assert begin_carrier.io is not None and begin_carrier.io.sensitive
    assert begin_carrier.io.input.sensitive  # type: ignore[union-attr]
    assert begin_carrier.invocation is not None
    assert str(root) not in begin_carrier.invocation.argv and "destination" not in begin_carrier.invocation.argv

    first = payload[:7]
    second = payload[7:]
    _, written = _chunk(root, "nested/destination", reference, 0, first, plan)
    _, completed = _chunk(root, "nested/destination", reference, len(first), second, plan)

    assert written.observation.state is FileStageObservationState.ACCEPTED
    assert completed.observation.state is FileStageObservationState.ACCEPTED
    assert data_path.read_bytes() == payload
    assert payload.decode() not in repr(completed)


def test_exact_duplicate_chunk_is_accepted_without_appending(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    payload = b"duplicate"
    _, begun = _begin(root, "destination", len(payload), plan)
    reference = begun.observation.reference
    assert reference is not None

    _, first = _chunk(root, "destination", reference, 0, payload, plan)
    _, duplicate = _chunk(root, "destination", reference, 0, payload, plan)

    assert first.observation.state is FileStageObservationState.ACCEPTED
    assert duplicate.observation.state is FileStageObservationState.ACCEPTED
    assert (root / scratch_name(_TOKEN) / "data").read_bytes() == payload


def test_wrong_hash_is_refused_with_exact_cleanup_debt(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    reference = begun.observation.reference
    assert reference is not None
    carrier = LocalCarrier()

    result = stage_chunk(
        carrier,
        trusted_root_path=str(root),
        relative_path="destination",
        token=_TOKEN,
        reference=reference,
        offset=0,
        data=b"x",
        chunk_digest=hashlib.sha256(b"y").digest(),
        plan=plan,
        deadline=Deadline.after(15),
        runtime_path=sys.executable,
    )

    assert result.observation.state is FileStageObservationState.REFUSED
    failure = result.observation.failure
    assert failure is not None and failure.code is FileStageFailureCode.SCRATCH
    assert failure.kind is ScratchFailureKind.INTEGRITY
    assert failure.cleanup_debt is not None


def test_changed_receipt_is_refused_and_never_recreated(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    _, begun = _begin(root, "destination", 1, plan)
    reference = begun.observation.reference
    assert reference is not None
    receipt = root / scratch_name(_TOKEN) / "receipt"
    receipt.chmod(0o600)
    receipt.write_bytes(b"changed")
    receipt.chmod(0o400)

    _, result = _chunk(root, "destination", reference, 0, b"x", plan)

    assert result.observation.state is FileStageObservationState.REFUSED
    failure = result.observation.failure
    assert failure is not None and failure.code is FileStageFailureCode.SCRATCH
    assert failure.kind is ScratchFailureKind.CONFLICT
    assert failure.cleanup_debt is not None
    assert receipt.read_bytes() == b"changed"


@pytest.mark.parametrize(
    ("root_exists", "parent_exists", "failure"),
    [
        (False, False, FileStageFailureCode.ROOT_REFUSED),
        (True, False, FileStageFailureCode.PARENT_REFUSED),
    ],
)
def test_private_creation_absence_is_a_refusal(
    tmp_path: Path,
    plan: IdentityPlan,
    root_exists: bool,
    parent_exists: bool,
    failure: FileStageFailureCode,
) -> None:
    root = tmp_path / "approved"
    if root_exists:
        root.mkdir()
    if parent_exists:
        (root / "nested").mkdir()

    _, result = _begin(root, "nested/destination", 1, plan)

    assert result.observation.state is FileStageObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.code is failure


def test_identity_mismatch_precedes_root_access(tmp_path: Path, plan: IdentityPlan) -> None:
    missing_root = tmp_path / "private-root-canary"
    expected = plan.expected
    mismatched = IdentityPlan(
        IdentityExpectation(expected.euid + 100_000, expected.egid, expected.groups),
        IdentityMode.DIRECT,
    )

    _, result = _begin(missing_root, "destination", 1, mismatched)

    assert result.observation.state is FileStageObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.code is FileStageFailureCode.IDENTITY_MISMATCH
    assert str(missing_root) not in repr(result)


def test_missing_fixed_lock_refuses_before_root_access(tmp_path: Path, plan: IdentityPlan) -> None:
    lock = tmp_path / "lock-root/var/lib/agentworks/execution/files.lock"
    lock.unlink()

    _, result = _begin(tmp_path / "missing-root", "destination", 1, plan)

    assert result.observation.state is FileStageObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.code is FileStageFailureCode.LOCK_MISSING


def test_helper_records_and_receipt_tolerate_short_writes(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_source = """
real_write=guest.os.write
def short_write(fd,data):
 return real_write(fd,data[:max(1,len(data)//3)])
guest.os.write=short_write
"""
    source = fixed_lock_source(tmp_path / "lock-root", patch_source)
    monkeypatch.setattr("agentworks.execution._file_stage_exchange.FIXED_SOURCE", source)
    root = tmp_path / "approved"
    root.mkdir()

    _, result = _begin(root, "destination", 1, plan)

    assert result.observation.state is FileStageObservationState.CREATED


def test_complete_proxmox_bodies_fit_for_begin_and_maximum_chunk(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    fixed_source: str,
) -> None:
    root = tmp_path / "approved"
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
            status.clear()
            status.update(
                exited=True,
                exitcode=completed.returncode,
                **{"out-data": completed.stdout.decode("ascii"), "err-data": completed.stderr.decode("ascii")},
            )
            return {"pid": len(body_sizes)}
        assert suffix.startswith("exec-status?pid=") and body is None
        return status

    monkeypatch.setattr(carrier._wire, "request", request)
    with patch("agentworks.execution._file_stage_exchange.FIXED_SOURCE", fixed_source):
        begun = stage_begin(
            carrier,
            trusted_root_path=str(root),
            relative_path="destination",
            token=_TOKEN,
            expected_length=MAX_STAGE_CHUNK_BYTES,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_path=sys.executable,
        )
        reference = begun.observation.reference
        assert reference is not None
        payload = b"x" * MAX_STAGE_CHUNK_BYTES
        written = stage_chunk(
            carrier,
            trusted_root_path=str(root),
            relative_path="destination",
            token=_TOKEN,
            reference=reference,
            offset=0,
            data=payload,
            chunk_digest=hashlib.sha256(payload).digest(),
            plan=plan,
            deadline=Deadline.after(15),
            runtime_path=sys.executable,
        )

    assert begun.dispatch is Dispatch.SENT and begun.observation.state is FileStageObservationState.CREATED
    assert written.dispatch is Dispatch.SENT and written.observation.state is FileStageObservationState.ACCEPTED
    assert len(body_sizes) == 2
    assert len(FIXED_LOADER) < body_sizes[0] < body_sizes[1] < 65_536


def test_fixed_helper_retains_windows_command_line_headroom(plan: IdentityPlan) -> None:
    invocation = PreparedInvocation(
        build_helper_argv(plan, runtime_path="/usr/bin/python3", fixed_source=FIXED_SOURCE, nonce="0" * 32)
    )
    connection = SSHConnection(
        "host.example",
        "agent",
        Path("/keys/identity"),
        Path("/keys/known-hosts"),
    )
    ssh_argv = build_ssh_argv(connection, invocation)
    windows_command = subprocess.list2cmdline(ssh_argv)

    assert len(FIXED_LOADER) < len(FIXED_SOURCE) < len(ssh_argv[-1]) < len(windows_command) < 32_767
