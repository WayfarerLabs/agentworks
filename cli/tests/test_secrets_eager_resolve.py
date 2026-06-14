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
