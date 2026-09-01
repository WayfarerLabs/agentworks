"""Transport identity for single-session ops (FRD R1).

Pins the contract that ``create_session`` / ``restart_session`` /
``stop_session`` / ``delete_session`` route destructive tmux operations
on agent-mode sessions through direct agent SSH (``agent_transport``)
rather than admin+sudo, and that the pre-rollout SSH probe runs BEFORE
any state mutation in each path.

These are integration-shaped: they spin up a real ``Database``,
monkey-patch the SSH layer, and call the public service-layer functions
so that any reorder of probe / mutation / kill steps fails this file.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.secrets.policy import TtyInteractionPolicy

from .conftest import (
    empty_secret_target,
    stub_build_registry,
    stub_session_resolvers,
    stub_vm_gates,
)


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """SimpleNamespace configs don't carry publish_to; Phase 2a's
    manager-entry hoist is no-op'd via the shared helper."""
    stub_build_registry(monkeypatch)


if TYPE_CHECKING:
    pass


class _Result:
    ok = True
    returncode = 0
    stdout = ""
    stderr = ""


class _Target:
    """``Transport`` stub that records every ``run`` call against a shared log."""

    def __init__(self, label: str, log: list[tuple[str, str]]) -> None:
        self.label = label
        self.log = log

    def run(self, cmd: str, *_args: object, **_kwargs: object) -> _Result:
        self.log.append((self.label, cmd))
        return _Result()


def _seed_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    db.insert_agent("a1", "vm1", "aw-a1")
    return db


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    call_log: list[tuple[str, str]],
) -> dict[str, _Target]:
    """Patch the SSH factories so admin/agent targets are distinguishable."""
    targets = {
        "admin": _Target("admin", call_log),
        "agent": _Target("agent", call_log),
    }

    admin_factory = lambda vm, config, **kwargs: targets["admin"]  # noqa: E731
    agent_factory = lambda vm, config, agent, **kwargs: targets["agent"]  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", admin_factory)
    monkeypatch.setattr("agentworks.transports.agent_transport", agent_factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", admin_factory)
    monkeypatch.setattr(
        "agentworks.sessions.tmux.capture_tmux_server_fingerprint",
        lambda **kwargs: SimpleNamespace(pid=12345, boot_id="boot-x", start_ticks=1),
    )
    stub_vm_gates(monkeypatch)
    return targets


def _seed_dedicated_admin_session(db: Database, name: str = "s1") -> object:
    from agentworks.db import SessionMode

    socket = f"/run/agentworks/admin-tmux-sockets/admin/{name}.sock"
    db.insert_session(name, "ws1", "default", SessionMode.ADMIN, socket_path=socket)
    db.update_session_runtime(
        name,
        socket_path=socket,
        pid=4242,
        boot_id="boot-x",
        tmux_server_start_ticks=77,
    )
    session = db.get_session(name)
    assert session is not None
    return session


def test_dedicated_teardown_kills_the_fingerprinted_server_and_not_a_numeric_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    db = _seed_db(tmp_path)
    session = _seed_dedicated_admin_session(db)
    commands: list[tuple[str, str]] = []
    target = _Target("admin", commands)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: SimpleNamespace(pid=4242, boot_id="boot-x", start_ticks=77),
    )
    killed: list[str] = []
    monkeypatch.setattr(
        tmux_mod,
        "kill_server",
        lambda *, run_command, socket_path: killed.append(socket_path) or True,
    )
    monkeypatch.setattr(session_manager, "_pid_alive", lambda *args, **kwargs: False)

    session_manager._teardown_session(  # type: ignore[arg-type]
        session,
        target=target,
        target_owns_session=True,
        db=db,
        force=False,
    )

    assert killed == ["/run/agentworks/admin-tmux-sockets/admin/s1.sock"]
    assert not any("kill -" in command for _, command in commands)
    refreshed = db.get_session("s1")
    assert refreshed is not None and refreshed.pid == PID_STOPPED


def test_dedicated_teardown_refuses_a_fingerprint_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.errors import BrokenStateError
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    db = _seed_db(tmp_path)
    session = _seed_dedicated_admin_session(db)
    target = _Target("admin", [])
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: SimpleNamespace(pid=9999, boot_id="boot-x", start_ticks=77),
    )
    monkeypatch.setattr(
        tmux_mod,
        "kill_server",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("mismatched server must not be killed")),
    )

    with pytest.raises(BrokenStateError, match="identity does not match"):
        session_manager._teardown_session(  # type: ignore[arg-type]
            session,
            target=target,
            target_owns_session=True,
            db=db,
            force=False,
        )


def test_create_session_probes_before_state_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_session --agent`` must probe agent SSH BEFORE inserting any
    DB rows or adding the agent to the workspace's Linux group.

    Pre-rollout agents (whose ``~/.ssh/authorized_keys`` was never
    populated) should surface as a clean ``StateError`` from
    ``_assert_agent_ssh_works`` rather than mutating state that the
    rollback path then has to unwind.
    """
    from agentworks.agents import manager as agent_mgr
    from agentworks.errors import StateError
    from agentworks.sessions import manager as session_manager

    db = _seed_db(tmp_path)
    call_log: list[tuple[str, str]] = []
    _patch_common(monkeypatch, call_log=call_log)

    add_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "agentworks.agents.grants.add_to_workspace_group",
        lambda vm, config, db, lu, ws, **k: add_calls.append((lu, ws)),
    )

    # Probe fails: simulate pre-rollout agent.
    def _probe_rejects(target, agent):  # type: ignore[no-untyped-def]
        raise StateError(
            f"agent '{agent.name}' rejected direct SSH",
            entity_kind="agent",
            entity_name=agent.name,
            hint="run reinit",
        )

    # _assert_agent_ssh_works is imported into create_session as a local;
    # patching at its source module catches every late import.
    monkeypatch.setattr(agent_mgr, "_assert_agent_ssh_works", _probe_rejects)

    stub_session_resolvers(monkeypatch)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(StateError):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name=None,
            agent="a1",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    # No state mutated: no session row, no implicit grant, no group add.
    assert db.get_session("s1") is None
    assert not db.has_any_grant("a1", "ws1")
    assert add_calls == [], (
        "agent was added to workspace group before probe rejected; probe must run BEFORE state mutation"
    )
    db.close()


def test_create_session_uses_agent_target_for_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``run_command`` passed to ``create_tmux_session`` for an agent
    session must come from ``agent_transport``, not admin+sudo.
    """
    from agentworks.agents import manager as agent_mgr
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    db = _seed_db(tmp_path)
    call_log: list[tuple[str, str]] = []
    targets = _patch_common(monkeypatch, call_log=call_log)

    monkeypatch.setattr(agent_mgr, "_assert_agent_ssh_works", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.agents.grants.add_to_workspace_group", lambda *a, **k: None)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *args, **kwargs: None)
    # _regenerate_tmuxinator fires after create_tmux_session returns; it
    # scps a YAML file which doesn't help this test's transport assertion.
    monkeypatch.setattr(session_manager, "_regenerate_tmuxinator", lambda *a, **k: None)

    captured: dict[str, object] = {}

    def _capture_create(name, ws_path, command, linux_user, *, run_command, target, admin_username, is_admin, env=None):  # type: ignore[no-untyped-def]
        captured["run_command"] = run_command
        captured["target"] = target
        captured["env"] = env
        return ("/tmp/sock", 12345)

    monkeypatch.setattr(tmux_mod, "create_session", _capture_create)

    stub_session_resolvers(monkeypatch)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    session_manager.create_session(
        db,
        config,  # type: ignore[arg-type]
        name="s1",
        workspace="ws1",
        template_name=None,
        agent="a1",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    # run_command must be agent_target.run, not admin_target.run.
    assert captured["run_command"] == targets["agent"].run
    # `target` (used for socket-root setup) is still admin's.
    assert captured["target"] is targets["admin"]
    db.close()


def test_create_session_aborts_on_missing_required_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A template whose ``required_commands`` aren't installed on the agent must
    abort with a clear ``StateError`` BEFORE any state mutation, rather than
    leaving the operator with the cryptic downstream tmux server-access failure.
    """
    from agentworks.agents import manager as agent_mgr
    from agentworks.errors import StateError
    from agentworks.sessions import manager as session_manager

    db = _seed_db(tmp_path)

    # Agent target whose `command -v` probe reports the binary as missing.
    class _MissingCmdResult:
        def __init__(self, ok: bool) -> None:
            self.ok = ok
            self.returncode = 0 if ok else 20
            self.stdout = ""
            self.stderr = ""

    seen_probes: list[str] = []

    class _MissingCmdTarget:
        def run(self, cmd: str, *_a: object, **_k: object) -> _MissingCmdResult:
            if "command -v" in cmd:
                seen_probes.append(cmd)
                return _MissingCmdResult(ok=False)
            return _MissingCmdResult(ok=True)

    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: _MissingCmdTarget())
    monkeypatch.setattr("agentworks.sessions.manager.transport", lambda *a, **k: _MissingCmdTarget())
    monkeypatch.setattr("agentworks.transports.agent_transport", lambda *a, **k: _MissingCmdTarget())
    monkeypatch.setattr(agent_mgr, "_assert_agent_ssh_works", lambda *a, **k: None)
    stub_vm_gates(monkeypatch)

    add_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "agentworks.agents.grants.add_to_workspace_group",
        lambda vm, config, db, lu, ws, **k: add_calls.append((lu, ws)),
    )

    stub_session_resolvers(monkeypatch)
    # Template that requires `claude` (not on the agent per the stub above).
    monkeypatch.setattr(
        session_manager,
        "_resolve_template",
        lambda *a, **k: SimpleNamespace(
            name="claude",
            harness_integration="shell",
            harness_integration_config={"command": "claude", "required_commands": ["claude"]},
            env={},
        ),
    )

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(
        StateError,
        match="'shell' harness.*session-template 'claude'.*requires 'claude'.*agent 'a1'",
    ):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name="claude",
            agent="a1",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    # Fail-fast: no session row, no implicit grant, no group add.
    assert db.get_session("s1") is None
    assert not db.has_any_grant("a1", "ws1")
    assert add_calls == [], "agent added to workspace group before command preflight"

    # Pin the probe's shell flags to `-lic`: same flags `tmux._pane_command`
    # uses for the actual pane. A regression to `-lc` would silently skip
    # PATH additions hidden behind interactive-only shell config.
    assert len(seen_probes) == 1
    assert " -lic " in seen_probes[0]
    db.close()


def test_delete_session_probes_before_confirm_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-rollout agent must trip ``_assert_agent_ssh_works`` BEFORE the
    "Delete session ...?" prompt fires; otherwise the operator confirms a
    delete that immediately bails with a StateError, wasting the confirm.
    """
    from agentworks.agents import manager as agent_mgr
    from agentworks.db import SessionMode
    from agentworks.errors import StateError
    from agentworks.sessions import manager as session_manager

    db = _seed_db(tmp_path)
    db.insert_session(
        "s1",
        "ws1",
        "default",
        SessionMode.AGENT,
        agent_name="a1",
        socket_path="/tmp/sock",
    )

    call_log: list[tuple[str, str]] = []
    _patch_common(monkeypatch, call_log=call_log)

    # Make _ensure_pid + check_session_status return clean values.
    monkeypatch.setattr(
        session_manager,
        "_ensure_pid",
        lambda session, **kwargs: session,
    )
    from agentworks.db import SessionStatus

    monkeypatch.setattr(session_manager, "check_session_status", lambda *a, **k: SessionStatus.STOPPED)

    # If we reach the confirm prompt, fail loudly.
    confirm_called = [False]

    def _confirm(_msg: str) -> bool:
        confirm_called[0] = True
        return True

    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    # Probe rejects.
    def _probe_rejects(target, agent):  # type: ignore[no-untyped-def]
        raise StateError("rejected", entity_kind="agent", entity_name="a1", hint="reinit")

    # _assert_agent_ssh_works is imported into create_session as a local;
    # patching at its source module catches every late import.
    monkeypatch.setattr(agent_mgr, "_assert_agent_ssh_works", _probe_rejects)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(StateError):
        session_manager.delete_session(db, config, name="s1", yes=False, interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]

    assert not confirm_called[0], "confirm prompt fired before probe rejected; probe must run first"
    db.close()


def test_exec_agent_uses_direct_agent_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``exec_agent`` must run through a direct-agent ``Transport`` (whose
    SSH user IS the agent's Linux user) and must NOT shell out through
    ``sudo --login -u``.
    """
    from agentworks.agents import manager as agent_mgr
    from agentworks.transports import SSHTransport

    db = _seed_db(tmp_path)

    # Real SSHTransport so call_streaming is exercised end-to-end; the only
    # thing we monkey-patch is subprocess.call (which call_streaming uses
    # to passthrough stdio).
    target = SSHTransport(host="100.64.0.5", user="aw-a1", identity_file=None, proxy_jump=None)

    monkeypatch.setattr(
        "agentworks.transports.agent_transport",
        lambda vm, config, agent, **kwargs: target,
    )
    monkeypatch.setattr(agent_mgr, "_assert_agent_ssh_works", lambda *a, **k: None)
    stub_vm_gates(monkeypatch)

    # Phase 6.5 added eager-resolve + env composition; stub both out so the
    # SimpleNamespace config below doesn't need vm_templates / agent_templates
    # / secrets plumbing. This test focuses on the SSH transport, not env.
    monkeypatch.setattr(
        agent_mgr,
        "_resolve_agent_direct_env_scopes",
        lambda *a, **k: agent_mgr._AgentDirectEnvScopes(vm={}, workspace=None, agent={}),
    )
    # A real, empty SecretTarget: the orchestrated root registers the
    # env target on the operation's REAL resolver, so a bare object()
    # sentinel no longer survives the seam.
    monkeypatch.setattr(
        agent_mgr,
        "_agent_direct_secret_target",
        lambda *a, **k: empty_secret_target(),
    )
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})

    called_args: list[list[str]] = []

    def _spy_call(args: list[str], *_a: object, **_k: object) -> int:
        called_args.append(args)
        return 0

    monkeypatch.setattr("subprocess.call", _spy_call)

    config = SimpleNamespace(
        operator=SimpleNamespace(ssh_private_key=None),
    )

    rc = agent_mgr.exec_agent(db, config, name="a1", command=["echo", "hi"], interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]
    assert rc == 0

    assert called_args, "subprocess.call was not invoked"
    argv = called_args[0]
    # The SSH destination is the agent's Linux user, NOT admin@host with
    # `sudo -n su --login`.
    assert any(a == "aw-a1@100.64.0.5" for a in argv), f"argv was: {argv}"
    # No remnants of the old admin+sudo path.
    assert not any("sudo" in a for a in argv), f"unexpected sudo in argv: {argv}"
    assert not any("su --login" in a or "su -" in a for a in argv), f"unexpected su in argv: {argv}"
    # The remote command is wrapped in $SHELL -lc to source the agent's env.
    assert any("$SHELL -lc" in a for a in argv), f"missing login shell wrapper: {argv}"

    db.close()


class _NullCM:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


def test_resume_migrates_legacy_session_to_per_session_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy admin sessions (created on the VM's default tmux server,
    ``socket_path=None``) used to surface ``check_session_status`` as a
    typed ``StateError`` and force the operator into a delete-then-create
    dance. Restart now migrates them: surgical ``tmux kill-session -t
    <name>`` on the default server, then create a fresh session under
    the per-session-socket model and persist the new socket_path.

    The kill primitive is load-bearing: legacy ``session.pid`` identifies
    the SHARED default tmux server, not this session. The old BROKEN
    handler's ``force_kill_tmux_server(pid)`` would SIGKILL the whole
    server -- nuking any other tmux session on it (ad-hoc work, other
    legacy Agentworks sessions). This test pins that the legacy path
    routes through ``kill_session`` (surgical, named-session-only) and
    NOT ``force_kill_tmux_server``.
    """
    from agentworks.db import SessionMode
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    db = _seed_db(tmp_path)
    # socket_path=None is the load-bearing legacy attribute.
    db.insert_session(
        "legacy",
        "ws1",
        "default",
        SessionMode.ADMIN,
        agent_name=None,
        socket_path=None,
    )
    db.update_session_pid("legacy", 12345, boot_id="boot-x")

    call_log: list[tuple[str, str]] = []
    _patch_common(monkeypatch, call_log=call_log)

    stub_session_resolvers(monkeypatch)

    # Boot ID for the post-create db.update_session_pid call.
    monkeypatch.setattr(session_manager, "_get_boot_id", lambda *_a, **_kw: "boot-x")
    # Tmuxinator regeneration is downstream of the migration; not in scope.
    monkeypatch.setattr(session_manager, "_regenerate_tmuxinator", lambda *_a, **_kw: None)
    # _resolve_session_linux_user reads the VM/agent rows; stub to a literal.
    monkeypatch.setattr(session_manager, "_resolve_session_linux_user", lambda *_a, **_kw: "admin")
    # Restricted-config deploy fires before create; no-op for this test.
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *_a, **_kw: None)

    # Capture the surgical kill: ``tmux kill-session -t <name>`` runs
    # against the default server (socket_path=None).
    kill_calls: list[tuple[str, str | None]] = []

    def _spy_kill_session(name, *, run_command, socket_path):  # type: ignore[no-untyped-def]
        kill_calls.append((name, socket_path))
        return True

    # ``kill_session`` is imported locally inside ``_kill_session``; patch
    # the source module so the late import resolves to the spy.
    monkeypatch.setattr(tmux_mod, "kill_session", _spy_kill_session)
    existence_calls: list[tuple[str, str | None]] = []

    def _spy_session_exists(name, *, run_command, socket_path):  # type: ignore[no-untyped-def]
        existence_calls.append((name, socket_path))
        return False

    monkeypatch.setattr(tmux_mod, "session_exists", _spy_session_exists)

    # Capture create_tmux_session: returns the new socket and PID so the
    # downstream ``db.update_session_socket_path`` lands the migration.
    create_calls: list[dict[str, object]] = []

    def _capture_create(name, ws_path, command, linux_user, *, run_command, target, admin_username, is_admin, env=None):  # type: ignore[no-untyped-def]
        create_calls.append({"name": name, "is_admin": is_admin})
        return ("/run/agentworks/admin-tmux-sockets/admin/legacy.sock", 67890)

    monkeypatch.setattr(tmux_mod, "create_session", _capture_create)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: SimpleNamespace(pid=67890, boot_id="boot-x", start_ticks=2),
    )

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # Should not raise: legacy migration flows around check_session_status.
    session_manager.restart_session(db, config, name="legacy", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]

    assert kill_calls == [("legacy", None)], (
        "kill_session must be invoked with the legacy session name and "
        f"socket_path=None (default server); got {kill_calls}"
    )
    assert existence_calls == [("legacy", None)]
    assert len(create_calls) == 1, "create_tmux_session must run once"
    assert create_calls[0]["is_admin"] is True

    # Migration persisted: the row now carries a real socket_path.
    refreshed = db.get_session("legacy")
    assert refreshed is not None
    assert refreshed.socket_path == "/run/agentworks/admin-tmux-sockets/admin/legacy.sock"
    assert refreshed.pid == 67890

    db.close()


def test_restart_dead_workload_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``restart_session`` routes through the same ``tmux.create_session`` as
    create, so a workload that dies instantly on restart must surface the
    dead-workload ``StateError`` with the captured output. Pins that the
    restart path's ``except RuntimeError`` clause (the active-server remap)
    neither swallows nor remaps it: the REAL create_session runs here against
    a scripted transport whose pane_dead probe answers dead."""
    from agentworks.db import SessionMode
    from agentworks.errors import StateError
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    db = _seed_db(tmp_path)
    # Legacy shape (socket_path=None, pid set) keeps the preflight simple:
    # it skips check_session_status and goes straight to kill + create.
    db.insert_session(
        "s1",
        "ws1",
        "default",
        SessionMode.ADMIN,
        agent_name=None,
        socket_path=None,
    )
    db.update_session_pid("s1", 12345, boot_id="boot-x")

    clap_error = "error: invalid value 'on-failure' for '--ask-for-approval <APPROVAL_POLICY>'"

    class _ScriptedResult:
        def __init__(self, ok: bool = True, stdout: str = "") -> None:
            self.ok = ok
            self.returncode = 0 if ok else 1
            self.stdout = stdout
            self.stderr = ""

    class _DeadPaneTarget:
        """Admin transport whose fresh tmux session holds a dead pane."""

        def run(self, cmd: str, *_args: object, **_kwargs: object) -> _ScriptedResult:
            if cmd.startswith("test -e "):
                return _ScriptedResult(ok=False)
            if "#{pane_dead}" in cmd:
                return _ScriptedResult(ok=True, stdout="1 2\n")
            if "capture-pane" in cmd:
                return _ScriptedResult(ok=True, stdout=f"{clap_error}\n")
            return _ScriptedResult()

    target = _DeadPaneTarget()
    factory = lambda *a, **k: target  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", factory)
    monkeypatch.setattr("agentworks.transports.agent_transport", factory)
    stub_vm_gates(monkeypatch)
    stub_session_resolvers(monkeypatch)

    monkeypatch.setattr(session_manager, "_regenerate_tmuxinator", lambda *_a, **_kw: None)
    monkeypatch.setattr(session_manager, "_resolve_session_linux_user", lambda *_a, **_kw: "admin")
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(tmux_mod, "kill_session", lambda *_a, **_kw: True)
    monkeypatch.setattr(tmux_mod, "session_exists", lambda *_a, **_kw: False)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(StateError) as excinfo:
        session_manager.restart_session(db, config, name="s1", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]

    msg = str(excinfo.value)
    assert "exited immediately after launch (status 2)" in msg
    assert clap_error in msg
    # NOT remapped by the restart path's active-server RuntimeError clause.
    assert "already has an active tmux server" not in msg
    db.close()
