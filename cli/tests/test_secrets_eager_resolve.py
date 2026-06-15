"""Tests for Phase 6 eager-prompting at manager entry points.

Pins the operator-facing guarantee from FRD R4: every shell-opening
command resolves secrets up front, BEFORE any state mutation. If
resolution fails (e.g. non-interactive + no AW_SECRET_<NAME> in env),
the failure surfaces as ``SecretUnavailableError`` with no DB or VM
side-effects.

The tests work by patching ``resolve_for_command`` to raise; if the
manager calls it AFTER mutating state, the DB inspection at the end of
the test catches the leak.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import SecretUnavailableError

if TYPE_CHECKING:
    pass


class _NullCM:
    """No-op context manager used to stub ``keep_vm_active``."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a: object) -> None:
        return None


def _stub_target() -> object:
    class _Result:
        ok = True
        returncode = 0
        stdout = ""
        stderr = ""

    class _Target:
        def run(self, *args: object, **kwargs: object) -> _Result:
            return _Result()

    return _Target()


def _seed_basic_db(tmp_path: Path) -> Database:
    """VM + workspace seeded; no agent. Enough for an admin-mode session."""
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
    return db


def _stub_session_prep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ssh / vm probes that would otherwise need a real VM."""
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running",
        lambda *args, **kwargs: None,
    )
    factory = lambda *a, **k: _stub_target()  # noqa: E731
    monkeypatch.setattr("agentworks.ssh.admin_exec_target", factory)
    monkeypatch.setattr("agentworks.sessions.manager.admin_exec_target", factory)


# ---------------------------------------------------------------------------
# session create
# ---------------------------------------------------------------------------


def test_session_create_eager_resolve_fires_before_db_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If resolve_for_command raises SecretUnavailableError, the session
    row must NOT be inserted (state mutation must come after eager
    resolution). The error propagates unchanged."""
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    class _Tmpl:
        name = "default"
        command = ""
        restart_command = None
        env: dict[str, str] = {}

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _Tmpl())
    # _session_secret_target needs config attributes the SimpleNamespace
    # below doesn't have; stub it to a sentinel value (the orchestrator
    # treats None as "no target", but here we want resolve_for_command
    # to be called regardless).
    sentinel_target = object()
    monkeypatch.setattr(
        session_manager, "_session_secret_target", lambda *a, **k: sentinel_target
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(SecretUnavailableError, match="api-key"):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace_name="ws1",
            template_name=None,
            agent_name=None,
        )

    # State must be untouched.
    assert db.get_session("s1") is None
    db.close()


def test_session_create_calls_resolve_with_session_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_session passes a single SecretTarget (the one returned by
    ``_session_secret_target``) into resolve_for_command. Verifies the
    glue that turns a session command into a candidate set."""
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    class _Tmpl:
        name = "default"
        command = ""
        restart_command = None
        env: dict[str, str] = {}

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _Tmpl())

    sentinel_target = object()
    monkeypatch.setattr(
        session_manager, "_session_secret_target", lambda *a, **k: sentinel_target
    )

    class _Sentinel(Exception):
        """Raised from the resolve spy so we can stop the test before the
        long-running SSH-driven part of create_session runs."""

    calls: list[list[object]] = []

    def _spy(targets: list[object], config: object, **kwargs: object) -> dict[str, str]:
        calls.append(targets)
        raise _Sentinel

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _spy)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(_Sentinel):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace_name="ws1",
            template_name=None,
            agent_name=None,
        )

    assert len(calls) == 1, f"expected exactly one eager-resolve call, got {len(calls)}"
    assert calls[0] == [sentinel_target], "session target list should contain one target"
    db.close()


# ---------------------------------------------------------------------------
# session restart
# ---------------------------------------------------------------------------


def test_session_restart_broken_no_force_bails_before_eager_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BROKEN session restarted without --force must raise BrokenStateError
    BEFORE eager-resolve runs. The operator gets a clean error without
    being asked for credentials they would have discarded."""
    from agentworks.db import SessionMode, SessionStatus
    from agentworks.errors import BrokenStateError
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, pid) "
        "VALUES ('s1', 'ws1', 'default', ?, 9999)",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    monkeypatch.setattr(
        session_manager,
        "_ensure_pid",
        lambda session, **kwargs: session,
    )
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.BROKEN
    )
    monkeypatch.setattr(
        session_manager,
        "_build_session_target",
        lambda *a, **k: SimpleNamespace(run=lambda *a, **k: None),
    )

    resolve_calls: list[object] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_calls.append(args)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(BrokenStateError, match="broken"):
        session_manager.restart_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            force=False,
            yes=True,
        )

    assert resolve_calls == [], (
        "BROKEN + no --force must bail BEFORE eager-resolve so the "
        "operator isn't asked for credentials they would discard"
    )
    db.close()


def test_session_restart_eager_resolve_fires_before_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """restart_session must call resolve_for_command BEFORE _kill_session.
    A failed eager-resolve leaves the running session untouched."""
    from agentworks.db import SessionMode, SessionStatus
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, pid) "
        "VALUES ('s1', 'ws1', 'default', ?, 9999)",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    # Status probes -> OK so the restart path would try to kill.
    monkeypatch.setattr(
        session_manager,
        "_ensure_pid",
        lambda session, **kwargs: session,
    )
    monkeypatch.setattr(
        session_manager, "check_session_status", lambda *a, **k: SessionStatus.OK
    )
    monkeypatch.setattr(
        session_manager,
        "_build_session_target",
        lambda *a, **k: SimpleNamespace(run=lambda *a, **k: None),
    )

    class _Tmpl:
        name = "default"
        command = ""
        restart_command = None
        env: dict[str, str] = {}

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _Tmpl())
    monkeypatch.setattr(
        session_manager, "_session_secret_target", lambda *a, **k: object()
    )

    kill_calls: list[str] = []

    def _track_kill(name: str, **kwargs: object) -> bool:
        kill_calls.append(name)
        return True

    monkeypatch.setattr(session_manager, "_kill_session", _track_kill)

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(SecretUnavailableError, match="api-key"):
        session_manager.restart_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            yes=True,
        )

    assert kill_calls == [], "eager-resolve must precede kill; kill ran anyway"
    db.close()


# ---------------------------------------------------------------------------
# console add-shell
# ---------------------------------------------------------------------------


def test_console_add_shell_eager_resolve_fires_before_db_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_shell must call resolve_for_command BEFORE update_console_shells.
    A failed eager-resolve leaves the console's shell list unchanged."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)

    # Seed: a session + a console + a console-session membership.
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) "
        "VALUES ('c1', 's1', '[]', 0)"
    )
    db._conn.commit()

    # Stub the secret-target builder so we don't need a real Config.
    monkeypatch.setattr(
        multi_console, "_pane_secret_target", lambda *a, **k: object()
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace()

    with pytest.raises(SecretUnavailableError, match="api-key"):
        multi_console.add_shell(
            db,
            config,  # type: ignore[arg-type]
            console_name="c1",
            session_name="s1",
            cwd=None,
            admin=False,
        )

    # The shells list must still be the original empty list -- no DB write.
    cs = db.get_console_session("c1", "s1")
    assert cs is not None
    assert cs.shells == [], "eager-resolve must precede update_console_shells"
    db.close()


def test_vm_create_eager_resolve_fires_before_db_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_vm must call resolve_for_command BEFORE db.insert_vm. A
    failed eager-resolve leaves no DB row behind."""
    from agentworks.vms import manager as vm_manager

    db = Database(tmp_path / "test.db")

    # Stub the upfront resolvers so the SimpleNamespace config below
    # doesn't need their fields.
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr(
        vm_manager, "resolve_git_credential_providers", lambda *a, **k: {}
    )
    monkeypatch.setattr(vm_manager, "verify_git_credential_auth", lambda *a, **k: None)
    monkeypatch.setattr(vm_manager, "_collect_secrets", lambda *a, **k: (None, {}))
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(
        vm=SimpleNamespace(
            name="default", env={}, cpus=2, memory=4, disk=20,
            azure_vm_size="x", swap=2,
        ),
        admin=SimpleNamespace(env={}, username="admin", git_credentials=[]),
        defaults=SimpleNamespace(platform=None, vm_host=None),
        azure=None,
        proxmox=None,
        vm_templates={"default": object()},
    )

    # Patch resolve_template to return config.vm so the early template
    # _replace pass through is a no-op for this test.
    monkeypatch.setattr(
        "agentworks.vms.templates.resolve_template", lambda *a, **k: config.vm
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        vm_manager.create_vm(
            db,
            config,  # type: ignore[arg-type]
            name="vm1",
        )

    # No DB row was inserted.
    assert db.get_vm("vm1") is None
    db.close()


def test_vm_reinit_eager_resolve_fires_before_ssh_target_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reinit_vm must call resolve_for_command BEFORE building the
    Tailscale SSH target / running initialize_vm. A failed eager-resolve
    leaves the VM untouched (no SSH session opened)."""
    from agentworks.db import InitStatus, ProvisioningStatus
    from agentworks.vms import manager as vm_manager

    db = _seed_basic_db(tmp_path)
    # Mark the VM as fully provisioned + with a tailscale_host so reinit
    # proceeds past its state guards.
    db._conn.execute(
        "UPDATE vms SET provisioning_status = ?, init_status = ?, tailscale_host = ? "
        "WHERE name = 'vm1'",
        (ProvisioningStatus.COMPLETE.value, InitStatus.COMPLETE.value, "100.64.0.5"),
    )
    db._conn.commit()

    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr(
        vm_manager, "resolve_git_credential_providers", lambda *a, **k: {}
    )
    monkeypatch.setattr(vm_manager, "verify_git_credential_auth", lambda *a, **k: None)
    monkeypatch.setattr(
        vm_manager, "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())

    ssh_target_called: list[bool] = []

    def _track_ssh_target(*args: object, **kwargs: object) -> object:
        ssh_target_called.append(True)
        return SimpleNamespace(run=lambda *a, **k: None)

    monkeypatch.setattr("agentworks.ssh.admin_exec_target", _track_ssh_target)

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(
        vm=SimpleNamespace(name="default", env={}),
        admin=SimpleNamespace(env={}, git_credentials=[]),
        vm_templates={"default": object()},
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        vm_manager.reinit_vm(db, config, "vm1")  # type: ignore[arg-type]

    assert ssh_target_called == [], (
        "eager-resolve must precede admin_exec_target; no SSH session opened"
    )
    db.close()


def test_agent_create_eager_resolve_fires_before_ssh_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_agent must call resolve_for_command BEFORE the SSH-driven
    _create_agent_on_vm runs. A failed eager-resolve leaves no agent
    row inserted (the DB insert happens AFTER _create_agent_on_vm
    succeeds) and no on-VM user created."""
    from agentworks.agents import manager as agent_manager

    db = _seed_basic_db(tmp_path)

    monkeypatch.setattr(
        agent_manager, "_collect_agent_credentials", lambda *a, **k: {}
    )
    create_called: list[bool] = []
    monkeypatch.setattr(
        agent_manager,
        "_create_agent_on_vm",
        lambda *a, **k: create_called.append(True),
    )
    monkeypatch.setattr(
        agent_manager, "_agent_secret_targets", lambda *a, **k: [object(), object()]
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    class _Tmpl:
        name = "default"
        env: dict[str, str] = {}

    monkeypatch.setattr(
        "agentworks.agents.templates.resolve_template", lambda *a, **k: _Tmpl()
    )

    config = SimpleNamespace(
        agent=_Tmpl(),
        vm=SimpleNamespace(name="default", env={}),
        admin=SimpleNamespace(env={}),
        vm_templates={"default": object()},
        agent_templates={"default": _Tmpl()},
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        agent_manager.create_agent(
            db,
            config,  # type: ignore[arg-type]
            name="a1",
            vm_name="vm1",
        )

    assert create_called == [], (
        "eager-resolve must precede _create_agent_on_vm; SSH setup ran anyway"
    )
    assert db.get_agent("a1") is None
    db.close()


def test_agent_reinit_eager_resolve_fires_before_ssh_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reinit_agent must call resolve_for_command BEFORE _create_agent_on_vm.
    A failed eager-resolve leaves the existing agent row untouched and
    no SSH session opened."""
    from agentworks.agents import manager as agent_manager

    db = _seed_basic_db(tmp_path)
    db.insert_agent("a1", "vm1", "agt-a1", template="default")

    monkeypatch.setattr(
        agent_manager, "_collect_agent_credentials", lambda *a, **k: {}
    )
    create_called: list[bool] = []
    monkeypatch.setattr(
        agent_manager,
        "_create_agent_on_vm",
        lambda *a, **k: create_called.append(True),
    )
    monkeypatch.setattr(
        agent_manager, "_agent_secret_targets", lambda *a, **k: [object(), object()]
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    class _Tmpl:
        name = "default"
        env: dict[str, str] = {}

    monkeypatch.setattr(
        "agentworks.agents.templates.resolve_template", lambda *a, **k: _Tmpl()
    )

    config = SimpleNamespace(
        agent=_Tmpl(),
        vm=SimpleNamespace(name="default", env={}),
        admin=SimpleNamespace(env={}),
        vm_templates={"default": object()},
        agent_templates={"default": _Tmpl()},
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        agent_manager.reinit_agent(
            db,
            config,  # type: ignore[arg-type]
            name="a1",
        )

    assert create_called == [], (
        "eager-resolve must precede _create_agent_on_vm; SSH setup ran anyway"
    )
    db.close()


def test_vm_shell_eager_resolve_fires_before_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_vm must call resolve_for_command BEFORE opening the SSH
    session. A failed eager-resolve produces no SSH call."""
    from agentworks.vms import manager as vm_manager

    db = _seed_basic_db(tmp_path)

    monkeypatch.setattr(
        vm_manager, "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    ssh_called: list[bool] = []

    def _track_interactive(*args: object, **kwargs: object) -> int:
        ssh_called.append(True)
        return 0

    monkeypatch.setattr("agentworks.ssh.interactive", _track_interactive)

    config = SimpleNamespace(
        vm=SimpleNamespace(env={}),
        admin=SimpleNamespace(env={}),
        secret_resolver=None,
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        vm_manager.shell_vm(db, config, "vm1")  # type: ignore[arg-type]

    assert ssh_called == [], "eager-resolve must precede the SSH session"
    db.close()


def test_vm_exec_eager_resolve_fires_before_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exec_vm must call resolve_for_command BEFORE running the remote
    command. A failed eager-resolve raises before call_streaming runs."""
    from agentworks.vms import manager as vm_manager

    db = _seed_basic_db(tmp_path)

    monkeypatch.setattr(
        vm_manager, "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    streaming_calls: list[str] = []

    class _Target:
        def call_streaming(self, cmd: str, *, env: object = None) -> int:
            streaming_calls.append(cmd)
            return 0

    monkeypatch.setattr(
        "agentworks.ssh.admin_exec_target", lambda *a, **k: _Target()
    )

    config = SimpleNamespace(
        vm=SimpleNamespace(env={}),
        admin=SimpleNamespace(env={}),
        secret_resolver=None,
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        vm_manager.exec_vm(db, config, "vm1", ["echo", "hi"])  # type: ignore[arg-type]

    assert streaming_calls == [], "eager-resolve must precede call_streaming"
    db.close()


def test_agent_exec_eager_resolve_fires_before_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exec_agent must call resolve_for_command BEFORE running the
    remote command. A failed eager-resolve raises before call_streaming."""
    from agentworks.agents import manager as agent_manager

    db = _seed_basic_db(tmp_path)
    db.insert_agent("a1", "vm1", "agt-a1", template="default")

    monkeypatch.setattr(
        agent_manager, "_resolve_agent_direct_env_scopes",
        lambda *a, **k: agent_manager._AgentDirectEnvScopes(vm={}, workspace=None, agent={}),
    )
    monkeypatch.setattr(
        agent_manager, "_agent_direct_secret_target", lambda *a, **k: object()
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    streaming_calls: list[str] = []

    class _Target:
        def call_streaming(self, cmd: str, *, env: object = None) -> int:
            streaming_calls.append(cmd)
            return 0

    monkeypatch.setattr(
        "agentworks.ssh.agent_exec_target", lambda *a, **k: _Target()
    )

    config = SimpleNamespace()

    with pytest.raises(SecretUnavailableError, match="api-key"):
        agent_manager.exec_agent(
            db, config, name="a1", command=["echo", "hi"],  # type: ignore[arg-type]
        )

    assert streaming_calls == [], "eager-resolve must precede call_streaming"
    db.close()


def test_shell_agent_passes_workspace_scope_to_secret_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_agent --workspace must include workspace-template env in
    the SecretTarget so workspace-scope secrets get eager-resolved.
    Regression test for the Phase 6.5 review's BLOCKING bug: workspace
    scope was silently dropped from agent shell --workspace."""
    from agentworks.agents import manager as agent_manager

    db = _seed_basic_db(tmp_path)
    db.insert_agent("a1", "vm1", "agt-a1", template="default")
    # Grant the agent access so the authz check passes.
    db.insert_agent_grant("a1", "ws1", "explicit")

    captured_scopes: dict[str, object] = {}

    def _spy_scopes(
        config: object, vm: object, agent: object, *, ws: object = None,
    ) -> object:
        # Record the ws arg so the test can pin "shell_agent passes the
        # workspace row through to the scope resolver."
        captured_scopes["ws"] = ws
        return agent_manager._AgentDirectEnvScopes(vm={}, workspace=None, agent={})

    monkeypatch.setattr(
        agent_manager, "_resolve_agent_direct_env_scopes", _spy_scopes
    )
    monkeypatch.setattr(
        agent_manager, "_agent_direct_secret_target", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running", lambda *a, **k: None
    )

    class _Sentinel(Exception):
        """Raised from resolve_for_command so the test stops before SSH."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise _Sentinel

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace()

    with pytest.raises(_Sentinel):
        agent_manager.shell_agent(
            db, config, name="a1", workspace_name="ws1",  # type: ignore[arg-type]
        )

    # The scope resolver received the workspace row, not None. The
    # workspace template env will then flow into both the SecretTarget
    # and compose_env, satisfying FRD R2 for `agent shell --workspace`.
    ws_arg = captured_scopes.get("ws")
    assert ws_arg is not None
    # Verify it's the right workspace row.
    assert getattr(ws_arg, "name", None) == "ws1"
    db.close()


def test_attach_console_build_path_eager_resolves_before_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attach_console's first-attach build path opens new shells
    (admin shell + per-session shell panes). resolve_for_command must
    fire BEFORE _build_console_tmux issues any tmux command. A failed
    eager-resolve leaves no tmux state created."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    monkeypatch.setattr(
        multi_console,
        "_prepare_vm_target_for_attach",
        lambda *a, **k: (
            SimpleNamespace(name="vm1", admin_username="admin"),
            SimpleNamespace(run=lambda *a, **k: None),
        ),
    )
    monkeypatch.setattr(multi_console, "keep_vm_active", lambda *a, **k: _NullCM())
    monkeypatch.setattr(
        multi_console, "_console_tmux_exists", lambda *a, **k: False,
    )
    monkeypatch.setattr(
        multi_console, "_console_build_secret_targets",
        lambda *a, **k: [object()],
    )

    build_called: list[bool] = []
    monkeypatch.setattr(
        multi_console,
        "_build_console_tmux",
        lambda *a, **k: build_called.append(True),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env_var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )

    monkeypatch.delenv("TMUX", raising=False)
    with pytest.raises(SecretUnavailableError, match="api-key"):
        multi_console.attach_console(db, config, name="c1")  # type: ignore[arg-type]

    assert build_called == [], (
        "eager-resolve must fire before _build_console_tmux; build ran anyway"
    )
    db.close()


def test_console_build_secret_targets_excludes_session_attach_panes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_console_build_secret_targets enumerates only the panes that
    OPEN NEW SHELLS: the admin shell (when set) and each configured
    helper shell pane. Session-attach windows are deliberately
    excluded per FRD R4 (they join existing tmux servers; SetEnv on
    the SSH connection doesn't flow into the existing server's panes).

    Pins enumeration shape (admin_shell present, helper shells one-per-
    config, no session-attach pane). The per-pane scope-selection
    contract (admin promotion, scope mix) is covered by sibling tests
    on _pane_secret_target via add_shell."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    # Seed: console with admin_shell=True + one session with two shells.
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute(
        "INSERT INTO consoles (name, vm_name, admin_shell) VALUES ('c1', 'vm1', 1)"
    )
    # Two shells: one --admin, one not. Admin-mode session promotes the
    # non-admin shell to admin via use_admin = ... or session_user ==
    # admin_user.
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) "
        "VALUES ('c1', 's1', '[{\"cwd\":null,\"admin\":true},{\"cwd\":null,\"admin\":false}]', 0)"
    )
    db._conn.commit()

    sentinel_pane = object()
    sentinel_admin = object()
    monkeypatch.setattr(
        multi_console, "_pane_secret_target", lambda *a, **k: sentinel_pane,
    )
    monkeypatch.setattr(
        multi_console, "_admin_only_secret_target", lambda *a, **k: sentinel_admin,
    )

    vm = db.get_vm("vm1")
    console = db.get_console("c1")
    assert vm is not None
    assert console is not None
    targets = multi_console._console_build_secret_targets(
        db, SimpleNamespace(), console=console, vm=vm,  # type: ignore[arg-type]
    )

    # Expected: 1 admin-shell + 2 shell panes (one per configured shell).
    # No session-attach pane.
    assert len(targets) == 3
    assert targets[0] is sentinel_admin
    assert targets[1] is sentinel_pane
    assert targets[2] is sentinel_pane
    db.close()


def test_attach_console_existing_tmux_session_skips_eager_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the tmux session already exists (plain attach, not first-
    attach build), FRD R4 says no secrets are consumed. The wiring
    must reflect that: resolve_for_command is NOT called."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    monkeypatch.setattr(
        multi_console,
        "_prepare_vm_target_for_attach",
        lambda *a, **k: (
            SimpleNamespace(name="vm1", admin_username="admin"),
            SimpleNamespace(run=lambda *a, **k: None),
        ),
    )
    monkeypatch.setattr(multi_console, "keep_vm_active", lambda *a, **k: _NullCM())
    monkeypatch.setattr(
        multi_console, "_console_tmux_exists", lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "agentworks.ssh.interactive", lambda *a, **k: 0,
    )

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command", _track_resolve
    )

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )

    monkeypatch.delenv("TMUX", raising=False)
    with pytest.raises(SystemExit):
        multi_console.attach_console(db, config, name="c1")  # type: ignore[arg-type]

    assert resolve_called == [], (
        "plain attach (existing tmux session) must NOT eager-resolve "
        "per FRD R4: it joins existing shells, consumes no secrets"
    )
    db.close()


# ---------------------------------------------------------------------------
# FRD R4 / R5 no-shell-opening surface: these commands MUST NOT eager-resolve
# ---------------------------------------------------------------------------


def test_session_list_does_not_eager_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session list`` reads the DB only; per FRD R4/R5 it opens no new
    shells and consumes no secrets. A spy on resolve_for_command must
    never fire. Seeded with a session row so the meaty body (not the
    empty-list short-circuit) is exercised."""
    from agentworks.db import SessionMode
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', ?)",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    config = SimpleNamespace()
    session_manager.list_sessions(
        db,
        config,  # type: ignore[arg-type]
        no_status=True,  # avoid SSH liveness probes
    )

    assert resolve_called == [], (
        "session list reads DB only; must not eager-resolve secrets"
    )
    db.close()


def test_session_describe_does_not_eager_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session describe`` reads DB + best-effort liveness; per FRD R4/R5
    it opens no new shells and consumes no secrets."""
    from agentworks.db import SessionMode, SessionStatus
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', ?)",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)
    # describe_session calls _prepare_vm which probes SSH connectivity.
    # Stub it and the downstream status helpers; the contract under test
    # is whether resolve_for_command fires, not the probe path.
    ws_row = db.get_workspace("ws1")
    vm_row = db.get_vm("vm1")
    assert ws_row is not None and vm_row is not None
    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        session_manager, "_prepare_vm",
        lambda *a, **k: (ws_row, vm_row, fake_target.run, None, fake_target),
    )
    monkeypatch.setattr(
        session_manager, "_ensure_pid", lambda session, **kwargs: session,
    )
    monkeypatch.setattr(
        session_manager, "check_session_status",
        lambda *a, **k: SessionStatus.UNKNOWN,
    )

    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key=None))
    # describe_session has `name` as a keyword-only arg.
    session_manager.describe_session(
        db, config, name="s1",  # type: ignore[arg-type]
    )

    assert resolve_called == [], (
        "session describe must not eager-resolve secrets"
    )
    db.close()


def test_console_add_sessions_does_not_eager_resolve_live_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``console add-sessions`` with a live tmux session: wraps existing
    sessions into new console windows via tmux new-window + attach. No
    new agent shells are opened; per FRD R4/R5 no secrets consumed."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute(
        "INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')"
    )
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)
    # Live branch: simulate a live tmux session so _add_session_window runs.
    fake_vm = SimpleNamespace(name="vm1", admin_username="admin", tailscale_host="x")
    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        multi_console, "_live_target", lambda *a, **k: (fake_vm, fake_target),
    )
    monkeypatch.setattr(
        multi_console, "_console_tmux_exists", lambda *a, **k: True,
    )
    monkeypatch.setattr(
        multi_console, "_add_session_window", lambda *a, **k: None,
    )

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )
    multi_console.add_sessions(
        db, config, console_name="c1", session_specs=["s1"],  # type: ignore[arg-type]
    )

    assert resolve_called == [], (
        "console add-sessions even on the live branch must not eager-resolve"
    )
    db.close()


def test_console_add_sessions_does_not_eager_resolve_db_only_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``console add-sessions`` DB-only branch (no live tmux): just
    inserts console_sessions rows. Trivially no secrets."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute(
        "INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')"
    )
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)
    monkeypatch.setattr(
        multi_console, "_live_target", lambda *a, **k: None
    )

    config = SimpleNamespace()
    multi_console.add_sessions(
        db, config, console_name="c1", session_specs=["s1"],  # type: ignore[arg-type]
    )

    assert resolve_called == [], (
        "console add-sessions DB-only branch must not eager-resolve"
    )
    db.close()


def test_agent_setup_runners_thread_env_via_setenv() -> None:
    """Source-level tripwire for the Phase 6.4b threading contract:
    every runner inside Phase 2 of ``_create_agent_on_vm`` that opens a
    new agent-facing shell (install commands, dotfiles install, mise
    install/prune, nerf plugin, claude plugins via _agent_run_cmd) must
    carry ``env=agent_env``. A future contributor dropping the kwarg
    from one site drops the count below 5."""
    import inspect

    from agentworks.agents import manager as agent_mgr

    src = inspect.getsource(agent_mgr._create_agent_on_vm)
    count = src.count("env=agent_env")
    assert count >= 5, (
        f"expected >=5 'env=agent_env' threading sites in _create_agent_on_vm, "
        f"got {count}; did a runner lose its env kwarg?"
    )


def test_vm_provisioning_runners_thread_env_via_setenv() -> None:
    """Source-level tripwire for the Phase 6.3b threading contract: every
    runner inside ``_phase_b_setup`` that opens a new operator-facing
    shell (dotfiles install, both mise call paths, user_install_commands,
    nerf plugin, claude plugins via _admin_run_cmd) must carry
    ``env=admin_env``.

    This is intentionally a source-inspection tripwire rather than a
    behavioral test: the behavior (env actually reaches the remote
    shell via SSH SetEnv) is already pinned by the Phase 3 ssh.py
    tests. What 6.3b adds is purely syntactic -- each call site got
    the kwarg. A future contributor dropping the kwarg from one site
    drops the count below 5; that's exactly what this assertion
    catches."""
    import inspect

    from agentworks.vms import initializer as init

    src = inspect.getsource(init._phase_b_setup)
    count = src.count("env=admin_env")
    assert count >= 5, (
        f"expected >=5 'env=admin_env' threading sites in _phase_b_setup, "
        f"got {count}; did a runner lose its env kwarg?"
    )


def test_console_add_shell_promotes_admin_for_admin_mode_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """add_shell against an admin-mode session must promote the pane's
    is_admin_pane to True even when the operator passed ``admin=False``,
    matching ``_split_shell_pane``'s ``use_admin = shell.admin or
    session_user == admin_user`` logic. Without the promotion, the
    helper's ``agent_name is None`` branch would silently skip eager-
    resolve while ``_resolve_pane_env`` at split time prompts for
    admin-scope secrets late."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) "
        "VALUES ('c1', 's1', '[]', 0)"
    )
    db._conn.commit()

    captured: dict[str, object] = {}

    def _spy_target(
        config: object,
        db: object,
        *,
        vm: object,
        session: object,
        is_admin_pane: bool,
    ) -> object:
        captured["is_admin_pane"] = is_admin_pane
        return object()

    monkeypatch.setattr(multi_console, "_pane_secret_target", _spy_target)
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: {})

    config = SimpleNamespace()

    multi_console.add_shell(
        db,
        config,  # type: ignore[arg-type]
        console_name="c1",
        session_name="s1",
        cwd=None,
        admin=False,  # operator did NOT pass --admin
    )

    assert captured["is_admin_pane"] is True, (
        "admin-mode session + add_shell without --admin should promote to admin pane"
    )
    db.close()
