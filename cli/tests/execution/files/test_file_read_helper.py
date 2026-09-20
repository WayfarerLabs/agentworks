"""End-to-end checks for the fixed no-staging bounded file-read helper."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_read import (
    FileReadCandidateResult,
    FileReadObservationState,
    PreparedFileRead,
    execute_file_read,
    prepare_file_read,
    read_file,
)
from agentworks.execution._file_read_protocol import FileReadFailure, FileReadIdentity
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

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file-read helper candidate requires Linux")


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
def identity() -> FileReadIdentity:
    return FileReadIdentity(
        os.geteuid(),
        os.getegid(),
        tuple(sorted(set(os.getgroups()) | {os.getegid()})),
    )


def _read(
    root: Path,
    leaf: str,
    identity: FileReadIdentity,
    *,
    max_bytes: int = 1024,
    runtime_path: str = sys.executable,
) -> tuple[LocalCarrier, FileReadCandidateResult]:
    carrier = LocalCarrier()
    result = read_file(
        carrier,
        trusted_root_path=str(root),
        relative_path=leaf,
        max_bytes=max_bytes,
        identity=identity,
        deadline=Deadline.after(15),
        runtime_path=runtime_path,
    )
    assert carrier.calls == 1
    return carrier, result


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "system-3.11"])
def test_binary_read_uses_one_sensitive_ascii_attempt_without_staging(
    tmp_path: Path,
    identity: FileReadIdentity,
    runtime: Path,
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    content = bytes(range(256)) * 33
    target = tmp_path / "payload.bin"
    target.write_bytes(content)
    before = tuple(tmp_path.iterdir())

    carrier, result = _read(
        tmp_path,
        target.name,
        identity,
        max_bytes=len(content),
        runtime_path=str(runtime),
    )
    assert result.observation.state is FileReadObservationState.PRESENT
    snapshot = result.observation.snapshot
    assert snapshot is not None
    assert snapshot.data == content
    assert snapshot.digest == hashlib.sha256(content).digest()
    assert snapshot.metadata.size == len(content)
    assert snapshot.metadata.inode == target.stat().st_ino
    assert result.carrier_completion == ExitStatus(code=0)
    assert carrier.io is not None and carrier.io.sensitive
    assert carrier.io.input.sensitive  # type: ignore[union-attr]
    assert carrier.io.input.data.isascii()  # type: ignore[union-attr]
    assert carrier.invocation is not None
    assert all(argument.isascii() for argument in carrier.invocation.argv)
    assert str(tmp_path) not in carrier.invocation.argv
    assert tuple(tmp_path.iterdir()) == before
    assert repr(content) not in repr(result)
    assert snapshot.digest.hex() not in repr(result)


@pytest.mark.parametrize(("name", "content"), [("empty", b""), ("small", b"content")])
def test_empty_and_small_regular_files_are_complete(
    tmp_path: Path,
    identity: FileReadIdentity,
    name: str,
    content: bytes,
) -> None:
    (tmp_path / name).write_bytes(content)

    _, result = _read(tmp_path, name, identity, max_bytes=max(1, len(content)))

    assert result.observation.state is FileReadObservationState.PRESENT
    assert result.observation.snapshot is not None
    assert result.observation.snapshot.data == content


def test_absence_is_distinct_from_every_failure(tmp_path: Path, identity: FileReadIdentity) -> None:
    _, result = _read(tmp_path, "missing", identity)
    _, missing_root = _read(tmp_path / "missing-root", "file", identity)

    assert result.observation.state is FileReadObservationState.ABSENT
    assert result.observation.snapshot is None
    assert result.observation.failure is None
    assert missing_root.observation.state is FileReadObservationState.ABSENT
    assert missing_root.observation.snapshot is None


def test_special_object_and_oversize_refusals_disclose_no_bytes(
    tmp_path: Path,
    identity: FileReadIdentity,
) -> None:
    (tmp_path / "directory").mkdir()
    secret = b"oversize-secret-canary"
    (tmp_path / "large").write_bytes(secret)

    _, directory = _read(tmp_path, "directory", identity)
    _, oversized = _read(tmp_path, "large", identity, max_bytes=1)

    assert directory.observation.state is FileReadObservationState.REFUSED
    assert directory.observation.failure is FileReadFailure.UNSUPPORTED_OBJECT
    assert directory.observation.snapshot is None
    assert oversized.observation.state is FileReadObservationState.REFUSED
    assert oversized.observation.failure is FileReadFailure.LIMIT
    assert oversized.observation.snapshot is None
    assert secret.decode() not in repr(oversized)
    assert hashlib.sha256(secret).hexdigest() not in repr(oversized)


def test_identity_mismatch_precedes_target_root_access(tmp_path: Path, identity: FileReadIdentity) -> None:
    missing_root = tmp_path / "root-secret-canary"
    mismatched = FileReadIdentity(identity.euid + 100_000, identity.egid, identity.groups)

    _, result = _read(missing_root, "file", mismatched)

    assert result.observation.state is FileReadObservationState.REFUSED
    assert result.observation.failure is FileReadFailure.IDENTITY_MISMATCH
    assert result.observation.snapshot is None
    assert str(missing_root) not in repr(result)


def test_symlinked_trusted_root_is_refused_without_path_disclosure(
    tmp_path: Path,
    identity: FileReadIdentity,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "file").write_bytes(b"private")
    link = tmp_path / "root-link-canary"
    link.symlink_to(actual, target_is_directory=True)

    carrier, result = _read(link, "file", identity)

    assert result.observation.state is FileReadObservationState.REFUSED
    assert result.observation.failure is FileReadFailure.ROOT_REFUSED
    assert result.observation.snapshot is None
    assert carrier.invocation is not None and str(link) not in carrier.invocation.argv
    assert str(link) not in repr(result)


def test_root_path_is_a_valid_request_and_absence_remains_complete(identity: FileReadIdentity) -> None:
    _, result = _read(Path("/"), "agentworks-definitely-absent-file-read-fixture", identity)

    assert result.observation.state is FileReadObservationState.ABSENT


def test_prepared_attempt_cannot_be_replayed(tmp_path: Path, identity: FileReadIdentity) -> None:
    (tmp_path / "file").write_bytes(b"content")
    prepared = prepare_file_read(
        trusted_root_path=str(tmp_path),
        relative_path="file",
        max_bytes=1024,
        identity=identity,
        runtime_path=sys.executable,
    )
    carrier = LocalCarrier()
    first = execute_file_read(carrier, prepared, deadline=Deadline.after(15))

    assert first.observation.state is FileReadObservationState.PRESENT
    with pytest.raises(ValidationError):
        execute_file_read(carrier, prepared, deadline=Deadline.after(15))
    assert carrier.calls == 1


@pytest.mark.parametrize(
    ("root", "leaf", "bound"),
    [
        ("relative", "file", 1),
        ("/root/", "file", 1),
        ("/root", "/file", 1),
        ("/root", "../file", 1),
        ("/root", "file", 0),
        ("/root", "file", True),
    ],
)
def test_invalid_requests_refuse_before_carrier_construction(
    identity: FileReadIdentity,
    root: str,
    leaf: str,
    bound: object,
) -> None:
    with pytest.raises(ValidationError):
        prepare_file_read(
            trusted_root_path=root,
            relative_path=leaf,
            max_bytes=bound,  # type: ignore[arg-type]
            identity=identity,
        )


def test_prepared_request_and_result_representations_hide_paths_and_payload(
    tmp_path: Path,
    identity: FileReadIdentity,
) -> None:
    secret_path = str(tmp_path / "path-canary")
    prepared: PreparedFileRead = prepare_file_read(
        trusted_root_path=secret_path,
        relative_path="leaf-canary",
        max_bytes=1,
        identity=identity,
    )

    assert secret_path not in repr(prepared)
    assert "leaf-canary" not in repr(prepared)


def test_caller_bound_has_no_file_layer_ceiling(identity: FileReadIdentity) -> None:
    prepared = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=10**100,
        identity=identity,
    )

    assert len(prepared.io.input.data) < 1024  # type: ignore[union-attr]


def test_request_manifest_has_an_independent_finite_bound(identity: FileReadIdentity) -> None:
    with pytest.raises(ValidationError) as raised:
        prepare_file_read(
            trusted_root_path="/" + "a" * 30_000,
            relative_path="file",
            max_bytes=1,
            identity=identity,
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_invalid_utf8_path_is_not_retained_by_the_validation_exception(identity: FileReadIdentity) -> None:
    with pytest.raises(ValidationError) as raised:
        prepare_file_read(
            trusted_root_path="/secret-\udcff-canary",
            relative_path="file",
            max_bytes=1,
            identity=identity,
        )

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "canary" not in repr(raised.value)
