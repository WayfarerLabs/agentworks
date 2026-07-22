"""``session create`` / ``session restart`` through the orchestrated
model: the parity carries the node layer could not prove on its own.

- restart's required-commands probe fires AT PREFLIGHT, before the
  kill (matching the imperative pre-kill guard), and a missing binary
  aborts with the old session still running;
- create's ephemeral agent defers the probe at preflight and probes
  right after its realization, through the real command;
- the session's partial-state teardown runs before the ephemeral
  unwind, reproducing the imperative rollback order end to end;
- the SESSION operation scope reaches the held harness's readiness.

Same fake surfaces as the imperative oracle tests: SimpleNamespace
config, stubbed registry/gates/transports; the service-layer functions
are driven for real.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

# Captured at import time (before any test's autouse registry stub is
# installed): the gate-parity tests below need the REAL registry and
# the REAL env-chain resolve.
from agentworks.bootstrap import build_registry as _real_build_registry
from agentworks.db import Database, SessionMode, SessionStatus
from agentworks.errors import StateError
from agentworks.output import Role
from agentworks.secrets.orchestration import (
    resolve_for_command as _real_resolve_for_command,
)

from ..conftest import stub_build_registry, stub_session_resolvers, stub_vm_gates
from ..orchestrated_fixtures import PROXMOX_SECTION, write_operator_config

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
            harness="shell",
            harness_config={"command": "claude", "required_commands": list(commands)},
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

    from agentworks.secrets.resolver import Resolver

    real_resolve = Resolver.resolve

    def _marking_resolve(self: Resolver) -> None:
        events.append("resolve")
        real_resolve(self)

    monkeypatch.setattr(Resolver, "resolve", _marking_resolve)

    # Instrument pass 2 (the env-chain resolve) with its own marker so
    # the refusal tests can prove BOTH secret passes stay behind the
    # gates, not just pass 1 (``Resolver.resolve``). ``restart_session``
    # imports ``resolve_for_command`` function-locally from
    # ``agentworks.secrets``, so patch it there, overriding the no-op the
    # conftest ``stub_session_resolvers`` installed.
    def _marking_resolve_env(*a: object, **k: object) -> dict[str, str]:
        events.append("resolve_env")
        return {}

    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command", _marking_resolve_env
    )

    def _spy_kill(name: str, **kwargs: object) -> bool:
        events.append("kill")
        return True

    monkeypatch.setattr(session_manager, "_kill_session", _spy_kill)
    monkeypatch.setattr(
        tmux_mod, "deploy_restricted_config", lambda *a, **k: None
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
    # Literal pin of the whole order: the probe fires at preflight,
    # BEFORE both secret passes (the graph-union boundary resolve, then
    # the env-chain resolve), which precede the kill. Pinning "resolve_env"
    # here proves the pass-2 marker fires, so its ABSENCE in the refusal
    # tests below is meaningful.
    assert events == ["probe", "resolve", "resolve_env", "kill", "tmux_create"]
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
    assert events == ["probe"]  # no resolve, no kill, no create
    refreshed = db.get_session("s1")
    assert refreshed is not None and refreshed.pid == 4242
    db.close()


# -- restart: both secret passes run AFTER the refusal/confirm gates ---------


def test_restart_broken_without_force_refuses_before_the_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BROKEN session without --force is refused up front. The pass-1
    graph-union resolve must NOT run first (issue #202): a refused
    restart never prompts. Preflight (read-only) still runs."""
    from agentworks.errors import BrokenStateError
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import restart_session

    db, events = _restart_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.BROKEN
    )

    with pytest.raises(BrokenStateError):
        restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1")  # type: ignore[arg-type]

    # Preflight probed, but neither secret pass ran and nothing was
    # killed: pass 1 (graph-union boundary) and pass 2 (env chain) both
    # stayed behind the refusal.
    assert "resolve" not in events
    assert "resolve_env" not in events
    assert "kill" not in events
    db.close()


def test_restart_declined_confirm_refuses_before_the_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OK session whose "Restart?" confirm is declined is refused up
    front. The pass-1 graph-union resolve must NOT run first (issue
    #202): a declined restart never prompts for secrets it was about to
    discard."""
    from agentworks import output
    from agentworks.errors import UserAbort
    from agentworks.sessions.manager import restart_session

    db, events = _restart_fixture(tmp_path, monkeypatch)  # status OK
    monkeypatch.setattr(output, "confirm", lambda *a, **k: False)

    with pytest.raises(UserAbort):
        restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=False)  # type: ignore[arg-type]

    # Both secret passes stayed behind the declined confirm.
    assert "resolve" not in events
    assert "resolve_env" not in events
    assert "kill" not in events
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
    monkeypatch.setattr(
        "agentworks.agents.grants.add_to_workspace_group", lambda *a, **k: None
    )
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *a, **k: None)
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
        "agentworks.agents.grants.remove_from_workspace_group",
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


# -- the operation scope reaches the harness ---------------------------------


def test_session_scope_reaches_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.capabilities.base import RunContext, ScopeLevel
    from agentworks.capabilities.harness.base import Harness
    from agentworks.sessions.manager import create_session

    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)
    db.insert_agent("a1", "vm1", "agt-a1")

    scopes: list[object] = []
    real_preflight = Harness.preflight

    def _recording(self: Harness, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real_preflight(self, ctx)

    monkeypatch.setattr(Harness, "preflight", _recording)

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


# -- pane-command parity: the harness op string + relocated substitution -----
#
# The command reaching tmux is the harness's start/restart output with the
# {{session_name}} / {{workspace_name}} substitution applied at the CALL
# SITE (lifted out of the deleted _build_session_command). These pin that
# every template produces the same pane command it did before the swap.


def _template(
    monkeypatch: pytest.MonkeyPatch,
    *,
    command: str = "",
    restart_command: str | None = None,
    required_commands: list[str] | None = None,
) -> None:
    """Stub ``_resolve_template`` with a ``shell``-harness resolved
    template built from the friendly flat kwargs (the harness now owns
    the command strings; the pane command is its start/restart output)."""
    from agentworks.sessions import manager as session_manager

    config: dict[str, object] = {"command": command}
    if restart_command is not None:
        config["restart_command"] = restart_command
    if required_commands is not None:
        config["required_commands"] = required_commands
    resolved = SimpleNamespace(
        name="claude", harness="shell", harness_config=config, env={}
    )
    monkeypatch.setattr(
        session_manager, "_resolve_template", lambda *a, **k: resolved
    )


def _capture_pane_command(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, str]
) -> None:
    from agentworks.sessions import tmux as tmux_mod

    def _capture(
        name: str, ws_path: str, command: str, linux_user: str, **kwargs: object
    ) -> tuple[str, int]:
        captured["command"] = command
        return ("/tmp/s1.sock", 4243)

    monkeypatch.setattr(tmux_mod, "create_session", _capture)


def test_create_pane_command_is_the_harness_output_substituted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create: the pane command is the shell harness's start() output
    (the template's ``command``) with BOTH template vars substituted at
    the call site."""
    from agentworks.sessions.manager import create_session

    events: list[str] = []
    db = _create_stubs(tmp_path, monkeypatch, events)
    db.insert_agent("a1", "vm1", "agt-a1")
    _template(monkeypatch, command="claude {{session_name}} in {{workspace_name}}")
    captured: dict[str, str] = {}
    _capture_pane_command(monkeypatch, captured)

    create_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        agent="a1",
    )

    assert captured["command"] == "claude s1 in ws1"
    db.close()


def test_restart_pane_command_uses_restart_command_and_session_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """restart: the pane command is the harness's restart() output (the
    template's ``restart_command``, preferred over ``command``) with
    ``workspace_name`` sourced from the SESSION ROW, matching the interim
    path's restart substitution."""
    from agentworks.sessions.manager import restart_session

    db, _events = _restart_fixture(tmp_path, monkeypatch)
    _template(
        monkeypatch,
        command="claude",
        restart_command="resume {{session_name}} {{workspace_name}}",
    )
    captured: dict[str, str] = {}
    _capture_pane_command(monkeypatch, captured)

    restart_session(db, SimpleNamespace(session=SimpleNamespace(history_limit=1)), name="s1", yes=True)  # type: ignore[arg-type]

    assert captured["command"] == "resume s1 ws1"
    db.close()


# -- gate-prompt parity: the walk-away invariant, per command ----------------
#
# Mirrors the add-git-credential gate parity proof for the session
# commands: on a STOPPED proxmox VM the gate resolves its API token
# just-in-time (first backend pass), SEEDS the boundary resolver so
# the token never resolves or prompts again, and every remaining
# resolution happens in the command's own recorded passes, all before
# the walk-away point; nothing resolves after them. Real config,
# registry, resolver, and backend loop (env-var backend); the platform
# backend ops, the reachability probe, and the transports are the
# fakes.

SESSION_ENV_SECTION = """
[session_templates.default.env]
API_KEY = { secret = "api-key" }

[secrets.api-key]
description = "session runtime input"
"""


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """This suite's ``make_config`` delta from the shared fixture: the
    session env secret in the operator env, the session-template
    section baked in, and the module-wide autouse stubs un-stubbed."""
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    monkeypatch.setenv("AW_SECRET_API_KEY", "shhh")

    # The module-wide autouse fixture stubs build_registry and
    # resolve_for_command for the SimpleNamespace-config tests; these
    # tests run the real ones.
    monkeypatch.setattr("agentworks.bootstrap.build_registry", _real_build_registry)
    monkeypatch.setattr(
        "agentworks.secrets.orchestration.resolve_for_command",
        _real_resolve_for_command,
    )
    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command", _real_resolve_for_command
    )

    def _make():  # noqa: ANN202
        return write_operator_config(
            tmp_path, PROXMOX_SECTION + SESSION_ENV_SECTION
        )

    return _make


def _seed_stopped_proxmox_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'box', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()


def _stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """The VM observes as stopped: the fast path fails, the gate's
    status/start ops run (needing the API token pre-boundary), and the
    reconnect repair is a recorded no-op."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
    from agentworks.db import VMStatus
    from agentworks.vms import manager as vm_manager

    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )


def _patch_session_ops(
    monkeypatch: pytest.MonkeyPatch, events: list[str], captured_env: dict[str, str]
) -> None:
    from agentworks.sessions import console as console_mod
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    _patch_transports(monkeypatch, _Target(events), _Target(events))
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *a, **k: None)

    def _capture_create(*a: object, env: dict[str, str] | None = None, **k: object):  # noqa: ANN202
        events.append("tmux_create")
        captured_env.update(env or {})
        return ("/tmp/s1.sock", 4243)

    monkeypatch.setattr(tmux_mod, "create_session", _capture_create)
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *a, **k: "boot-x")
    monkeypatch.setattr(
        session_manager, "_regenerate_tmuxinator", lambda *a, **k: None
    )
    monkeypatch.setattr(
        console_mod, "add_session_to_console", lambda *a, **k: None
    )


def test_create_stopped_vm_gate_resolves_once_and_seeds_the_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """session create on a stopped VM: the gate's just-in-time token
    resolve is the first backend pass, the boundary pass covers only
    the remainder (the seeded token is excluded), no name resolves
    twice, and nothing resolves after the boundary (a post-boundary
    read would be a third pass). The resolved env value reaches the
    session's composed env, proving the one boundary fed the ops."""
    from agentworks.sessions.manager import create_session

    config = make_config()
    _seed_stopped_proxmox_vm(db)
    events: list[str] = []
    captured_env: dict[str, str] = {}
    _stop_the_vm(monkeypatch, events)
    _patch_session_ops(monkeypatch, events, captured_env)

    create_session(db, config, name="s1", workspace="ws1", admin=True)

    # Exactly two backend passes: the gate's (API token, pre-boundary),
    # then the boundary's (the env-chain remainder). Nothing twice,
    # nothing after.
    assert resolve_counter == [["proxmox-token"], ["api-key"]]
    assert events[:3] == ["status", "start", "tailscale"]  # the gate ran
    assert captured_env["API_KEY"] == "shhh"  # boundary values reached compose
    assert db.get_session("s1") is not None

    # Phased framing (the vm-create model): even this admin + existing-
    # workspace create, which has no ephemeral stages, reads as a plan
    # with Preflight, Resolving Secrets, and Starting Session phases.
    assert "=== Preflight ===" in captured_output.info
    assert "=== Resolving Secrets ===" in captured_output.info
    assert "=== Starting Session ===" in captured_output.info
    assert any(
        m.startswith("Checking session-template/") for m in captured_output.detail
    )

    # Nesting, not just substrings: each phase header sits at level 0, the
    # Preflight "Checking ..." lines nest one deeper (level 1), and the
    # terminal result line dedents to column 0 (Role.RESULT, level 0) even
    # though it is emitted from inside the Starting Session section.
    assert (Role.HEADER, 0, "Preflight") in captured_output.lines
    assert (Role.HEADER, 0, "Starting Session") in captured_output.lines
    assert any(
        role is Role.DETAIL and level == 1 and msg.startswith("Checking session-template/")
        for role, level, msg in captured_output.lines
    )
    assert any(
        role is Role.RESULT and level == 0 and msg.startswith("Session 's1' started")
        for role, level, msg in captured_output.lines
    )


def test_restart_stopped_vm_gate_seeds_and_env_pass_is_the_only_other(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """session restart on a stopped VM: the gate's just-in-time token
    resolve seeds the boundary, whose own pass then covers NOTHING (the
    graph union is exactly the seeded site secret, so no second backend
    pass runs at the boundary); the recorded post-confirm env-chain
    resolve is the only other pass, and nothing resolves after it."""
    from agentworks.db import SessionStatus
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions.manager import restart_session

    config = make_config()
    _seed_stopped_proxmox_vm(db)
    db.insert_session("s1", "ws1", "default", SessionMode.ADMIN)
    db.update_session_pid("s1", 4242, boot_id="boot-x")
    events: list[str] = []
    captured_env: dict[str, str] = {}
    _stop_the_vm(monkeypatch, events)
    _patch_session_ops(monkeypatch, events, captured_env)
    monkeypatch.setattr(session_manager, "_ensure_pid", lambda session, **k: session)
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.STOPPED
    )

    restart_session(db, config, name="s1", yes=True)

    # Two backend passes total: the gate's token resolve, then the
    # env chain's post-confirm pass. The boundary itself contributed no
    # pass (its union was fully seeded), no name resolves twice, and
    # nothing resolves after the env pass.
    assert resolve_counter == [["proxmox-token"], ["api-key"]]
    assert events[:3] == ["status", "start", "tailscale"]  # the gate ran
    assert captured_env["API_KEY"] == "shhh"
    assert "tmux_create" in events  # the command completed

    # Restart now reads as a structured plan mirroring create: the
    # Preflight / Resolving Secrets / Starting Session headers sit at level
    # 0, the "Restarting..." announce nests at level 1, and the terminal
    # result line dedents to column 0.
    assert (Role.HEADER, 0, "Preflight") in captured_output.lines
    assert (Role.HEADER, 0, "Resolving Secrets") in captured_output.lines
    assert (Role.HEADER, 0, "Starting Session") in captured_output.lines
    assert any(
        role is Role.BODY and level == 1 and msg.startswith("Restarting session 's1'")
        for role, level, msg in captured_output.lines
    )
    assert any(
        role is Role.RESULT and level == 0 and msg == "Session 's1' restarted"
        for role, level, msg in captured_output.lines
    )


def test_restart_broken_force_kill_warning_nests_under_starting_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: Any
) -> None:
    """A BROKEN session restarted with --force force-kills via PID; that
    'force-killing' warning is emitted from inside the Starting Session
    section, so it renders at level 1 under the header, and the terminal
    result line still dedents to column 0."""
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod
    from agentworks.sessions.manager import restart_session

    db, events = _restart_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.BROKEN
    )
    monkeypatch.setattr(tmux_mod, "force_kill_tmux_server", lambda *a, **k: True)

    restart_session(
        db,
        SimpleNamespace(session=SimpleNamespace(history_limit=1)),  # type: ignore[arg-type]
        name="s1",
        force=True,
    )

    assert (Role.HEADER, 0, "Starting Session") in captured_output.lines
    assert any(
        role is Role.WARNING and level == 1 and "force-killing via PID" in msg
        for role, level, msg in captured_output.lines
    )
    assert (Role.RESULT, 0, "Session 's1' restarted") in captured_output.lines
    db.close()
