"""The detached client over the recording fake: what each operation asks, and how it reads the answers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ExternalError, ValidationError
from agentworks.execution import detached_program
from agentworks.execution.access import Admin
from agentworks.execution.detached import Launch, Observation, State
from agentworks.execution.errors import (
    CommandFailed,
    DetachedRefused,
    ExecutionIncomplete,
    RequestRefused,
    VMNotReady,
)
from agentworks.execution.requests import Bytes, Command, NoInput, PythonProgram, Script, Shell, ToFile
from agentworks.execution.results import Completed, NotDelivered, NotDeliveredReason, Result, Unknown, UnknownReason
from tests.vm_access_support import RecordingVMAccess

if TYPE_CHECKING:
    from pathlib import Path

ARCHIVE = Command(("tar", "-cf", "archive.tar", "--", "srv"), env={"ZSTD_CLEVEL": "15"}, elevated=True)


def admin() -> RecordingVMAccess:
    return RecordingVMAccess([], Admin())


def observed(state: str, status: int | None = None, work: dict[str, int] | None = None, tail: bytes = b"") -> Result:
    line = json.dumps({"state": state, "status": status, "work": work or {}}).encode()
    return Result(Completed(0), line + b"\n" + tail)


def operations(access: RecordingVMAccess) -> list[tuple[str, ...]]:
    return [request.args for request in access.requests() if isinstance(request, PythonProgram)]


# -- launch ------------------------------------------------------------------------------


def test_launch_sends_the_spec_on_stdin_and_only_the_id_as_an_argument() -> None:
    access = admin()
    detached = access.detached()
    launch = detached.new_launch()

    detached.launch(launch, ARCHIVE)

    (request,) = access.requests()
    assert isinstance(request, PythonProgram)
    assert (request.args, request.elevated, request.deadline) == (("launch", launch.id), True, 60)
    assert isinstance(request.stdin, Bytes)
    assert json.loads(request.stdin.data) == {"argv": list(ARCHIVE.argv), "env": {"ZSTD_CLEVEL": "15"}}


def test_each_new_launch_has_a_fresh_id_on_this_vm() -> None:
    detached = admin().detached()

    first, second = detached.new_launch(), detached.new_launch()

    assert first.vm_name == second.vm_name == "box"
    assert first.id != second.id
    assert len(first.id) == 32


@pytest.mark.parametrize(
    "command",
    [
        Command(("tar",)),
        Command(("tar",), elevated=True, stdin=Bytes(b"paths")),
        Command(("tar",), elevated=True, cwd="/srv"),
        Command(("tar",), elevated=True, deadline=60),
        Script("tar", Shell.SH, elevated=True),
    ],
    ids=["not-elevated", "stdin", "cwd", "deadline", "script"],
)
def test_launch_refuses_a_request_detached_work_cannot_take_with_nothing_sent(command: Command) -> None:
    access = admin()
    detached = access.detached()

    with pytest.raises(ValidationError):
        detached.launch(detached.new_launch(), command)

    assert access.requests() == []


@pytest.mark.parametrize(
    "launch",
    [Launch("other-vm", "a" * 32), Launch("box", "A" * 32), Launch("box", "../" + "a" * 29)],
    ids=["other-vm", "uppercase", "traversal"],
)
def test_a_launch_that_new_launch_could_not_have_chosen_is_refused_by_every_operation(launch: Launch) -> None:
    access = admin()
    detached = access.detached()

    for operation in (
        lambda: detached.launch(launch, ARCHIVE),
        lambda: detached.observe(launch),
        lambda: detached.interrupt(launch),
        lambda: detached.discard(launch),
    ):
        with pytest.raises(ValidationError):
            operation()

    assert access.requests() == []


@pytest.mark.parametrize("seen", [State.RUNNING, State.EXITED, State.LOST])
def test_after_a_lost_connection_launch_returns_when_the_follow_up_sees_the_launch(seen: State) -> None:
    access = admin()
    access.run_outcomes = [
        Result(Unknown(UnknownReason.CONNECTION)),
        observed(seen, 0 if seen is State.EXITED else None),
    ]
    detached = access.detached()
    launch = detached.new_launch()

    detached.launch(launch, ARCHIVE)

    assert operations(access) == [("launch", launch.id), ("observe", launch.id)]


@pytest.mark.parametrize(
    "follow_up",
    [observed(State.ABSENT), Result(Unknown(UnknownReason.DEADLINE))],
    ids=["absent", "follow-up-incomplete"],
)
def test_after_a_lost_connection_launch_raises_naming_the_id_unless_the_launch_is_seen(follow_up: Result) -> None:
    access = admin()
    access.run_outcomes = [Result(Unknown(UnknownReason.TRUNCATED)), follow_up]
    detached = access.detached()
    launch = detached.new_launch()

    with pytest.raises(ExecutionIncomplete) as raised:
        detached.launch(launch, ARCHIVE)

    assert raised.value.result.completion == Unknown(UnknownReason.TRUNCATED)
    assert launch.id in (raised.value.hint or "")
    assert operations(access) == [("launch", launch.id), ("observe", launch.id)]


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (Result(NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)), ExecutionIncomplete),
        (Result(NotDelivered(NotDeliveredReason.PYTHON_MISSING)), VMNotReady),
        (Result(Completed(detached_program.EXIT_EXISTS)), DetachedRefused),
        (Result(Completed(127), stderr=b"exec ENOENT\n"), RequestRefused),
        (Result(Completed(126), stderr=b"exec EACCES\n"), RequestRefused),
        (Result(Completed(126), stderr=b"exec ENOENT\n"), CommandFailed),
        (Result(Completed(127), stderr=b"\x1b[31mexec ENOENT; rm -rf\n"), CommandFailed),
        (Result(Completed(1)), CommandFailed),
    ],
    ids=["not-delivered", "no-python", "exists", "not-found", "not-executable", "mismatched", "not-a-code", "failed"],
)
def test_any_other_outcome_raises_at_once_with_no_follow_up(outcome: Result, error: type[Exception]) -> None:
    access = admin()
    access.run_outcomes = [outcome]
    detached = access.detached()

    with pytest.raises(error):
        detached.launch(detached.new_launch(), ARCHIVE)

    assert len(access.requests()) == 1


# -- observe, interrupt, download, discard -----------------------------------------------


def test_observe_reads_the_state_the_work_sizes_and_the_tail() -> None:
    access = admin()
    access.default_outcome = observed(State.EXITED, 0, {"workspaces.tar.zst": 1234}, b"tar: warning\n\x00")
    detached = access.detached()
    launch = detached.new_launch()

    observation = detached.observe(launch)

    assert observation == Observation(State.EXITED, 0, {"workspaces.tar.zst": 1234}, b"tar: warning\n\x00")
    (request,) = access.requests()
    assert isinstance(request, PythonProgram)
    assert (request.args, request.elevated, request.deadline, request.stdin) == (
        ("observe", launch.id),
        True,
        30,
        NoInput(),
    )


def test_interrupt_is_one_request_that_returns_the_state_after() -> None:
    access = admin()
    access.default_outcome = observed(State.EXITED, 143)
    detached = access.detached()
    launch = detached.new_launch()

    assert detached.interrupt(launch) == Observation(State.EXITED, 143)
    assert operations(access) == [("interrupt", launch.id)]


@pytest.mark.parametrize(
    "answer",
    [
        b"not json\n",
        b'{"state": "exploded", "status": null, "work": {}}\n',
        b'{"state": "exited", "status": null, "work": {}}\n',
        b'{"state": "running", "status": 0, "work": {}}\n',
        b'{"state": "exited", "status": "0", "work": {}}\n',
        b'{"state": "running", "status": null, "work": {"a": "big"}}\n',
        b'{"state": "running", "status": null}\n',
    ],
)
def test_an_unreadable_observation_is_an_external_error(answer: bytes) -> None:
    access = admin()
    access.default_outcome = Result(Completed(0), answer)
    detached = access.detached()

    with pytest.raises(ExternalError):
        detached.observe(detached.new_launch())


def test_download_cats_one_work_file_as_root_into_the_sink(tmp_path: Path) -> None:
    access = admin()
    access.default_outcome = Result(Completed(0), b"archive bytes")
    detached = access.detached()
    launch = detached.new_launch()
    sink = ToFile(tmp_path / "archive")

    detached.download(launch, "workspaces.tar.zst", sink)

    (call,) = access.calls
    assert call.request == Command(
        ("cat", "--", f"/var/lib/agentworks/detached/{launch.id}/work/workspaces.tar.zst"),
        elevated=True,
        log_streams=False,
    )
    assert (call.options["stdout"], call.options["check"]) == (sink, True)
    assert (tmp_path / "archive").read_bytes() == b"archive bytes"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../escape", "line\nbreak"])
def test_download_refuses_a_name_that_is_not_one_file_of_the_work_directory(tmp_path: Path, name: str) -> None:
    access = admin()
    detached = access.detached()

    with pytest.raises(ValidationError):
        detached.download(detached.new_launch(), name, ToFile(tmp_path / "archive"))

    assert access.requests() == []


def test_discard_refuses_a_running_launch_and_is_done_otherwise() -> None:
    access = admin()
    access.run_outcomes = [Result(Completed(detached_program.EXIT_RUNNING)), Result(Completed(0))]
    detached = access.detached()
    launch = detached.new_launch()

    with pytest.raises(DetachedRefused):
        detached.discard(launch)
    detached.discard(launch)

    assert operations(access) == [("discard", launch.id), ("discard", launch.id)]
