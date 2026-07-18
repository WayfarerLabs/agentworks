"""``session create`` / ``session restart`` through the orchestrated
model: the parity carries the node layer could not prove on its own.

- restart's required-commands probe fires AT PREFLIGHT, before the
  kill (matching the imperative pre-kill guard), and a missing binary
  aborts with the old session still running;
- create's ephemeral agent defers the probe at preflight and probes
  right after its realization, through the real command;
- the session's partial-state teardown runs before the ephemeral
  unwind, reproducing the imperative rollback order end to end;
- the SESSION operation scope reaches the check's readiness.

Same fake surfaces as the imperative oracle tests: SimpleNamespace
config, stubbed registry/gates/transports; the service-layer functions
are driven for real.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.db import Database, SessionMode, SessionStatus
from agentworks.errors import StateError

from ..conftest import stub_build_registry, stub_session_resolvers, stub_vm_gates

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_build_registry(monkeypatch)


class _Result:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = ""
        self.stderr = ""


class _Target:
    """Transport double: records probe events into a shared log."""

    def __init__(self, events: list[str], *, missing: set[str] | None = None) -> None:
        self._events = events
        self._missing = missing or set()

    def run(self, cmd: str, **kwargs: object) -> _Result:
        if "command -v" in cmd:
            self._events.append("probe")
            return _Result(ok=not any(f"command -v {m} " in cmd for m in self._missing))
        return _Result()


def _seed_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host, init_status) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5', 'complete')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    return db


def _requiring_template(monkeypatch: pytest.MonkeyPatch, *commands: str) -> None:
    """Override the stubbed session template with one that requires
    ``commands`` (the fork's probe needs a non-empty set to fire a real
    probe command)."""
    from agentworks.sessions import manager as session_manager

    monkeypatch.setattr(
        session_manager,
        "_resolve_template",
        lambda *a, **k: SimpleNamespace(
            name="claude",
            command="claude",
            restart_command=None,
            required_commands=list(commands),
            env={},
        ),
    )


def _patch_transports(
    monkeypatch: pytest.MonkeyPatch, admin: _Target, agent: _Target
) -> None:
    admin_factory = lambda vm, config, **kwargs: admin  # noqa: E731
    agent_factory = lambda vm, config, agent_row, **kwargs: agent  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", admin_factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", admin_factory)
    monkeypatch.setattr("agentworks.transports.agent_transport", agent_factory)


# -- restart: the pre-kill probe carry ---------------------------------------


def _restart_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing: set[str] | None = None,
) -> tuple[Database, list[str]]:
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    db = _seed_db(tmp_path)
    db.insert_agent("a1", "vm1", "agt-a1")
    db.insert_session(
        "s1", "ws1", "claude", SessionMode.AGENT,
        agent_name="a1", socket_path="/tmp/s1.sock",
    )
    db.update_session_pid("s1", 4242, boot_id="boot-x")

    events: list[str] = []
    _patch_transports(
        monkeypatch, _Target(events), _Target(events, missing=missing)
    )
    stub_vm_gates(monkeypatch)
    stub_session_resolvers(monkeypatch)
    _requiring_template(monkeypatch, "claude")

    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.OK
    )

    def _spy_kill(name: str, **kwargs: object) -> bool:
        events.append("kill")
        return True

    monkeypatch.setattr(session_manager, "_kill_session", _spy_kill)
    monkeypatch.setattr(
        tmux_mod, "deploy_restricted_config", lambda *a, **k: None
    )
    monkeypatch.setattr(
        session_manager, "_build_session_command", lambda *a, **k: "true"
    )
    monkeypatch.setattr(
        tmux_mod,
        "create_session",
        lambda *a, **k: events.append("tmux_create") or ("/tmp/s1.sock", 4243),
    )
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *a, **k: "boot-x")
    monkeypatch.setattr(
        session_manager, "_regenerate_tmuxinator", lambda *a, **k: None
    )
    return db, events


def test_restart_probe_fires_at_preflight_before_the_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-kill guard, orchestrated: the required-commands probe
    fires at PREFLIGHT, strictly before the kill, not merely once
    somewhere in the command."""
    from agentworks.sessions.manager import restart_session

    db, events = _restart_fixture(tmp_path, monkeypatch)

    restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    assert "probe" in events and "kill" in events
    assert events.index("probe") < events.index("kill"), (
        f"the probe must fire BEFORE the kill; got {events}"
    )
    assert events == ["probe", "kill", "tmux_create"]
    db.close()


def test_restart_missing_binary_aborts_with_the_old_session_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing required command aborts the restart at the sweep: no
    kill, no tmux create, the old session untouched."""
    from agentworks.sessions.manager import restart_session

    db, events = _restart_fixture(tmp_path, monkeypatch, missing={"claude"})

    with pytest.raises(StateError, match="requires 'claude'") as exc:
        restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    assert "agent 'a1'" in str(exc.value)
    assert events == ["probe"]  # no kill, no create
    refreshed = db.get_session("s1")
    assert refreshed is not None and refreshed.pid == 4242
    db.close()


# -- create: defer-then-probe through the real command -----------------------


def _create_stubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> Database:
    from agentworks.agents import manager as agent_mgr
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    db = _seed_db(tmp_path)
    _patch_transports(monkeypatch, _Target(events), _Target(events))
    stub_vm_gates(monkeypatch)
    stub_session_resolvers(monkeypatch)
    _requiring_template(monkeypatch, "claude")

    monkeypatch.setattr(agent_mgr, "_assert_agent_ssh_works", lambda *a, **k: None)
    monkeypatch.setattr(agent_mgr, "_add_to_workspace_group", lambda *a, **k: None)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *a, **k: None)
    monkeypatch.setattr(
        session_manager, "_build_session_command", lambda *a, **k: "true"
    )
    monkeypatch.setattr(
        tmux_mod,
        "create_session",
        lambda *a, **k: events.append("tmux_create") or ("/tmp/s1.sock", 4243),
    )
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *a, **k: "boot-x")
    monkeypatch.setattr(
        session_manager, "_regenerate_tmuxinator", lambda *a, **k: None
    )
    return db


def test_create_ephemeral_agent_defers_probe_until_realized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defer-then-probe fork through the real command: a pending
    agent target defers the probe at preflight; the probe fires exactly
    once, right after the agent's realization, before any session
    mutation."""
    from agentworks.sessions.manager import create_session

    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)

    def _realize_agent(db_: Any, config: Any, registry: Any, **kwargs: Any) -> None:
        events.append("realize_agent")
        db_.insert_agent(kwargs["name"], kwargs["vm"].name, f"agt-{kwargs['name']}")

    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _realize_agent)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        new_agent=True,
    )

    assert events == ["realize_agent", "probe", "tmux_create"], (
        "the probe must defer past preflight and fire once, right after "
        f"the agent realizes; got {events}"
    )
    assert db.get_session("s1") is not None
    db.close()


def test_create_existing_agent_probes_at_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A realized (existing) agent probes at PREFLIGHT: the
    earlier-failure win, before the resolve boundary and any mutation."""
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.manager import create_session

    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)
    db.insert_agent("a1", "vm1", "agt-a1")

    real_resolve = Resolver.resolve

    def _marking_resolve(self: Resolver) -> None:
        events.append("resolve")
        real_resolve(self)

    monkeypatch.setattr(Resolver, "resolve", _marking_resolve)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        agent="a1",
    )

    assert events == ["probe", "resolve", "tmux_create"], (
        f"a realized target probes pre-resolve; got {events}"
    )
    db.close()


# -- create: the session teardown's place in the unwind ----------------------


def test_create_failure_cleans_session_slice_then_unwinds_ephemerals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full rollback order end to end, reproducing the imperative
    shape: the session's partial-state cleanup (row delete, grant
    revoke, group removal) runs FIRST, then the realized ephemerals
    unwind in reverse realization order (agent before workspace)."""
    from agentworks.agents import manager as agent_mgr
    from agentworks.sessions import tmux as tmux_mod
    from agentworks.sessions.manager import create_session

    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)

    def _realize_workspace(db_: Any, config: Any, registry: Any, **kwargs: Any) -> None:
        db_._conn.execute(
            "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
            "VALUES (?, ?, '/tmp/ws', ?)",
            (kwargs["name"], kwargs["vm"].name, f"ws-{kwargs['name']}"),
        )
        db_._conn.commit()

    def _realize_agent(db_: Any, config: Any, registry: Any, **kwargs: Any) -> None:
        db_.insert_agent(kwargs["name"], kwargs["vm"].name, f"agt-{kwargs['name']}")

    monkeypatch.setattr(
        "agentworks.workspaces.realize.realize_workspace", _realize_workspace
    )
    monkeypatch.setattr("agentworks.agents.realize.realize_agent", _realize_agent)

    real_delete_session = Database.delete_session

    def _marking_delete_session(self: Database, name: str) -> None:
        events.append("delete_session_row")
        real_delete_session(self, name)

    monkeypatch.setattr(Database, "delete_session", _marking_delete_session)
    monkeypatch.setattr(
        agent_mgr,
        "_remove_from_workspace_group",
        lambda *a, **k: events.append("remove_group"),
    )
    monkeypatch.setattr(
        "agentworks.agents.manager.delete_agent",
        lambda *a, **k: events.append("delete_agent"),
    )
    monkeypatch.setattr(
        "agentworks.workspaces.manager.delete_workspace",
        lambda *a, **k: events.append("delete_workspace"),
    )

    def _explode(*a: object, **k: object) -> None:
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(tmux_mod, "create_session", _explode)

    with pytest.raises(RuntimeError, match="tmux exploded"):
        create_session(
            db,
            SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
            name="s1",
            new_workspace=True,
            new_agent=True,
            vm_name="vm1",
        )

    cleanup = [e for e in events if e not in ("probe", "realize_agent")]
    assert cleanup == [
        "delete_session_row",
        "remove_group",
        "delete_agent",
        "delete_workspace",
    ], f"session slice cleans first, then agent, then workspace; got {events}"
    assert db.get_session("s1") is None
    assert not db.has_any_grant("s1", "s1")
    db.close()


# -- the operation scope reaches the check -----------------------------------


def test_session_scope_reaches_the_required_commands_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.capabilities.base import RunContext, ScopeLevel
    from agentworks.sessions.manager import create_session
    from agentworks.sessions.nodes import RequiredCommandsCheck

    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)
    db.insert_agent("a1", "vm1", "agt-a1")

    scopes: list[object] = []
    real_preflight = RequiredCommandsCheck.preflight

    def _recording(self: RequiredCommandsCheck, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real_preflight(self, ctx)

    monkeypatch.setattr(RequiredCommandsCheck, "preflight", _recording)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        agent="a1",
    )

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.SESSION  # type: ignore[attr-defined]
    assert scope.session == "s1"  # type: ignore[attr-defined]
    assert scope.agent == "a1" and scope.admin is False  # type: ignore[attr-defined]
    assert scope.vm == "vm1" and scope.workspace == "ws1"  # type: ignore[attr-defined]
    db.close()
