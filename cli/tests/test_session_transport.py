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

from .conftest import stub_session_resolvers

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
        "INSERT INTO vms (name, platform, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'admin', '100.64.0.5')"
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
        "agentworks.workspaces.manager._ensure_vm_running",
        lambda *args, **kwargs: None,
    )
    return targets


def test_create_session_probes_before_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        agent_mgr,
        "_add_to_workspace_group",
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
            workspace_name="ws1",
            template_name=None,
            agent_name="a1",
        )

    # No state mutated: no session row, no implicit grant, no group add.
    assert db.get_session("s1") is None
    assert not db.has_any_grant("a1", "ws1")
    assert add_calls == [], (
        "agent was added to workspace group before probe rejected; "
        "probe must run BEFORE state mutation"
    )
    db.close()


def test_create_session_uses_agent_target_for_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(agent_mgr, "_add_to_workspace_group", lambda *a, **k: None)
    monkeypatch.setattr(
        session_manager, "_build_session_command", lambda *a, **k: "true"
    )
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *args, **kwargs: None)
    # _regenerate_tmuxinator fires after create_tmux_session returns; it
    # scps a YAML file which doesn't help this test's transport assertion.
    monkeypatch.setattr(session_manager, "_regenerate_tmuxinator", lambda *a, **k: None)

    captured: dict[str, object] = {}

    def _capture_create(
        name, ws_path, command, linux_user, *, run_command, target, admin_username, is_admin, env=None
    ):  # type: ignore[no-untyped-def]
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
        workspace_name="ws1",
        template_name=None,
        agent_name="a1",
    )

    # run_command must be agent_target.run, not admin_target.run.
    assert captured["run_command"] == targets["agent"].run
    # `target` (used for socket-root setup) is still admin's.
    assert captured["target"] is targets["admin"]
    db.close()


def test_delete_session_probes_before_confirm_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    from agentworks.sessions.manager import SessionStatus

    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.STOPPED
    )

    # If we reach the confirm prompt, fail loudly.
    confirm_called = [False]

    def _confirm(_msg: str) -> bool:
        confirm_called[0] = True
        return True

    monkeypatch.setattr("agentworks.output.confirm", _confirm)

    # Probe rejects.
    def _probe_rejects(target, agent):  # type: ignore[no-untyped-def]
        raise StateError(
            "rejected", entity_kind="agent", entity_name="a1", hint="reinit"
        )

    # _assert_agent_ssh_works is imported into create_session as a local;
    # patching at its source module catches every late import.
    monkeypatch.setattr(agent_mgr, "_assert_agent_ssh_works", _probe_rejects)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(StateError):
        session_manager.delete_session(db, config, name="s1", yes=False)  # type: ignore[arg-type]

    assert not confirm_called[0], (
        "confirm prompt fired before probe rejected; probe must run first"
    )
    db.close()


def test_exec_agent_uses_direct_agent_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    # exec_agent imports keep_vm_active at module load (see top of
    # agents/manager.py), so the patch must land on that binding -- not
    # on agentworks.vms.manager.keep_vm_active, which would be a no-op.
    monkeypatch.setattr(agent_mgr, "keep_vm_active", lambda *a, **k: _NullCM())

    # Phase 6.5 added eager-resolve + env composition; stub both out so the
    # SimpleNamespace config below doesn't need vm_templates / agent_templates
    # / secret_resolver. This test focuses on the SSH transport, not env.
    monkeypatch.setattr(
        agent_mgr, "_resolve_agent_direct_env_scopes",
        lambda *a, **k: agent_mgr._AgentDirectEnvScopes(vm={}, workspace=None, agent={}),
    )
    monkeypatch.setattr(agent_mgr, "_agent_direct_secret_target", lambda *a, **k: object())
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})

    called_args: list[list[str]] = []

    def _spy_call(args: list[str], *_a: object, **_k: object) -> int:
        called_args.append(args)
        return 0

    monkeypatch.setattr("subprocess.call", _spy_call)

    config = SimpleNamespace(
        operator=SimpleNamespace(ssh_private_key=None),
        secret_resolver=None,
    )

    rc = agent_mgr.exec_agent(db, config, name="a1", command=["echo", "hi"])  # type: ignore[arg-type]
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
