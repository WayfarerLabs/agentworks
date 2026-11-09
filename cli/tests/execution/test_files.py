"""``Files``: the workstation half of file operations, over a recording access and the L lane."""

from __future__ import annotations

import hashlib
import json
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateError
from agentworks.execution import file_program
from agentworks.execution.errors import (
    CommandFailed,
    ExecutionIncomplete,
    FileConflict,
    FileContentMismatch,
    FileLockTimeout,
    PathRefusal,
    PathRefused,
    PermissionDenied,
)
from agentworks.execution.files import UPDATE_ATTEMPTS, Absent, Directory, Files, Pruned, RegularFile, guest_path
from agentworks.execution.oplog import format_record
from agentworks.execution.requests import PythonProgram
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from tests.vm_access_support import Admin, Agent, RecordingVMAccess, account_shell, local_vm_access
from tests.vm_access_support import local_files as recorded_local_files

if TYPE_CHECKING:
    from collections.abc import Callable

DIGEST = hashlib.sha256(b"current").hexdigest()


def recording(*outcomes: Result | BaseException) -> tuple[Files, RecordingVMAccess]:
    access = RecordingVMAccess([], Admin())
    access.run_outcomes = list(outcomes)
    return access.files(deadline=60), access


def operations(access: RecordingVMAccess) -> list[tuple[str, dict]]:
    requests = access.requests()
    assert all(isinstance(request, PythonProgram) and request.deadline == 60 for request in requests)
    return [(request.args[0], json.loads(request.args[1])) for request in requests]  # type: ignore[union-attr]


# -- Status mapping ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,error",
    [
        (1, CommandFailed),
        (file_program.EXIT_CONFLICT, FileConflict),
        (file_program.EXIT_UNSUITABLE, PathRefused),
        (file_program.EXIT_DENIED, PathRefused),
        (file_program.EXIT_CORRUPT_INPUT, FileContentMismatch),
        (file_program.EXIT_LOCK_TIMEOUT, FileLockTimeout),
        (128 + signal.SIGTERM, CommandFailed),
        (99, CommandFailed),
    ],
)
def test_each_failing_status_raises_its_error(status: int, error: type[Exception]) -> None:
    files, _ = recording(Result(Completed(status)))

    with pytest.raises(error):
        files.write_if("/home/a/file", b"x", expected=None, mode=0o600, parent_mode=0o700)


@pytest.mark.parametrize(
    "status,reason",
    [(file_program.EXIT_UNSUITABLE, PathRefusal.UNSUITABLE), (file_program.EXIT_DENIED, PathRefusal.DENIED)],
)
def test_a_refusal_names_the_path_and_its_reason(status: int, reason: PathRefusal) -> None:
    files, _ = recording(Result(Completed(status)))

    with pytest.raises(PathRefused) as refused:
        files.remove_if("/home/a/file", expected=DIGEST)

    assert (refused.value.path, refused.value.reason) == ("/home/a/file", reason)


def test_successful_statuses_and_their_results() -> None:
    stat_reply = json.dumps({"kind": "file", "mode": 0o644, "digest": DIGEST}).encode()
    files, access = recording(
        Result(Completed(0), b"current"),
        Result(Completed(file_program.EXIT_ABSENT)),
        Result(Completed(0), stat_reply),
        Result(Completed(0), b'{"kind": "directory", "mode": null, "digest": null}'),
        Result(Completed(0), b'{"kind": "absent", "mode": null, "digest": null}'),
        Result(Completed(0)),
        Result(Completed(file_program.EXIT_ROOT_RETAINED)),
    )

    assert files.read("/home/a/file") == b"current"
    assert files.read("/home/a/absent") is None
    assert files.stat("/home/a/file") == RegularFile(DIGEST, 0o644)
    assert files.stat("/home/a") == Directory()
    assert files.stat("/home/a/absent") == Absent()
    assert files.prune_empty_parents("/home/a/pkg/x/file", root="/home/a/pkg") is Pruned.COMPLETE
    assert files.prune_empty_parents("/home/a/pkg/x/file", root="/home/a/pkg") is Pruned.ROOT_RETAINED
    reads = [request for request in access.requests() if request.args[0] in ("read", "stat")]  # type: ignore[union-attr]
    assert all(not request.log_streams for request in reads)


@pytest.mark.parametrize("reply", [b"", b"not json", b'{"kind": "file", "mode": 420, "digest": "short"}'])
def test_an_unreadable_stat_reply_is_an_external_error(reply: bytes) -> None:
    from agentworks.errors import ExternalError

    files, _ = recording(Result(Completed(0), reply))

    with pytest.raises(ExternalError):
        files.stat("/home/a/file")


def test_an_incomplete_outcome_raises_its_transport_error() -> None:
    files, _ = recording(Result(Unknown(UnknownReason.DEADLINE)))

    with pytest.raises(ExecutionIncomplete):
        files.write_if("/home/a/file", b"x", expected=None, mode=0o600, parent_mode=0o700)


def test_the_operation_log_holds_no_content_and_no_digest_of_it() -> None:
    files, access = recording(Result(Completed(0)), Result(Completed(0)))

    files.write_if("/home/a/file", b"secret content", expected=DIGEST, mode=0o640, parent_mode=0o750, group="ws")
    files.remove_if("/home/a/file", expected=DIGEST)

    records = "".join(format_record(request, Result(Completed(0)), label="file") for request in access.requests())
    assert "/home/a/file" in records
    for secret in ("secret content", DIGEST, hashlib.sha256(b"secret content").hexdigest()):
        assert secret not in records


# -- update ------------------------------------------------------------------------------------


def _append(suffix: bytes) -> Callable[[bytes | None], bytes]:
    return lambda current: (current or b"") + suffix


def test_update_transforms_what_the_caller_observed_and_rereads_after_a_conflict() -> None:
    files, access = recording(Result(Completed(file_program.EXIT_CONFLICT)), Result(Completed(0), b"theirs"))
    seen: list[bytes | None] = []

    def transform(current: bytes | None) -> bytes:
        seen.append(current)
        return (current or b"") + b"+ours"

    assert files.update("/home/a/file", transform, observed=b"mine", mode=0o600, parent_mode=0o700) is True
    assert seen == [b"mine", b"theirs"]
    assert [operation for operation, _ in operations(access)] == ["write_if", "read", "write_if"]


def test_update_gives_up_after_its_attempts_with_a_conflict() -> None:
    conflict = Result(Completed(file_program.EXIT_CONFLICT))
    files, access = recording(conflict, Result(Completed(0), b"a"), conflict, Result(Completed(0), b"b"), conflict)

    with pytest.raises(FileConflict) as caught:
        files.update("/home/a/file", _append(b"x"), observed=None, mode=0o600, parent_mode=0o700)

    assert caught.value.operation == "update"
    writes = [operation for operation, _ in operations(access) if operation == "write_if"]
    assert len(writes) == UPDATE_ATTEMPTS


@pytest.mark.parametrize(
    "outcome,error",
    [
        (Result(Completed(file_program.EXIT_LOCK_TIMEOUT)), FileLockTimeout),
        (Result(Completed(file_program.EXIT_DENIED)), PathRefused),
        (Result(Unknown(UnknownReason.DEADLINE)), ExecutionIncomplete),
    ],
)
def test_update_retries_nothing_but_a_conflict(outcome: Result, error: type[Exception]) -> None:
    files, access = recording(outcome)

    with pytest.raises(error):
        files.update("/home/a/file", _append(b"x"), observed=b"mine", mode=0o600, parent_mode=0o700)

    assert len(access.requests()) == 1


@pytest.mark.parametrize("transform", [lambda current: None, lambda current: current])
def test_update_writes_nothing_when_the_transform_changes_nothing(transform: Callable[[bytes | None], bytes]) -> None:
    files, access = recording()

    assert files.update("/home/a/file", transform, observed=b"same", mode=0o600, parent_mode=0o700) is False
    assert access.requests() == []


# -- Refusals before dispatch ------------------------------------------------------------------


@pytest.mark.parametrize("path", ["relative", "/a//b", "/a/./b", "/a/../b", "/a/", "/a/b\n", ""])
def test_unnormalized_paths_are_refused_before_dispatch(path: str) -> None:
    files, access = recording()

    with pytest.raises(StateError):
        files.read(path)
    with pytest.raises(StateError):
        guest_path(path)
    assert access.requests() == []


def test_prune_refuses_a_root_that_is_not_a_proper_ancestor() -> None:
    files, access = recording()

    for root in ("/home/a/pkg/file", "/home/b"):
        with pytest.raises(StateError):
            files.prune_empty_parents("/home/a/pkg/file", root=root)
    assert access.requests() == []


def test_an_agent_access_refuses_an_elevated_file_operation_before_dispatch() -> None:
    from agentworks.execution.access import VMAccess

    access = VMAccess(
        vm_name="box",
        host="100.64.0.9",
        user="worker",
        identity=Agent("worker", "worker"),
        identity_file=Path("/nonexistent/key"),
    )

    with pytest.raises(PermissionDenied):
        access.files().write_if("/etc/x", b"x", expected=None, mode=0o644, parent_mode=0o755, elevated=True)


# -- Over the L lane ------------------------------------------------------------------------------


@pytest.fixture
def local_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Files:
    if sys.platform != "linux":
        pytest.skip("the L lane runs agw-launch, a Linux guest program, on this machine")
    return local_vm_access(monkeypatch, home=tmp_path, shell=account_shell(tmp_path)).files(deadline=60)


def test_operations_round_trip_on_a_real_filesystem(local_files: Files, tmp_path: Path) -> None:
    package = tmp_path / "package"
    destination = package / "nested" / "file"

    local_files.write_if(str(destination), b"first", expected=None, mode=0o640, parent_mode=0o700)
    first = hashlib.sha256(b"first").hexdigest()
    assert local_files.read(str(destination)) == b"first"
    assert local_files.stat(str(destination)) == RegularFile(first, 0o640)
    assert local_files.stat(str(destination.parent)) == Directory()
    with pytest.raises(FileConflict):
        local_files.write_if(str(destination), b"second", expected=None, mode=0o640, parent_mode=0o700)
    local_files.remove_if(str(destination), expected=first)
    assert local_files.prune_empty_parents(str(destination), root=str(package)) is Pruned.COMPLETE
    assert not package.exists()
    local_files.ensure_directory(str(package / "made"), mode=0o700)
    assert local_files.stat(str(package / "made")) == Directory()
    assert local_files.update(str(destination), _append(b"!"), observed=None, mode=0o600, parent_mode=0o700)
    assert local_files.read(str(destination)) == b"!"


def test_programs_run_on_this_machine_take_the_private_lock(
    local_files: Files, file_lock_path: Path, tmp_path: Path
) -> None:
    for name, files in (("lane", local_files), ("recorded", recorded_local_files())):
        file_lock_path.unlink(missing_ok=True)

        files.write_if(str(tmp_path / name), b"content", expected=None, mode=0o600, parent_mode=0o700)

        assert file_lock_path.is_file()


def test_a_link_is_refused_as_unsuitable(local_files: Files, tmp_path: Path) -> None:
    (tmp_path / "target").write_bytes(b"target")
    (tmp_path / "link").symlink_to(tmp_path / "target")

    with pytest.raises(PathRefused) as refused:
        local_files.read(str(tmp_path / "link"))

    assert refused.value.reason is PathRefusal.UNSUITABLE
