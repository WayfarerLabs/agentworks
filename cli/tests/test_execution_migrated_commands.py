"""What every command migrated onto ``agentworks.execution`` shares: it refuses an unsupported client first."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents import manager as agent_manager
from agentworks.agents.manager.access import _assert_agent_access_works
from agentworks.errors import ConnectivityError, StateError
from agentworks.execution.access import Admin, Agent, VMAccess
from agentworks.execution.errors import (
    ClientUnsupported,
    ExecutionIncomplete,
    RequestRefused,
    TrustMismatch,
    VMNotReady,
)
from agentworks.execution.results import (
    Completed,
    NotDelivered,
    NotDeliveredReason,
    Result,
    Unknown,
    UnknownReason,
)
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.vm_access_support import AccessRecorder

REFUSE = TtyInteractionPolicy.REFUSE


class UntouchableDatabase:
    """A database any read of which fails the test: the client must be resolved before it is touched."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the database was read ({name}) before the OpenSSH client was resolved")


def unsupported() -> None:
    raise ClientUnsupported("no supported OpenSSH client")


COMMANDS: dict[str, Callable[[Any, Any], object]] = {
    "vm exec": lambda db, cfg: vm_manager.exec_vm(db, cfg, "box", ["true"], interaction=REFUSE),
    "vm shell": lambda db, cfg: vm_manager.shell_vm(db, cfg, "box", interaction=REFUSE),
    "vm port-forward": lambda db, cfg: vm_manager.port_forward_vm(db, cfg, "box", ["8080"], interaction=REFUSE),
    "vm verify-connection": lambda db, cfg: vm_manager.verify_vm_connection(db, cfg, SimpleNamespace(), "box"),
    "agent exec": lambda db, cfg: agent_manager.exec_agent(db, cfg, name="a1", command=["true"], interaction=REFUSE),
    "agent shell": lambda db, cfg: agent_manager.shell_agent(db, cfg, name="a1", interaction=REFUSE),
}


@pytest.mark.parametrize("command", COMMANDS)
def test_an_unsupported_client_refuses_before_the_database_is_read(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.execution.resolve_ssh", unsupported)

    with pytest.raises(ClientUnsupported):
        COMMANDS[command](UntouchableDatabase(), SimpleNamespace())


def test_vm_shell_platform_does_not_need_an_openssh_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # --platform reaches the VM through the platform's own channel, which is the way in when ssh is the problem.
    monkeypatch.setattr("agentworks.execution.resolve_ssh", unsupported)

    with pytest.raises(AssertionError, match="database was read"):
        vm_manager.shell_vm(
            UntouchableDatabase(), SimpleNamespace(), "box", platform_transport=True, interaction=REFUSE
        )  # type: ignore[arg-type]


# -- The agent probe: an unreachable VM versus an agent that predates the rollout (I19) ------


AGENT = Agent("a1", "agt-a1")
AGENT_ROW = SimpleNamespace(name="a1", vm_name="box")


def probe(
    recorder: AccessRecorder,
    agent_outcome: Result | BaseException,
    admin_outcome: Result | None = None,
) -> None:
    agent_access = recorder.access(AGENT)
    admin_access = recorder.access(Admin())
    agent_access.run_outcomes = [agent_outcome]
    admin_access.run_outcomes = [] if admin_outcome is None else [admin_outcome]
    _assert_agent_access_works(agent_access, admin_access, AGENT_ROW)  # type: ignore[arg-type]


def test_an_agent_that_answers_passes_whatever_the_status(vm_access_recorder: AccessRecorder) -> None:
    for status in (0, 1):
        probe(vm_access_recorder, Result(Completed(status)))
    assert vm_access_recorder.operations == [("a1", "run"), ("a1", "run")]  # the admin was never asked


def test_the_probe_runs_a_bounded_true_as_the_agent(vm_access_recorder: AccessRecorder) -> None:
    probe(vm_access_recorder, Result(Completed(0)))

    (request,) = vm_access_recorder.requests("run")
    assert request.argv == ("true",)
    assert request.deadline is not None


def test_a_dropped_connection_with_a_healthy_admin_means_the_agent_predates_the_rollout(
    vm_access_recorder: AccessRecorder,
) -> None:
    with pytest.raises(StateError) as raised:
        probe(vm_access_recorder, Result(Unknown(UnknownReason.CONNECTION)), Result(Completed(0)))

    assert type(raised.value) is StateError  # not VMNotReady or a trust error, which are its subclasses
    assert vm_access_recorder.operations == [("a1", "run"), ("admin", "run")]


def test_a_dropped_connection_with_an_unreachable_admin_means_the_vm_is_unreachable(
    vm_access_recorder: AccessRecorder,
) -> None:
    with pytest.raises(ConnectivityError) as raised:
        probe(vm_access_recorder, Result(Unknown(UnknownReason.CONNECTION)), Result(Unknown(UnknownReason.CONNECTION)))

    assert type(raised.value) is ConnectivityError


def test_an_unreachable_vm_carries_the_access_objects_passphrase_hint(
    vm_access_recorder: AccessRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreachable = Result(Unknown(UnknownReason.CONNECTION))
    hints = []
    for passphrase_hint in (None, "load the key"):
        monkeypatch.setattr(VMAccess, "passphrase_hint", lambda self, hint=passphrase_hint: hint)
        with pytest.raises(ConnectivityError) as raised:
            probe(vm_access_recorder, unreachable, unreachable)
        hints.append(raised.value.hint)

    plain, hinted = hints
    assert plain is not None
    assert hinted is not None
    assert hinted != plain
    assert hinted.startswith(plain)


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (NotDelivered(NotDeliveredReason.PYTHON_MISSING), VMNotReady),
        (NotDelivered(NotDeliveredReason.STDLIB_MISSING), VMNotReady),
        (Unknown(UnknownReason.HOST_KEY), TrustMismatch),
        (NotDelivered(NotDeliveredReason.REFUSED, "exec ENOENT"), RequestRefused),
        (NotDelivered(NotDeliveredReason.LAUNCHER_FAILED), ExecutionIncomplete),
        (Unknown(UnknownReason.TRUNCATED), ExecutionIncomplete),
        (Unknown(UnknownReason.DEADLINE), ExecutionIncomplete),
    ],
)
def test_every_other_outcome_raises_as_it_is_without_asking_the_admin(
    vm_access_recorder: AccessRecorder, outcome: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        probe(vm_access_recorder, Result(outcome))  # type: ignore[arg-type]

    assert vm_access_recorder.operations == [("a1", "run")]


def test_the_probe_does_not_ask_the_admin_to_run_anything_but_true(vm_access_recorder: AccessRecorder) -> None:
    with pytest.raises(StateError):
        probe(vm_access_recorder, Result(Unknown(UnknownReason.CONNECTION)), Result(Completed(0)))

    assert {request.argv for request in vm_access_recorder.requests("run")} == {("true",)}
