"""The access object: passive until used, ordered when used, and silent about payloads."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import attach as attach_module
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
from tests.ssh_fixtures import write_test_ssh_keypair

if TYPE_CHECKING:
    from collections.abc import Callable

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


def test_a_refused_handover_does_not_get_the_working_directory_hint() -> None:
    refusal = result(NotDelivered(NotDeliveredReason.REFUSED, "handover"))

    handover = RequestRefused("box", "handover", refusal).hint
    working_directory = RequestRefused("box", "cwd", refusal).hint

    assert handover is not None
    assert handover != working_directory


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


# -- The passphrase-protected key hint ---------------------------------------------------


def _key(tmp_path: Path, *, cipher: bytes) -> Path:
    key = tmp_path / f"id-{cipher.decode()}"
    write_test_ssh_keypair(key, cipher=cipher)
    return key


def _unhinted(completion: object) -> str | None:
    return ExecutionIncomplete("box", result(completion)).hint


def _operations(access: VMAccess) -> dict[str, Any]:
    return {
        "run": lambda: access.run(Command(("true",)), check=True),
        "stream": lambda: access.stream(Command(("true",)), stdin=None, stdout=None, stderr=None),  # type: ignore[arg-type]
        "attach": lambda: access.attach(LoginShell()),
    }


@pytest.fixture
def connection_fails(supported_client: SSHClient, monkeypatch: pytest.MonkeyPatch) -> Callable[[object], None]:
    """Make every operation fail as the given completion, the way the real layers do."""
    seed_vm_trust("box")
    monkeypatch.setattr(client, "identity_is_loaded", lambda ssh, identity_file: False)

    def fail_with(completion: object) -> None:
        monkeypatch.setattr(VMAccess, "_execute", lambda self, *a, **k: result(completion))
        error = ExecutionIncomplete("box", result(completion), attaching=True)

        def attach_fails(**kwargs: object) -> int:
            raise error

        monkeypatch.setattr(attach_module, "attach", attach_fails)

    return fail_with


@pytest.mark.parametrize("operation", ["run", "stream", "attach"])
def test_a_connection_failure_with_an_encrypted_key_adds_to_the_hint(
    connection_fails: Callable[[object], None], operation: str, tmp_path: Path
) -> None:
    connection_fails(Unknown(UnknownReason.CONNECTION))
    hints = {}
    for cipher in (b"none", b"aes256-ctr"):
        access = vm_access(vm(), config(_key(tmp_path, cipher=cipher)), Admin())
        with pytest.raises(ExecutionIncomplete) as caught:
            _operations(access)[operation]()
        hints[cipher] = caught.value.hint

    plain, encrypted = hints[b"none"], hints[b"aes256-ctr"]
    assert plain is not None
    assert encrypted is not None
    assert encrypted != plain
    assert encrypted.startswith(plain)


@pytest.mark.parametrize("operation", ["run", "stream"])
@pytest.mark.parametrize(
    "completion",
    [
        Unknown(UnknownReason.TRUNCATED),
        Unknown(UnknownReason.DEADLINE),
        NotDelivered(NotDeliveredReason.LAUNCHER_FAILED),
    ],
)
def test_only_a_connection_failure_gets_the_hint(
    connection_fails: Callable[[object], None], operation: str, completion: object, tmp_path: Path
) -> None:
    connection_fails(completion)
    access = vm_access(vm(), config(_key(tmp_path, cipher=b"aes256-ctr")), Admin())

    with pytest.raises(ExecutionIncomplete) as caught:
        _operations(access)[operation]()

    assert caught.value.hint == _unhinted(completion)


def test_the_key_is_read_only_after_a_failure(
    supported_client: SSHClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reads: list[str] = []
    monkeypatch.setattr("agentworks.ssh_identity.passphrase_hint", lambda path: reads.append(str(path)))
    monkeypatch.setattr(VMAccess, "_execute", lambda self, *a, **k: result(Completed(0)))
    access = vm_access(vm(), config(tmp_path / "key"), Admin())

    access.run(Command(("true",)), check=True)
    access.stream(Command(("true",)), stdin=None, stdout=None, stderr=None)  # type: ignore[arg-type]

    assert reads == []


# -- Asking the SSH agent -------------------------------------------------------------------


@pytest.fixture
def fake_ssh_add(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Put a tiny ``ssh-add`` that records its arguments and exits with a chosen status."""
    if sys.platform == "win32":
        pytest.skip("the fake ssh-add is a POSIX script")

    def install(directory: Path, status: int) -> Path:
        directory.mkdir(exist_ok=True)
        tool = directory / "ssh-add"
        calls = directory / "ssh-add-calls"
        tool.write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit {status}\n')
        tool.chmod(0o755)
        return calls

    return install


def agent_key(tmp_path: Path, *, with_public: bool = True) -> Path:
    key = tmp_path / "id-aes256-ctr"
    write_test_ssh_keypair(key, cipher=b"aes256-ctr")
    if not with_public:
        key.with_name(f"{key.name}.pub").unlink()
    return key


@pytest.mark.parametrize(("status", "loaded"), [(0, True), (1, False), (2, False)])
def test_the_agent_holds_the_key_when_ssh_add_beside_the_client_succeeds(
    fake_ssh_add, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int, loaded: bool
) -> None:
    calls = fake_ssh_add(tmp_path / "bin", status)
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    key = agent_key(tmp_path)

    assert client.identity_is_loaded(SSHClient(str(tmp_path / "bin" / "ssh"), (9, 2)), key) is loaded
    assert calls.read_text().split() == ["-T", f"{key}.pub"]


def test_ssh_add_is_found_on_path_when_none_sits_beside_the_client(
    fake_ssh_add, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_ssh_add(tmp_path / "onpath", 0)
    monkeypatch.setenv("PATH", str(tmp_path / "onpath"))

    assert client.identity_is_loaded(SSHClient(str(tmp_path / "nowhere" / "ssh"), (9, 2)), agent_key(tmp_path))


def test_the_agent_is_not_asked_without_a_public_key_or_an_ssh_add(
    fake_ssh_add, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_ssh_add(tmp_path / "bin", 0)
    ssh = SSHClient(str(tmp_path / "bin" / "ssh"), (9, 2))

    assert client.identity_is_loaded(ssh, agent_key(tmp_path, with_public=False)) is False
    assert not calls.exists()

    (tmp_path / "bin" / "ssh-add").unlink()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert client.identity_is_loaded(ssh, agent_key(tmp_path)) is False


def test_a_key_the_agent_holds_gets_no_hint_and_one_it_lacks_does(
    fake_ssh_add, supported_client: SSHClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(client, "resolve_ssh", lambda: SSHClient(str(tmp_path / "bin" / "ssh"), (9, 2)))
    key = agent_key(tmp_path)
    access = vm_access(vm(), config(key), Admin())

    fake_ssh_add(tmp_path / "bin", 0)
    assert access.passphrase_hint() is None
    fake_ssh_add(tmp_path / "bin", 1)
    assert access.passphrase_hint() is not None


def test_an_unencrypted_key_never_asks_the_agent(fake_ssh_add, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_ssh_add(tmp_path / "bin", 1)
    monkeypatch.setattr(client, "resolve_ssh", lambda: SSHClient(str(tmp_path / "bin" / "ssh"), (9, 2)))
    key = tmp_path / "id-none"
    write_test_ssh_keypair(key)

    assert vm_access(vm(), config(key), Admin()).passphrase_hint() is None
    assert not calls.exists()


# -- A consumer that leaves ------------------------------------------------------------------


def test_a_departed_consumer_without_a_status_is_reported_as_a_writer_killed_by_sigpipe(
    supported_client: SSHClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seed_vm_trust("box")
    monkeypatch.setattr(VMAccess, "_execute", lambda self, *a, **k: result(Unknown(UnknownReason.CONSUMER_LEFT)))
    access = vm_access(vm(), config(tmp_path / "key"), Admin())

    status = access.stream(Command(("true",)), stdin=None, stdout=io.BytesIO(), stderr=io.BytesIO())

    assert status == 141


# The reader leaves after the first line; the late line stays in the buffer and, unless the descriptor is
# pointed at the null device, fails again at interpreter shutdown (exit status 120 and a traceback).
LATE_OUTPUT = """
import sys, time
from agentworks.execution.access import stream_to_operator

class Access:
    def stream(self, request, *, stdin, stdout, stderr):
        stdout.write(b"first\\n"); stdout.flush()
        time.sleep(0.5)
        try:
            stdout.write(b"late\\n"); stdout.flush()
        except BrokenPipeError:
            pass
        return 141

status = stream_to_operator(Access(), None)
sys.exit(0 if status == 141 else 3)
"""


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("head") is None, reason="needs a POSIX pipeline")
def test_output_that_arrives_after_the_reader_left_does_not_fail_at_interpreter_shutdown() -> None:
    writer = subprocess.Popen(
        [sys.executable, "-I", "-c", LATE_OUTPUT],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    assert writer.stdout is not None
    reader = subprocess.Popen(["head", "-1"], stdin=writer.stdout, stdout=subprocess.PIPE)
    writer.stdout.close()

    reader.communicate(timeout=30)
    _, stderr = writer.communicate(timeout=30)

    assert writer.returncode == 0
    assert stderr == b""
