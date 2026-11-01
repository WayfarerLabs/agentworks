"""The access object: passive until used, ordered when used, and silent about payloads."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import client, ssh_process, trust
from agentworks.execution.access import Admin, Agent, VMAccess, vm_access
from agentworks.execution.client import SSHClient
from agentworks.execution.errors import (
    ClientUnsupported,
    CommandFailed,
    ExecutionIncomplete,
    RequestRefused,
    TrustMismatch,
    TrustMissing,
    VMNotReady,
)
from agentworks.execution.requests import Command, LoginShell, Script, Shell
from agentworks.execution.results import (
    Completed,
    NotDelivered,
    NotDeliveredReason,
    Result,
    Unknown,
    UnknownReason,
)
from tests.conftest import seed_vm_trust

SECRET = "sekrit-env-value-91"


def vm(name: str = "box", host: str | None = "100.64.0.9") -> Any:
    return SimpleNamespace(name=name, tailscale_host=host, admin_username="agentworks")


def config(key: Path) -> Any:
    return SimpleNamespace(operator=SimpleNamespace(ssh_private_key=key))


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    spawned: list[object] = []

    def forbid(*args: object, **kwargs: object) -> None:
        spawned.append(args)
        raise AssertionError("nothing may be spawned")

    monkeypatch.setattr(subprocess, "Popen", forbid)
    monkeypatch.setattr(subprocess, "run", forbid)
    monkeypatch.setattr(subprocess, "call", forbid)
    return spawned


@pytest.fixture
def supported_client(monkeypatch: pytest.MonkeyPatch) -> SSHClient:
    found = SSHClient("/usr/bin/ssh", (9, 2))
    monkeypatch.setattr(client, "resolve_ssh", lambda: found)
    return found


# -- Passive construction ----------------------------------------------------------------


def test_construction_resolves_reads_and_spawns_nothing(no_spawn: list[object], tmp_path: Path) -> None:
    missing_key = tmp_path / "no-such-key"

    for candidate in (vm(host=None), vm()):
        access = vm_access(candidate, config(missing_key), Admin())
        assert isinstance(access, VMAccess)

    vm_access(vm(), config(missing_key), Agent("worker", "agent-worker"))
    assert no_spawn == []
    assert not trust.STORE_DIR.exists()


def test_the_ssh_user_is_the_admin_or_the_agents_linux_user(
    supported_client: SSHClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seed_vm_trust("box")
    users: list[str] = []

    def record(client_: SSHClient, policy: object, destination: ssh_process.Destination, *args: object, **kw: object):
        users.append(destination.user)
        raise ClientUnsupported("stop here")

    monkeypatch.setattr(ssh_process, "build_argv", record)
    for identity in (Admin(), Agent("worker", "agent-worker")):
        with pytest.raises(ClientUnsupported):
            vm_access(vm(), config(tmp_path / "key"), identity).run(Command(("true",)))

    assert users == ["agentworks", "agent-worker"]


# -- Order: everything before the spawn raises with nothing dispatched ----------------


def test_an_unsupported_client_wins_over_an_invalid_request(
    monkeypatch: pytest.MonkeyPatch, no_spawn: list[object], tmp_path: Path
) -> None:
    def refuse() -> SSHClient:
        raise ClientUnsupported("no client")

    monkeypatch.setattr(client, "resolve_ssh", refuse)
    access = vm_access(vm(), config(tmp_path / "key"), Admin())

    with pytest.raises(ClientUnsupported):
        access.run(Command(("tool", "a\0b")))


def test_an_invalid_request_wins_over_a_missing_trust_record(
    supported_client: SSHClient, no_spawn: list[object], tmp_path: Path
) -> None:
    access = vm_access(vm("never-enrolled"), config(tmp_path / "key"), Admin())

    with pytest.raises(ValidationError):
        access.run(Command(("tool", "a\0b")))
    with pytest.raises(TrustMissing):
        access.run(Command(("tool",)))
    assert no_spawn == []


def test_a_vm_without_an_address_is_a_state_error_at_first_use(
    supported_client: SSHClient, no_spawn: list[object], tmp_path: Path
) -> None:
    seed_vm_trust("box")
    access = vm_access(vm(host=None), config(tmp_path / "key"), Admin())

    for operation in (
        lambda: access.run(Command(("true",))),
        lambda: access.attach(LoginShell()),
        lambda: access.forward([]),
    ):
        with pytest.raises(StateError):
            operation()
    assert no_spawn == []


def test_attach_builds_every_argv_before_it_spawns(
    supported_client: SSHClient, no_spawn: list[object], tmp_path: Path
) -> None:
    access = vm_access(vm("never-enrolled"), config(tmp_path / "key"), Admin())

    with pytest.raises(TrustMissing):
        access.attach(LoginShell())
    assert no_spawn == []


def test_a_login_shell_can_only_be_attached_and_an_attach_takes_no_deadline(
    supported_client: SSHClient, no_spawn: list[object], tmp_path: Path
) -> None:
    seed_vm_trust("box")
    access = vm_access(vm(), config(tmp_path / "key"), Admin())

    with pytest.raises(ValidationError):
        access.run(LoginShell())
    with pytest.raises(ValidationError):
        access.stream(LoginShell(), stdin=None, stdout=None, stderr=None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        access.attach(LoginShell(deadline=5))
    assert no_spawn == []


# -- Typed outcomes ----------------------------------------------------------------------


def result(completion: object, *, stdout: bytes = b"", log: str = "") -> Result:
    return Result(completion, stdout, b"", log)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("completion", "error"),
    [
        (NotDelivered(NotDeliveredReason.PYTHON_MISSING), VMNotReady),
        (NotDelivered(NotDeliveredReason.STDLIB_MISSING), VMNotReady),
        (NotDelivered(NotDeliveredReason.REFUSED, "cwd"), RequestRefused),
        (NotDelivered(NotDeliveredReason.LAUNCHER_FAILED), ExecutionIncomplete),
        (Unknown(UnknownReason.HOST_KEY), TrustMismatch),
        (Unknown(UnknownReason.CONNECTION), ExecutionIncomplete),
        (Unknown(UnknownReason.TRUNCATED), ExecutionIncomplete),
        (Unknown(UnknownReason.DEADLINE), ExecutionIncomplete),
        (Unknown(UnknownReason.OUTPUT_LIMIT), ExecutionIncomplete),
    ],
)
def test_each_outcome_that_is_not_a_completion_raises_its_typed_error(completion: object, error: type) -> None:
    with pytest.raises(error):
        result(completion).require_completed("box")


def test_a_completion_returns_its_status_whatever_it_is() -> None:
    assert result(Completed(255)).require_completed("box") == 255


def test_the_error_kinds_decide_who_handles_them() -> None:
    from agentworks.errors import ConfigError, ConnectivityError, ExternalError

    assert issubclass(VMNotReady, StateError)
    assert issubclass(TrustMismatch, StateError)
    assert issubclass(RequestRefused, StateError)
    assert issubclass(ClientUnsupported, ConfigError)
    assert issubclass(ExecutionIncomplete, ConnectivityError)
    assert issubclass(CommandFailed, ExternalError)


def test_a_vm_without_python_points_at_reinit() -> None:
    error = VMNotReady("box", NotDeliveredReason.PYTHON_MISSING)

    assert error.hint is not None
    assert "agw vm reinit box" in error.hint


def test_errors_show_ssh_diagnostics_but_never_the_payload() -> None:
    tail = "box: Permission denied (publickey)."
    loud = result(Unknown(UnknownReason.CONNECTION), stdout=SECRET.encode(), log=tail)

    for error in (
        ExecutionIncomplete("box", loud),
        CommandFailed("box", 3, result(Completed(3), stdout=SECRET.encode())),
        RequestRefused("box", "exec ENOENT", result(NotDelivered(NotDeliveredReason.REFUSED, "exec ENOENT"))),
        VMNotReady("box", NotDeliveredReason.STDLIB_MISSING),
        TrustMismatch("box", tail),
    ):
        for text in (str(error), repr(error), error.hint or ""):
            assert SECRET not in text
    assert tail in str(ExecutionIncomplete("box", loud))
    assert CommandFailed("box", 3, result(Completed(3))).status == 3


def test_a_result_does_not_repr_its_streams() -> None:
    assert SECRET not in repr(Result(Completed(0), SECRET.encode(), SECRET.encode()))
    assert SECRET not in repr(Script(SECRET, Shell.SH, env={"K": SECRET}))
