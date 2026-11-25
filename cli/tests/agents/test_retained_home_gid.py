"""Missing DB-owned accounts reuse only free retained-home IDs, without migrating files."""

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

UID = "1001"
GID = "1003"
HOME_PROBE = Script(f"test -e {HOME} || test -L {HOME}", Shell.SH, elevated=True)
STAT = Command(("stat", "-c", "%f:%u:%g", "--", HOME), elevated=True)
GROUPADD = ("groupadd", "-g", GID, LINUX_USER)
USERADD = ("useradd", "-M", "-d", HOME, "-u", UID, "-g", GID, "-s", "/bin/bash", LINUX_USER)


def _retained_home(recorder: AccessRecorder, overrides: list[tuple[object, Result]] | None = None) -> None:
    answer = recorder.answer
    responses = [
        *(overrides or []),
        (HOME_PROBE, Result(Completed(0))),
        (STAT, Result(Completed(0), f"41e8:{UID}:{GID}\n".encode())),
    ]

    def scripted(call: Call) -> Result | BaseException | None:
        response = next((result for request, result in responses if request == call.request), None)
        if response is not None:
            return response
        if isinstance(call.request, Command):
            if call.request.argv in (("getent", "group", LINUX_USER), ("getent", "group", GID)):
                return Result(Completed(0), f"{LINUX_USER}:x:{GID}:\n".encode())
            if call.request.argv in (
                ("getent", "passwd", LINUX_USER),
                ("getent", "passwd", UID),
            ) and USERADD in _commands(recorder):
                return Result(Completed(0), f"{LINUX_USER}:x:{UID}:{GID}::{HOME}:/bin/bash\n".encode())
        return answer(call) if answer is not None else None

    recorder.answer = scripted


def _ownership_changes(recorder: AccessRecorder) -> list[tuple[str, ...]]:
    return [
        argv
        for argv in _commands(recorder)
        if argv[0] in ("chown", "chgrp") and any(arg == HOME or arg.startswith(HOME + "/") for arg in argv)
    ]


def test_recreation_uses_the_retained_numeric_ids_without_reowning_files(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=_retained_home)

    commands = _commands(recorder)
    assert GROUPADD in commands
    assert USERADD in commands
    assert commands.index(GROUPADD) < commands.index(USERADD) < commands.index(("chmod", "0750", HOME))
    assert [argv for argv in commands if argv[0] == "useradd"] == [USERADD]
    assert _ownership_changes(recorder) == []


@pytest.mark.parametrize("create_home_by_default", [False, True])
def test_recovery_binds_the_inspected_home_without_requesting_creation_under_changed_guest_defaults(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, create_home_by_default: bool
) -> None:
    account_home: str | None = None
    home_creation_requests: list[str] = []

    def configure(recorder: AccessRecorder) -> None:
        _retained_home(recorder)
        answer = recorder.answer

        def changed_defaults(call: Call) -> Result | BaseException | None:
            nonlocal account_home
            if isinstance(call.request, Command):
                argv = call.request.argv
                if argv[0] == "useradd":
                    account_home = argv[argv.index("-d") + 1] if "-d" in argv else f"/srv/logins/{LINUX_USER}"
                    if "-m" in argv or (create_home_by_default and "-M" not in argv):
                        home_creation_requests.append(account_home)
                    return Result(Completed(0))
                if argv in (("getent", "passwd", LINUX_USER), ("getent", "passwd", UID)) and account_home is not None:
                    return Result(Completed(0), f"{LINUX_USER}:x:{UID}:{GID}::{account_home}:/bin/bash\n".encode())
            return answer(call) if answer is not None else None

        recorder.answer = changed_defaults

    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    assert account_home == HOME
    assert home_creation_requests == []
    assert _ownership_changes(recorder) == []


@pytest.mark.parametrize(
    "status",
    [4, 9],
    ids=["gid-in-use", "name-in-use"],
)
def test_group_collisions_stop_before_account_or_home_changes(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder, [(Command(GROUPADD, elevated=True), Result(Completed(status)))])

    with pytest.raises(CommandFailed):
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    commands = _commands(recorders[0])
    assert [argv for argv in commands if argv[0] == "groupadd"] == [GROUPADD]
    assert all(argv[0] in ("getent", "stat", "groupadd") for argv in commands)
    assert _ownership_changes(recorders[0]) == []


@pytest.mark.parametrize("probe", [HOME_PROBE, STAT], ids=["home", "stat"])
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


@pytest.mark.parametrize(
    "entry", [b"", b"41e8:1001:garbled\n", b"41e8:garbled:1003\n", b"a1ff:1001:1003\n", b"81a4:1001:1003\n"]
)
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


def test_an_existing_account_preserves_its_shared_group_and_home_ownership(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=True, primary_group="users", shell="/bin/bash")
    assert not any(argv[0] in ("useradd", "groupadd") for argv in _commands(recorder))
    assert STAT not in recorder.requests("run")
    assert HOME_PROBE not in recorder.requests("run")
    assert _ownership_changes(recorder) == []


@pytest.mark.parametrize("key", [LINUX_USER, GID], ids=["name", "gid"])
@pytest.mark.parametrize(
    "entry", [b"ws-other:x:1003:\n", b"agt-dev:x:9999:\n", b"", b"agt-dev:x:1003:\nother:x:1003:\n"]
)
def test_inconsistent_group_identity_stops_before_useradd(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, key: str, entry: bytes
) -> None:
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder, [(Command(("getent", "group", key)), Result(Completed(0), entry))])

    with pytest.raises(StateError):
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    assert GROUPADD in _commands(recorders[0])
    assert not any(argv[0] in ("useradd", "chmod", "chown", "chgrp") for argv in _commands(recorders[0]))


@pytest.mark.parametrize("key", [LINUX_USER, UID], ids=["name", "uid"])
@pytest.mark.parametrize("field,value", [(0, "other"), (2, "9999"), (3, "9999"), (5, "/home/other"), (6, "/bin/sh")])
def test_inconsistent_account_identity_stops_before_home_or_ssh_setup(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, key: str, field: int, value: str
) -> None:
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder)
        answer = recorder.answer

        def changed_identity(call: Call) -> Result | BaseException | None:
            if call.request == Command(("getent", "passwd", key)) and USERADD in _commands(recorder):
                fields = [LINUX_USER, "x", UID, GID, "", HOME, "/bin/bash"]
                fields[field] = value
                return Result(Completed(0), (":".join(fields) + "\n").encode())
            return answer(call) if answer is not None else None

        recorder.answer = changed_identity

    with pytest.raises(StateError):
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    assert USERADD in _commands(recorders[0])
    assert not any(argv[0] in ("chmod", "chown", "chgrp") for argv in _commands(recorders[0]))
    assert all(isinstance(request, (Command, Script)) for request in recorders[0].requests("run"))


@pytest.mark.parametrize(
    "outcome,error",
    [
        (Result(Completed(4)), CommandFailed),
        (Result(Unknown(UnknownReason.CONNECTION)), ExecutionIncomplete),
    ],
)
def test_uid_collision_or_uncertain_useradd_preserves_retained_tree_and_leaves_residue_for_inspection(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, outcome: Result, error: type[Exception]
) -> None:
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder, [(Command(USERADD, elevated=True), outcome)])

    with pytest.raises(error) as caught:
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    commands = _commands(recorders[0])
    assert [argv for argv in commands if argv[0] == "groupadd"] == [GROUPADD]
    assert [argv for argv in commands if argv[0] == "useradd"] == [USERADD]
    assert not any(argv[0] in ("chmod", "chown", "chgrp", "groupdel", "userdel") for argv in commands)
    assert getattr(caught.value, "hint", None) is not None


@pytest.mark.parametrize(
    "database,key", [("group", LINUX_USER), ("group", GID), ("passwd", LINUX_USER), ("passwd", UID)]
)
@pytest.mark.parametrize(
    "completion,error",
    [
        (Completed(2), CommandFailed),
        (Unknown(UnknownReason.DEADLINE), ExecutionIncomplete),
    ],
)
def test_failed_identity_lookups_refuse_further_account_setup(
    db: Database,
    config: Any,
    monkeypatch: pytest.MonkeyPatch,
    database: str,
    key: str,
    completion: Completed | Unknown,
    error: type[Exception],
) -> None:
    recorders = []

    def configure(recorder: AccessRecorder) -> None:
        recorders.append(recorder)
        _retained_home(recorder)
        answer = recorder.answer

        def failed_lookup(call: Call) -> Result | BaseException | None:
            if call.request == Command(("getent", database, key)) and (
                database == "group" or USERADD in _commands(recorder)
            ):
                return Result(completion)
            return answer(call) if answer is not None else None

        recorder.answer = failed_lookup

    with pytest.raises(error):
        _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=configure)

    commands = _commands(recorders[0])
    if database == "group":
        assert USERADD not in commands
    assert not any(argv[0] in ("chmod", "chown", "chgrp", "groupdel", "userdel") for argv in commands)
