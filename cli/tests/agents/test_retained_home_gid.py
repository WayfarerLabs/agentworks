"""Missing DB-owned accounts reuse only a free retained-home GID, without migrating files."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks.errors import StateError
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command, Script, Shell
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from tests.agents.test_agent_home_permissions import HOME, LINUX_USER, _commands, _run_create_on_vm
from tests.agents.test_agent_home_permissions import config as config
from tests.vm_access_support import AccessRecorder, Call

if TYPE_CHECKING:
    from agentworks.db import Database

GID = "1003"
HOME_PROBE = Script(f"test -e {HOME} || test -L {HOME}", Shell.SH, elevated=True)
STAT = Command(("stat", "-c", "%f:%g", "--", HOME), elevated=True)
GID_PROBE = Command(("getent", "group", GID))
NAME_PROBE = Command(("getent", "group", LINUX_USER))
GROUPADD = ("groupadd", "-g", GID, LINUX_USER)
USERADD = ("useradd", "-m", "-g", GID, "-s", "/bin/bash", LINUX_USER)


def _retained_home(recorder: AccessRecorder, overrides: list[tuple[object, Result]] | None = None) -> None:
    answer = recorder.answer
    responses = [
        *(overrides or []),
        (HOME_PROBE, Result(Completed(0))),
        (STAT, Result(Completed(0), f"41e8:{GID}\n".encode())),
        (GID_PROBE, Result(Completed(2))),
        (NAME_PROBE, Result(Completed(2))),
    ]

    def scripted(call: Call) -> Result | BaseException | None:
        response = next((result for request, result in responses if request == call.request), None)
        return response if response is not None else answer(call) if answer is not None else None

    recorder.answer = scripted


def _ownership_changes(recorder: AccessRecorder) -> list[tuple[str, ...]]:
    return [
        argv
        for argv in _commands(recorder)
        if argv[0] in ("chown", "chgrp") and any(arg == HOME or arg.startswith(HOME + "/") for arg in argv)
    ]


def test_recreation_uses_the_retained_numeric_gid_without_reowning_files(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=_retained_home)

    commands = _commands(recorder)
    assert GROUPADD in commands
    assert USERADD in commands
    assert commands.index(GROUPADD) < commands.index(USERADD) < commands.index(("chmod", "0750", HOME))
    assert [argv for argv in commands if argv[0] == "useradd"] == [USERADD]
    assert _ownership_changes(recorder) == []
    assert GID_PROBE in recorder.requests("run")  # NSS lookup needs no sudo.


@pytest.mark.parametrize(
    "collision",
    [
        [(GID_PROBE, Result(Completed(0), f"unrelated:x:{GID}:someone\n".encode()))],
        [(GID_PROBE, Result(Completed(0), f"{LINUX_USER}:x:{GID}:\n".encode()))],
        [(NAME_PROBE, Result(Completed(0), f"{LINUX_USER}:x:1006:\n".encode()))],
    ],
    ids=["unrelated-gid", "remaining-named-group", "name-with-other-gid"],
)
def test_collisions_stop_before_any_mutation(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, collision: list[tuple[object, Result]]
) -> None:
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder, collision)

    with pytest.raises(StateError):
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    commands = _commands(recorders[0])
    assert all(argv[0] in ("getent", "stat") for argv in commands)
    assert _ownership_changes(recorders[0]) == []


@pytest.mark.parametrize("probe", [HOME_PROBE, STAT, GID_PROBE, NAME_PROBE], ids=["home", "stat", "gid", "name"])
@pytest.mark.parametrize("failure", ["connection", "deadline", "exit"])
def test_an_unanswered_recovery_probe_never_creates_or_changes_ownership(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, probe: object, failure: str
) -> None:
    completion: Completed | Unknown
    error: type[Exception]
    if failure == "exit":
        completion = Completed(2 if probe == HOME_PROBE else 1)
        error = CommandFailed
    else:
        completion = Unknown(UnknownReason.CONNECTION if failure == "connection" else UnknownReason.DEADLINE)
        error = ExecutionIncomplete
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder, [(probe, Result(completion))])

    with pytest.raises(error):
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    assert all(argv[0] in ("getent", "stat") for argv in _commands(recorders[0]))


@pytest.mark.parametrize("entry", [b"", b"41e8:garbled\n", b"a1ff:1003\n", b"81a4:1003\n"])
def test_an_unusable_home_stat_stops_recovery(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, entry: bytes
) -> None:
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder, [(STAT, Result(Completed(0), entry))])

    with pytest.raises(StateError):
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    assert all(argv[0] in ("getent", "stat") for argv in _commands(recorders[0]))


@pytest.mark.parametrize("failed", [GROUPADD, USERADD], ids=["groupadd-race", "useradd-failed"])
def test_an_exclusive_creation_failure_never_retries_with_another_group(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, failed: tuple[str, ...]
) -> None:
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder, [(Command(failed, elevated=True), Result(Completed(9)))])

    with pytest.raises(CommandFailed) as caught:
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    commands = _commands(recorders[0])
    assert [argv for argv in commands if argv[0] == "groupadd"] == [GROUPADD]
    assert [argv for argv in commands if argv[0] == "useradd"] == ([] if failed == GROUPADD else [USERADD])
    assert not any(argv[0] in ("chown", "chgrp", "chmod", "groupdel", "userdel") for argv in commands)
    assert caught.value.hint is not None


def test_a_missing_home_keeps_ordinary_private_group_creation(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False)
    commands = _commands(recorder)
    assert ("useradd", "-m", "-U", "-s", "/bin/bash", LINUX_USER) in commands
    assert not any(argv[0] == "groupadd" for argv in commands)
    assert STAT not in recorder.requests("run")
    assert _ownership_changes(recorder) == []
    assert GID_PROBE not in recorder.requests("run")


def test_an_existing_account_preserves_its_shared_group_and_home_ownership(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=True, primary_group="users", shell="/bin/bash")
    assert not any(argv[0] in ("useradd", "groupadd") for argv in _commands(recorder))
    assert STAT not in recorder.requests("run")
    assert HOME_PROBE not in recorder.requests("run")
    assert _ownership_changes(recorder) == []
