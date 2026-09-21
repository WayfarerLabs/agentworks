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
    read_file,
)
from agentworks.execution._file_read_bundle import FIXED_BUNDLE
from agentworks.execution._file_read_protocol import FileReadFailure
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import Deadline, ExitStatus
from tests.execution.files._file_read_support import LocalCarrier
from tests.execution.files._runtime_support import require_observation, require_value, runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file-read helper candidate requires Linux")


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


def _read(
    root: Path,
    leaf: str,
    plan: IdentityPlan,
    *,
    max_bytes: int = 1024,
    runtime: str = sys.executable,
) -> tuple[LocalCarrier, FileReadCandidateResult]:
    carrier = LocalCarrier()
    result = read_file(
        carrier,
        trusted_root_path=str(root),
        relative_path=leaf,
        max_bytes=max_bytes,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(runtime),
    )
    assert carrier.calls == 1
    return carrier, result


def test_missing_runtime_yields_no_file_observation(tmp_path: Path, plan: IdentityPlan) -> None:
    carrier, result = _read(tmp_path, "missing", plan, runtime="/missing/agentworks-python")

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
    assert result.observation is None


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")], ids=["current", "system-3.11"])
def test_binary_read_uses_one_sensitive_ascii_attempt_without_staging(
    tmp_path: Path,
    plan: IdentityPlan,
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
        plan,
        max_bytes=len(content),
        runtime=str(runtime),
    )
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert require_observation(result.observation).state is FileReadObservationState.PRESENT
    snapshot = require_observation(result.observation).snapshot
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
    plan: IdentityPlan,
    name: str,
    content: bytes,
) -> None:
    (tmp_path / name).write_bytes(content)

    _, result = _read(tmp_path, name, plan, max_bytes=max(1, len(content)))

    assert require_observation(result.observation).state is FileReadObservationState.PRESENT
    assert require_observation(result.observation).snapshot is not None
    assert require_value(require_observation(result.observation).snapshot).data == content


def test_execute_only_root_ancestors_do_not_require_directory_read_permission(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "execute-only"
    nested = root / "nested"
    nested.mkdir(parents=True)
    content = b"bounded-content"
    (nested / "leaf").write_bytes(content)
    root.chmod(0o111)
    nested.chmod(0o111)
    try:
        _, result = _read(nested, "leaf", plan, max_bytes=len(content))
    finally:
        root.chmod(0o700)
        nested.chmod(0o700)

    assert require_observation(result.observation).state is FileReadObservationState.PRESENT
    assert require_observation(result.observation).snapshot is not None
    assert require_value(require_observation(result.observation).snapshot).data == content


def test_absence_is_distinct_from_every_failure(tmp_path: Path, plan: IdentityPlan) -> None:
    _, result = _read(tmp_path, "missing", plan)
    _, missing_root = _read(tmp_path / "missing-root", "file", plan)

    assert require_observation(result.observation).state is FileReadObservationState.ABSENT
    assert require_observation(result.observation).snapshot is None
    assert require_observation(result.observation).failure is None
    assert require_observation(missing_root.observation).state is FileReadObservationState.ABSENT
    assert require_observation(missing_root.observation).snapshot is None


def test_special_object_and_oversize_refusals_disclose_no_bytes(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    (tmp_path / "directory").mkdir()
    secret = b"oversize-secret-canary"
    (tmp_path / "large").write_bytes(secret)

    _, directory = _read(tmp_path, "directory", plan)
    _, oversized = _read(tmp_path, "large", plan, max_bytes=1)

    assert require_observation(directory.observation).state is FileReadObservationState.REFUSED
    assert require_observation(directory.observation).failure is FileReadFailure.UNSUPPORTED_OBJECT
    assert require_observation(directory.observation).snapshot is None
    assert require_observation(oversized.observation).state is FileReadObservationState.REFUSED
    assert require_observation(oversized.observation).failure is FileReadFailure.LIMIT
    assert require_observation(oversized.observation).snapshot is None
    assert secret.decode() not in repr(oversized)
    assert hashlib.sha256(secret).hexdigest() not in repr(oversized)


def test_identity_mismatch_precedes_target_root_access(tmp_path: Path, plan: IdentityPlan) -> None:
    missing_root = tmp_path / "root-secret-canary"
    expected = plan.expected
    mismatched = IdentityPlan(
        IdentityExpectation(expected.euid + 100_000, expected.egid, expected.groups),
        IdentityMode.DIRECT,
    )

    _, result = _read(missing_root, "file", mismatched)

    assert require_observation(result.observation).state is FileReadObservationState.REFUSED
    assert require_observation(result.observation).failure is FileReadFailure.IDENTITY_MISMATCH
    assert require_observation(result.observation).snapshot is None
    assert str(missing_root) not in repr(result)


def test_symlinked_trusted_root_is_refused_without_path_disclosure(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "file").write_bytes(b"private")
    link = tmp_path / "root-link-canary"
    link.symlink_to(actual, target_is_directory=True)

    carrier, result = _read(link, "file", plan)

    assert require_observation(result.observation).state is FileReadObservationState.REFUSED
    assert require_observation(result.observation).failure is FileReadFailure.ROOT_REFUSED
    assert require_observation(result.observation).snapshot is None
    assert carrier.invocation is not None and str(link) not in carrier.invocation.argv
    assert str(link) not in repr(result)


def test_root_path_is_a_valid_request_and_absence_remains_complete(plan: IdentityPlan) -> None:
    _, result = _read(Path("/"), "agentworks-definitely-absent-file-read-fixture", plan)

    assert require_observation(result.observation).state is FileReadObservationState.ABSENT


def test_one_shot_read_uses_exactly_one_carrier_attempt(tmp_path: Path, plan: IdentityPlan) -> None:
    (tmp_path / "file").write_bytes(b"content")
    carrier = LocalCarrier()

    result = read_file(
        carrier,
        trusted_root_path=str(tmp_path),
        relative_path="file",
        max_bytes=1024,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )

    assert require_observation(result.observation).state is FileReadObservationState.PRESENT
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
    plan: IdentityPlan,
    root: str,
    leaf: str,
    bound: object,
) -> None:
    carrier = LocalCarrier()
    with pytest.raises(ValidationError):
        read_file(
            carrier,
            trusted_root_path=root,
            relative_path=leaf,
            max_bytes=bound,  # type: ignore[arg-type]
            plan=plan,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(sys.executable),
        )
    assert carrier.calls == 0


def test_request_and_result_representations_hide_paths_and_payload(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    secret_path = str(tmp_path / "path-canary")
    Path(secret_path).mkdir()
    carrier = LocalCarrier()
    result = read_file(
        carrier,
        trusted_root_path=secret_path,
        relative_path="leaf-canary",
        max_bytes=1,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )

    assert secret_path not in repr(carrier)
    assert "leaf-canary" not in repr(carrier)
    assert secret_path not in repr(result)
    assert "leaf-canary" not in repr(result)


def test_caller_bound_has_no_file_layer_ceiling(plan: IdentityPlan) -> None:
    carrier = LocalCarrier()
    read_file(
        carrier,
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=10**100,
        plan=plan,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )

    assert carrier.io is not None
    data = carrier.io.input.data  # type: ignore[union-attr]
    assert data.startswith(FIXED_BUNDLE.prefix)
    assert len(data) - len(FIXED_BUNDLE.prefix) < 1024


def test_request_manifest_has_an_independent_finite_bound(plan: IdentityPlan) -> None:
    carrier = LocalCarrier()
    with pytest.raises(ValidationError) as raised:
        read_file(
            carrier,
            trusted_root_path="/" + "a" * 30_000,
            relative_path="file",
            max_bytes=1,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(sys.executable),
        )

    assert carrier.calls == 0
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_invalid_utf8_path_is_not_retained_by_the_validation_exception(plan: IdentityPlan) -> None:
    carrier = LocalCarrier()
    with pytest.raises(ValidationError) as raised:
        read_file(
            carrier,
            trusted_root_path="/secret-\udcff-canary",
            relative_path="file",
            max_bytes=1,
            plan=plan,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(sys.executable),
        )

    assert carrier.calls == 0
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "canary" not in repr(raised.value)
