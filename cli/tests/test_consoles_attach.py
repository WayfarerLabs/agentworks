"""Named-console attach behavior after explicit lifecycle cutover."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import Database
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.multi_console import _attach_loop_wrapper, attach_console
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests._tmux_model import TmuxModel
from tests.console_helpers import create_console_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import _FakeTarget


def test_attach_loop_uses_exact_session_targets() -> None:
    wrapper = _attach_loop_wrapper("backend", "/tmp/backend.sock", owner=None)
    assert "tmux -S /tmp/backend.sock has-session -t '=backend'" in wrapper
    assert "tmux -S /tmp/backend.sock attach -t '=backend'" in wrapper


def test_attach_loop_runs_every_agent_server_client_as_the_owner() -> None:
    socket_path = "/run/agentworks/agent-tmux-sockets/agt-alice/backend.sock"
    wrapper = _attach_loop_wrapper("backend", socket_path, owner="agt-alice")
    clients = wrapper.count("tmux -S ")
    assert clients > 0
    assert wrapper.count(f"sudo -n /usr/sbin/runuser -u agt-alice -- tmux -S {socket_path} ") == clients
    assert f"runuser -u agt-alice -- tmux -S {socket_path} attach -t '=backend'" in wrapper


def test_console_window_for_agent_session_attaches_as_the_owner(db: Database, fake_target: _FakeTarget) -> None:
    import shlex

    from agentworks.db import SessionMode
    from agentworks.sessions.multi_console import add_sessions
    from tests.conftest import _FakeResult

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    db.insert_agent("alice", "vm1", "agt-alice")
    socket_path = "/run/agentworks/agent-tmux-sockets/agt-alice/b.sock"
    db.insert_session("b", "ws-vm1", "default", SessionMode.AGENT, agent_name="alice", socket_path=socket_path)
    create_console_record(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t '=aw-console-con'"] = _FakeResult(returncode=0)
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b"], interaction=TtyInteractionPolicy.REFUSE)

    new_window = [c for c in fake_target.commands if "new-window -t '=aw-console-con'" in c]
    assert len(new_window) == 1
    wrapper = shlex.split(new_window[0])[-1]
    clients = wrapper.count(f"tmux -S {socket_path} ")
    assert clients > 0
    assert wrapper.count(f"sudo -n /usr/sbin/runuser -u agt-alice -- tmux -S {socket_path} ") == clients


def test_attach_joins_existing_canonical_runtime_without_building(
    db: Database,
    console_target_factory: Callable[[TmuxModel], _FakeTarget],
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console_record(db, name="con", vm_name="vm1", session_specs=["alpha"])
    model = TmuxModel()
    model.seed_session("aw-console-con", "alpha")
    target = console_target_factory(model)
    interactive: list[str] = []
    target.interactive = lambda command, **kwargs: interactive.append(command) or 0  # type: ignore[attr-defined]

    assert (
        attach_console(
            db,
            _StubConfig(),
            name="con",
            allow_nesting=True,
            interaction=TtyInteractionPolicy.REFUSE,
        )
        == 0
    )
    assert interactive == ["tmux attach -t '=aw-console-con'"]
    assert not any("new-session" in command for command in target.commands)
