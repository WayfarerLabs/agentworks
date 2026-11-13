"""Backup's workspace archive on detached work, over the recording fake.

A scripted guest answers each request: the path and zstd checks, ``du``, the
detached program's operations and the download's ``cat``. Every test runs on
every CI host; the live cells (a forced disconnect, an interrupt) are in the
LLD's live evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.errors import BackupError
from agentworks.execution.access import Admin
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Bytes, Command, PythonProgram, Script
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.output import Role
from agentworks.vms import backup as vm_backup
from tests.vm_access_support import RecordingVMAccess

if TYPE_CHECKING:
    from agentworks.db import WorkspaceRow
    from tests.conftest import CapturedOutput
    from tests.vm_access_support import Call

ARCHIVE_BYTES = b"zstd archive bytes"
LOST_CONNECTION = Result(Unknown(UnknownReason.CONNECTION))
LAUNCHED = Result(Completed(0))
MAX_POLLS = 50


def observed(state: str, status: int | None = None, *, size: int = 0, tail: bytes = b"") -> Result:
    line = json.dumps({"state": state, "status": status, "work": {"workspaces.tar.zst": size}}).encode()
    return Result(Completed(0), line + b"\n" + tail)


class Guest:
    """Answers backup's requests; ``observations`` are consumed one per ``observe``, the last repeating."""

    def __init__(self, *observations: Result | BaseException, launch: Result | BaseException = LAUNCHED) -> None:
        self.observations = list(observations) or [observed("exited", 0, size=len(ARCHIVE_BYTES))]
        self.launch = launch
        self.missing: set[str] = set()
        self.interrupt: Result | BaseException = observed("exited", 143)
        self.discard = Result(Completed(0))
        self.polls = 0

    def answer(self, call: Call) -> Result | BaseException | None:
        request = call.request
        if isinstance(request, PythonProgram):
            operation = request.args[0]
            if operation == "observe":
                self.polls += 1
                if self.polls > MAX_POLLS:
                    raise AssertionError("backup kept polling an archive that had ended")
                return self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]
            if operation == "launch":
                return self.launch
            if operation == "interrupt":
                return self.interrupt
            return self.discard
        if isinstance(request, Command) and request.argv[0] == "test":
            return Result(Completed(1 if request.argv[2] in self.missing else 0))
        if isinstance(request, Command) and request.argv[0] == "du":
            return Result(Completed(0), b"4096\t/srv/a\n4096\ttotal\n")
        if isinstance(request, Command) and request.argv[0] == "cat":
            return Result(Completed(0), ARCHIVE_BYTES)
        if isinstance(request, Script):
            return Result(Completed(0))
        raise AssertionError(f"backup sent an unexpected request: {request!r}")


def workspaces(*paths: str) -> list[WorkspaceRow]:
    return [cast("WorkspaceRow", SimpleNamespace(workspace_path=path)) for path in paths]


def access_for(guest: Guest) -> RecordingVMAccess:
    access = RecordingVMAccess([], Admin())
    access.answer = guest.answer
    return access


def operations(access: RecordingVMAccess) -> list[str]:
    """Each request as a short name: the program's operation, or the command's first word."""
    names = []
    for request in access.requests():
        if isinstance(request, PythonProgram):
            names.append(request.args[0])
        elif isinstance(request, Command):
            names.append(request.argv[0])
        else:
            names.append("script")
    return names


def launch_ids(access: RecordingVMAccess) -> set[str]:
    return {request.args[1] for request in access.requests() if isinstance(request, PythonProgram)}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vm_backup, "POLL_SECONDS", 0)


def test_the_archive_runs_detached_and_downloads_with_nothing_staged(tmp_path: Path) -> None:
    access = access_for(Guest(observed("running", size=10), observed("exited", 0, size=len(ARCHIVE_BYTES))))
    local = tmp_path / "workspaces.tar.zst"

    archived, skipped = vm_backup._archive_workspaces(access, workspaces("/srv/a", "/srv/-b"), local)  # noqa: SLF001

    assert (archived, skipped) == (["/srv/a", "/srv/-b"], [])
    assert local.read_bytes() == ARCHIVE_BYTES
    assert operations(access) == ["test", "test", "script", "du", "launch", "observe", "observe", "cat", "discard"]
    assert len(launch_ids(access)) == 1
    (launch,) = [r for r in access.requests() if isinstance(r, PythonProgram) and r.args[0] == "launch"]
    assert isinstance(launch.stdin, Bytes)
    launched = json.loads(launch.stdin.data)
    # Every option precedes "--", and every member, one starting with "-" included, follows it.
    assert launched["argv"] == ["tar", "--zstd", "-cf", "workspaces.tar.zst", "-C", "/", "--", "srv/a", "srv/-b"]
    assert launched["env"] == {"ZSTD_CLEVEL": "15"}


def test_a_dropped_connection_is_reported_once_with_its_recovery_and_nothing_launches_again(
    tmp_path: Path, captured_output: CapturedOutput
) -> None:
    guest = Guest(
        observed("running", size=10),
        LOST_CONNECTION,
        LOST_CONNECTION,
        observed("exited", 0, size=len(ARCHIVE_BYTES)),
    )
    access = access_for(guest)
    local = tmp_path / "workspaces.tar.zst"

    vm_backup._archive_workspaces(access, workspaces("/srv/a"), local)  # noqa: SLF001

    assert operations(access).count("launch") == 1
    assert operations(access)[-2:] == ["cat", "discard"]
    assert local.read_bytes() == ARCHIVE_BYTES
    roles = [role for role, _level, _message in captured_output.lines]
    # The archive's only warning is the loss and its only body line the recovery.
    assert roles.count(Role.WARNING) == 1
    assert roles.count(Role.BODY) == 1


@pytest.mark.parametrize(
    "ended",
    [observed("lost"), observed("absent"), observed("exited", 2, tail=b"tar: srv/a: Cannot open\n")],
    ids=["lost", "absent", "failed"],
)
def test_an_archive_that_did_not_succeed_raises_and_is_discarded_without_a_download(
    tmp_path: Path, ended: Result
) -> None:
    access = access_for(Guest(ended))
    local = tmp_path / "workspaces.tar.zst"

    with pytest.raises(BackupError):
        vm_backup._archive_workspaces(access, workspaces("/srv/a"), local)  # noqa: SLF001

    assert operations(access).count("observe") == 1  # the ended archive is never polled again
    assert "cat" not in operations(access)
    assert operations(access)[-1] == "discard"
    assert not local.exists()


@pytest.mark.parametrize(
    ("guest", "raised", "tail"),
    [
        (Guest(launch=Result(Completed(1))), CommandFailed, ["launch", "interrupt", "discard"]),
        (Guest(launch=KeyboardInterrupt()), KeyboardInterrupt, ["launch", "interrupt", "discard"]),
        (Guest(observed("running"), KeyboardInterrupt()), KeyboardInterrupt, ["observe", "interrupt", "discard"]),
        (Guest(observed("running"), Result(Completed(1))), CommandFailed, ["observe", "interrupt", "discard"]),
    ],
    ids=["launch-failed", "ctrl-c-in-launch", "ctrl-c-in-poll", "poll-failed"],
)
def test_leaving_before_the_archive_ended_interrupts_it_then_discards_it_by_its_id(
    tmp_path: Path, guest: Guest, raised: type[BaseException], tail: list[str]
) -> None:
    access = access_for(guest)

    with pytest.raises(raised):
        vm_backup._archive_workspaces(access, workspaces("/srv/a"), tmp_path / "archive")  # noqa: SLF001

    assert operations(access)[-3:] == tail
    assert len(launch_ids(access)) == 1


def test_a_second_ctrl_c_while_stopping_the_archive_still_discards_it(tmp_path: Path) -> None:
    guest = Guest(observed("running"), KeyboardInterrupt())
    guest.interrupt = KeyboardInterrupt()
    access = access_for(guest)

    with pytest.raises(KeyboardInterrupt):
        vm_backup._archive_workspaces(access, workspaces("/srv/a"), tmp_path / "archive")  # noqa: SLF001

    assert operations(access)[-2:] == ["interrupt", "discard"]


def test_an_archive_that_ended_is_not_interrupted_when_its_download_fails(tmp_path: Path) -> None:
    guest = Guest()
    access = access_for(guest)
    access.answer = lambda call: (
        Result(Unknown(UnknownReason.CONNECTION))
        if isinstance(call.request, Command) and call.request.argv[0] == "cat"
        else guest.answer(call)
    )

    with pytest.raises(ExecutionIncomplete):
        vm_backup._archive_workspaces(access, workspaces("/srv/a"), tmp_path / "archive")  # noqa: SLF001

    assert operations(access)[-2:] == ["cat", "discard"]


def test_a_discard_that_cannot_reach_the_vm_warns_and_the_backup_stands(
    tmp_path: Path, captured_output: CapturedOutput
) -> None:
    guest = Guest()
    guest.discard = LOST_CONNECTION
    access = access_for(guest)
    local = tmp_path / "workspaces.tar.zst"

    vm_backup._archive_workspaces(access, workspaces("/srv/a"), local)  # noqa: SLF001

    assert local.read_bytes() == ARCHIVE_BYTES
    assert len(captured_output.warnings) == 1


def test_missing_paths_are_skipped_and_none_left_launches_nothing(tmp_path: Path) -> None:
    guest = Guest()
    guest.missing = {"/srv/gone"}
    access = access_for(guest)

    archived, skipped = vm_backup._archive_workspaces(  # noqa: SLF001
        access, workspaces("/srv/a", "/srv/gone"), tmp_path / "one"
    )
    assert (archived, skipped) == (["/srv/a"], ["/srv/gone"])

    only_missing = access_for(guest)
    with pytest.raises(BackupError):
        vm_backup._archive_workspaces(only_missing, workspaces("/srv/gone"), tmp_path / "two")  # noqa: SLF001
    assert operations(only_missing) == ["test"]
