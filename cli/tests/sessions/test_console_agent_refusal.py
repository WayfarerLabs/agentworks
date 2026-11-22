"""A console operation that would park a refused agent's shell pane changes nothing.

Every operation that opens an agent's shell pane checks the agent's direct SSH
first (``console_access``). An agent that predates the direct-SSH rollout, whose
login is refused, stops the operation with its reinit hint before it resolves a
pane secret, writes the database, changes tmux, or parks a pane. A membership
edit on a console that is not running opens no pane, so it checks no agent.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import pytest

from agentworks.db import DesiredRuntimeState, SessionMode
from agentworks.errors import StateError
from agentworks.execution.access import Agent
from agentworks.execution.requests import Command
from agentworks.execution.results import Result, Unknown, UnknownReason
from agentworks.runtime_state import reconcile_console
from agentworks.runtime_state._models import Outcome, RuntimeResult
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolver import Resolver
from agentworks.sessions import multi_console as mc
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import Database
    from tests.conftest import _ScriptedGuest

CON = "aw-console-con"
BOT = Agent("bot", "bot-user")
REFUSE = TtyInteractionPolicy.REFUSE


def _seed(db: Database) -> None:
    """VM, the admin session ``a``, and the agent sessions ``s`` and ``t`` of agent ``bot``."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    db._conn.execute("INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')")
    for name in ("s", "t"):
        db.insert_session(
            name,
            "ws-vm1",
            "default",
            SessionMode.AGENT,
            agent_name="bot",
            socket_path=f"/run/agentworks/agent-tmux-sockets/bot-user/{name}.sock",
        )
    db._conn.commit()


def _running(model: TmuxModel) -> None:
    """The console's tmux: window ``a``, and window ``s`` with its one shell pane killed."""
    model.seed_session(CON, "a")
    model.seed_window(CON, "s")


def _create(db: Database, model: TmuxModel) -> Callable[[], None]:
    return lambda: mc.create_console(
        db, _StubConfig(), name="con", vm_name="vm1", session_specs=["a", "s+1"], interaction=REFUSE
    )


def _start(db: Database, model: TmuxModel) -> Callable[[], None]:
    return lambda: mc.start_console(db, _StubConfig(), name="con", interaction=REFUSE)


def _restart(db: Database, model: TmuxModel) -> Callable[[], None]:
    _running(model)
    return lambda: mc.restart_console(db, _StubConfig(), name="con", interaction=REFUSE)


def _reconcile(db: Database, model: TmuxModel) -> Callable[[], None]:
    return lambda: reconcile_console(db, _StubConfig(), name="con", interaction=REFUSE)  # type: ignore[arg-type]


def _restore_window(db: Database, model: TmuxModel) -> Callable[[], None]:
    model.seed_session(CON, "a")
    return lambda: mc.restore_session(db, _StubConfig(), console_name="con", session_name="s", interaction=REFUSE)


def _restore_panes(db: Database, model: TmuxModel) -> Callable[[], None]:
    _running(model)
    return lambda: mc.restore_session(db, _StubConfig(), console_name="con", session_name="s", interaction=REFUSE)


def _add_sessions(db: Database, model: TmuxModel) -> Callable[[], None]:
    _running(model)
    return lambda: mc.add_sessions(db, _StubConfig(), console_name="con", session_specs=["t+1"], interaction=REFUSE)


def _add_shell(db: Database, model: TmuxModel) -> Callable[[], None]:
    _running(model)
    return lambda: mc.add_shell(db, _StubConfig(), console_name="con", session_name="s", interaction=REFUSE)


@pytest.mark.parametrize(
    "operation",
    [_create, _start, _restart, _reconcile, _restore_window, _restore_panes, _add_sessions, _add_shell],
    ids=lambda operation: operation.__name__.lstrip("_"),
)
def test_a_refused_agent_stops_the_operation_before_it_resolves_or_changes_anything(
    db: Database,
    console_guest_factory: Callable[..., _ScriptedGuest],
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[Database, TmuxModel], Callable[[], None]],
) -> None:
    _seed(db)
    if operation is not _create:
        create_console_record(db, name="con", vm_name="vm1", session_specs=["a", "s+1"])
        # Start accepts running intent before it prepares; a console already meant
        # to run makes that a no-op, so what follows sees only the operation's own changes.
        db.set_console_desired_runtime_state("con", DesiredRuntimeState.RUNNING)
    model = TmuxModel()
    run = operation(db, model)
    guest = console_guest_factory(model)
    assert guest.recorder is not None
    agent = guest.recorder.access(BOT)
    agent.answer = lambda _call: Result(Unknown(UnknownReason.AUTH_FAILED))
    resolves: list[str] = []
    resolve = Resolver.resolve
    monkeypatch.setattr(Resolver, "resolve", lambda self: resolves.append("boundary") or resolve(self))
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: resolves.append("command") or {})
    rendered: list[RuntimeResult] = []
    monkeypatch.setattr("agentworks.runtime_state._service.render_results", lambda rows, **k: rendered.extend(rows))
    database, tmux = list(db._conn.iterdump()), copy.deepcopy(model._sessions)

    with pytest.raises(StateError) as raised:
        run()

    assert agent.requests() == [Command(("true",), deadline=30)]
    if operation is _reconcile:
        # Reconcile reports each console's refusal as its row and raises for the run.
        [row] = [row for row in rendered if row.kind == "console"]
        assert (row.name, row.outcome) == ("con", Outcome.BLOCKED)
    else:
        assert (raised.value.entity_kind, raised.value.entity_name) == ("agent", "bot")
        assert raised.value.hint
    assert resolves == []
    assert list(db._conn.iterdump()) == database
    assert model._sessions == tmux
    assert guest.parks() == []


def test_add_sessions_on_a_console_that_is_not_running_records_the_member_without_checking_its_agent(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest]
) -> None:
    """With no live tmux there is no pane to park, so the member is recorded and its agent is not asked."""
    _seed(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a", "s+1"])
    guest = console_guest_factory(TmuxModel())
    assert guest.recorder is not None
    agent = guest.recorder.access(BOT)
    agent.answer = lambda _call: Result(Unknown(UnknownReason.AUTH_FAILED))

    mc.add_sessions(db, _StubConfig(), console_name="con", session_specs=["t+1"], interaction=REFUSE)

    member = db.get_console_session("con", "t")
    assert member is not None and len(member.shells) == 1
    assert agent.requests() == []
    assert guest.parks() == []


def test_restoring_an_admin_pane_does_not_check_an_agent_whose_panes_are_all_live(
    db: Database, console_guest_factory: Callable[..., _ScriptedGuest]
) -> None:
    """Restore checks the agents of only the panes it opens, so a refused agent with nothing missing is not asked."""
    _seed(db)
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a", "s+2"])
    db.update_console_shells("con", "s", [{"cwd": "agent", "admin": False}, {"cwd": "admin", "admin": True}])
    model = TmuxModel()
    model.seed_session(CON, "a")
    model.seed_window(CON, "s", pane_tags=(None, 0))
    guest = console_guest_factory(model)
    assert guest.recorder is not None
    agent = guest.recorder.access(BOT)
    agent.answer = lambda _call: Result(Unknown(UnknownReason.AUTH_FAILED))

    mc.restore_session(db, _StubConfig(), console_name="con", session_name="s", interaction=REFUSE)

    assert agent.requests() == []
    assert [tag for _pid, _pidx, tag in model.pane_rows(CON, "s") or []] == [None, 0, 1]
