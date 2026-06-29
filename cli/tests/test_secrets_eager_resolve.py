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
    monkeypatch.setattr("agentworks.transports.transport", factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", factory)


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
        required_commands: list[str] = []
        env: dict[str, str] = {}

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _Tmpl())
    # _session_secret_target_pre_create reads config.vm_templates /
    # agent_templates which the SimpleNamespace below doesn't have; stub
    # it to a sentinel so the orchestrator still calls resolve_for_command.
    sentinel_target = object()
    monkeypatch.setattr(
        session_manager, "_session_secret_target_pre_create", lambda *a, **k: sentinel_target
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(SecretUnavailableError, match="api-key"):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name=None,
            agent_name=None,
            admin=True,
        )

    # State must be untouched.
    assert db.get_session("s1") is None
    db.close()


def test_session_create_calls_resolve_with_session_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_session passes a single SecretTarget (the one returned by
    ``_session_secret_target_pre_create``) into resolve_for_command.
    Verifies the glue that turns a session command into a candidate set."""
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    _stub_session_prep(monkeypatch)

    class _Tmpl:
        name = "default"
        command = ""
        restart_command = None
        required_commands: list[str] = []
        env: dict[str, str] = {}

    monkeypatch.setattr(session_manager, "_resolve_template", lambda *a, **k: _Tmpl())

    sentinel_target = object()
    monkeypatch.setattr(
        session_manager, "_session_secret_target_pre_create", lambda *a, **k: sentinel_target
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
            workspace="ws1",
            template_name=None,
            agent_name=None,
            admin=True,
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

    # A non-NULL socket_path keeps this session on the new per-session-socket
    # model; with socket_path=None, restart_session treats the row as a
    # legacy migration target and skips the gates these tests exist to pin.
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, pid, socket_path) "
        "VALUES ('s1', 'ws1', 'default', ?, 9999, '/tmp/sock')",
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

    # A non-NULL socket_path keeps this session on the new per-session-socket
    # model; with socket_path=None, restart_session treats the row as a
    # legacy migration target and skips the gates these tests exist to pin.
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, pid, socket_path) "
        "VALUES ('s1', 'ws1', 'default', ?, 9999, '/tmp/sock')",
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
        required_commands: list[str] = []
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
            hint="api-key: tried env-var",
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
            hint="api-key: tried env-var",
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


def test_vm_create_does_not_eager_resolve_operator_env() -> None:
    """Provisioning is hermetic: operator [admin.env] / [vm_templates.*.env]
    secrets are NOT prompted at vm create. Phase 1c of the Resource
    Registry SDD routes Tailscale through ``resolve_for_command`` with
    ``targets=[]`` and ``extra_decls=[tailscale_decl]`` -- a tight call
    shape that resolves ONLY the system secret without walking any
    SecretTarget env scopes. Verify by source inspection that no
    ``SecretTarget(...)`` constructor appears in the vm-create call path
    (no env scope handed to the resolver).

    Tailscale auth key / git credentials remain provisioning-time
    secrets; Phase 1c moved Tailscale to the framework, Phase 1d will
    move git credentials.
    """
    import inspect

    from agentworks.vms import manager as vm_manager

    # Walk the call chain explicitly so the check survives refactors.
    sources = [
        inspect.getsource(vm_manager.create_vm),
        inspect.getsource(vm_manager._collect_secrets),
    ]
    joined = "\n".join(sources)
    assert "SecretTarget(" not in joined, (
        "found SecretTarget(...) constructed in the vm-create path; "
        "provisioning should not walk operator-env scopes. Operator env "
        "reaches runtime shells only; they get prompted at the use site."
    )


def test_vm_reinit_does_not_eager_resolve_operator_env() -> None:
    """Mirror of test_vm_create_does_not_eager_resolve_operator_env for
    vm reinit. Provisioning paths are hermetic; runtime paths are where
    operator-env secrets get prompted."""
    import inspect

    from agentworks.vms import manager as vm_manager

    src = inspect.getsource(vm_manager.reinit_vm)
    assert "SecretTarget(" not in src, (
        "found SecretTarget(...) constructed in reinit_vm; provisioning "
        "should not walk operator-env scopes."
    )


def test_agent_create_does_not_eager_resolve_operator_env() -> None:
    """Provisioning is hermetic: operator [agent.env] / [vm_templates.*.env]
    secrets are NOT prompted at agent create. They're prompted at the
    use site (agent shell, session create, etc.). git credentials remain
    prompted upfront via _collect_agent_credentials; they're a
    provisioning-time concern that lives outside the env-block system."""
    import inspect

    from agentworks.agents import manager as agent_manager

    src = inspect.getsource(agent_manager.create_agent)
    assert "resolve_for_command" not in src, (
        "found resolve_for_command in create_agent; provisioning should "
        "not prompt for operator-env secrets."
    )


def test_agent_reinit_does_not_eager_resolve_operator_env() -> None:
    """Mirror of test_agent_create_does_not_eager_resolve_operator_env
    for agent reinit."""
    import inspect

    from agentworks.agents import manager as agent_manager

    src = inspect.getsource(agent_manager.reinit_agent)
    assert "resolve_for_command" not in src, (
        "found resolve_for_command in reinit_agent; provisioning should "
        "not prompt for operator-env secrets."
    )


def test_vm_shell_eager_resolve_fires_before_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_vm must call resolve_for_command BEFORE opening the SSH
    session. A failed eager-resolve produces no SSH call."""
    from agentworks.vms import manager as vm_manager

    db = _seed_basic_db(tmp_path)

    monkeypatch.setattr(
        vm_manager, "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, workspace=None, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    ssh_called: list[bool] = []

    class _Target:
        def interactive(self, *args: object, **kwargs: object) -> int:
            ssh_called.append(True)
            return 0

    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda *a, **k: _Target(),
    )

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
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, workspace=None, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    streaming_calls: list[str] = []

    class _Target:
        def call_streaming(self, cmd: str, *, env: object = None) -> int:
            streaming_calls.append(cmd)
            return 0

    monkeypatch.setattr(
        "agentworks.transports.transport", lambda *a, **k: _Target()
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
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    streaming_calls: list[str] = []

    class _Target:
        def call_streaming(self, cmd: str, *, env: object = None) -> int:
            streaming_calls.append(cmd)
            return 0

    monkeypatch.setattr(
        "agentworks.transports.agent_transport", lambda *a, **k: _Target()
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
            hint="api-key: tried env-var",
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
            SimpleNamespace(
                run=lambda *a, **k: None,
                interactive=lambda *a, **k: 0,
            ),
        ),
    )
    monkeypatch.setattr(multi_console, "keep_vm_active", lambda *a, **k: _NullCM())
    monkeypatch.setattr(
        multi_console, "_console_tmux_exists", lambda *a, **k: True,
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


def test_session_attach_does_not_eager_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session attach`` joins an existing tmux session via SSH; the
    existing session retains its create-time env (FRD R5 "Attach
    inherits create-time env"). Eager-resolve must NOT fire."""
    from agentworks.db import SessionMode, SessionStatus
    from agentworks.sessions import manager as session_manager

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, pid) "
        "VALUES ('s1', 'ws1', 'default', ?, 1234)",
        (SessionMode.ADMIN.value,),
    )
    db._conn.commit()

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    # Stub out the SSH probe + transport so we don't need a real VM.
    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="", stderr=""),
        interactive=lambda *a, **k: 0,
    )
    ws_row = db.get_workspace("ws1")
    vm_row = db.get_vm("vm1")
    assert ws_row is not None and vm_row is not None
    monkeypatch.setattr(
        session_manager, "_prepare_vm",
        lambda *a, **k: (ws_row, vm_row, fake_target.run, None, fake_target),
    )
    monkeypatch.setattr(
        session_manager, "_ensure_pid", lambda session, **kwargs: session,
    )
    monkeypatch.setattr(
        session_manager, "check_session_status",
        lambda *a, **k: SessionStatus.OK,
    )
    monkeypatch.setattr(session_manager, "keep_vm_active", lambda *a, **k: _NullCM())

    import contextlib

    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key=None))
    # interactive() returns int and the manager doesn't sys.exit here,
    # but suppress for forward-compat with future refactors.
    with contextlib.suppress(SystemExit):
        session_manager.attach_session(db, config, name="s1")  # type: ignore[arg-type]

    assert resolve_called == [], (
        "session attach joins existing shell; must not eager-resolve"
    )
    db.close()


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


def test_agent_setup_runners_have_no_env_injection() -> None:
    """Source-level tripwire: provisioning is hermetic. None of the agent
    setup runners (install commands, dotfiles install, mise, claude
    plugins) should pass ``env=`` from operator [agent.env] /
    [vm_templates.*.env] tables. Static identity (AGENTWORKS_AGENT)
    reaches them via the per-user ~/.agentworks-profile.sh, written
    EARLY in agent setup phase 2 before any install command runs.
    A future contributor adding ``env=agent_env`` (or any variant) to a
    runner re-introduces the coupling this rule exists to prevent."""
    import inspect

    from agentworks.agents import manager as agent_mgr

    src = inspect.getsource(agent_mgr._create_agent_on_vm)
    assert "env=agent_env" not in src, (
        "found 'env=agent_env' in _create_agent_on_vm; provisioning runners "
        "must not inject operator env. Identity comes via the per-user "
        "profile fragment, not SetEnv."
    )
    assert "agent_env = compose_env" not in src, (
        "found 'agent_env = compose_env' in _create_agent_on_vm; the "
        "operator-env composition was removed because no provisioning "
        "runner consumes it."
    )


def test_vm_provisioning_runners_have_no_env_injection() -> None:
    """Source-level tripwire: provisioning is hermetic. None of the VM
    init user-facing runners (dotfiles install, mise, user_install_commands,
    claude plugins) should pass ``env=`` from operator [admin.env] /
    [vm_templates.*.env] tables. Static identity reaches
    them via the system-wide /etc/profile.d/agentworks-identity.sh
    written by VM init. Operator env only lands at RUNTIME shells.

    A future contributor adding ``env=admin_env`` to a provisioning
    runner re-introduces the build-time-config-coupling this rule
    exists to prevent."""
    import inspect

    from agentworks.vms import initializer as init

    src = inspect.getsource(init._phase_b_setup)
    assert "env=admin_env" not in src, (
        "found 'env=admin_env' in _phase_b_setup; provisioning runners "
        "must not inject operator env. Identity reaches them via the "
        "system-wide /etc/profile.d/ fragment, not SetEnv."
    )
    assert "admin_env = compose_env" not in src, (
        "found 'admin_env = compose_env' in _phase_b_setup; the "
        "operator-env composition was removed because no provisioning "
        "runner consumes it."
    )


def test_phase_b_setup_ends_with_ensure_files_sourced() -> None:
    """Defensive: ``_ensure_agentworks_files_sourced`` runs as the final
    step of admin VM init so that source lines in shell rc files survive
    a dotfiles installer that ships its own ``.zprofile`` / ``.bashrc`` /
    etc. The grep-or-append shape is idempotent; the rule is just that
    the call exists at the end."""
    import inspect

    from agentworks.vms import initializer as init

    src = inspect.getsource(init._phase_b_setup)
    assert "_ensure_agentworks_files_sourced" in src, (
        "expected _ensure_agentworks_files_sourced call in _phase_b_setup; "
        "without it, a dotfiles installer that overwrites a shell rc "
        "file can leave AGENTWORKS_AGENT and mise activation unreachable."
    )


def test_create_agent_on_vm_ends_with_ensure_files_sourced() -> None:
    """Mirror of test_phase_b_setup_ends_with_ensure_files_sourced for the
    agent path. Agent's dotfiles install runs after the early profile
    write; the final ensure step recovers if dotfiles clobbered our
    source lines."""
    import inspect

    from agentworks.agents import manager as agent_mgr

    src = inspect.getsource(agent_mgr._create_agent_on_vm)
    assert "_ensure_agentworks_files_sourced" in src, (
        "expected _ensure_agentworks_files_sourced call in "
        "_create_agent_on_vm; without it, dotfiles install can leave "
        "AGENTWORKS_AGENT and mise activation unreachable for the agent."
    )


def test_console_add_sessions_with_shells_eager_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``console add-sessions s1+2`` requests 2 new shell panes per
    session. Per FRD R4 those panes consume secrets at open time, so
    eager-resolve must fire BEFORE the DB write that records the new
    shells. Regression test for the PR-review finding that the +N path
    silently opened shells with no eager-resolve."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    monkeypatch.setattr(
        multi_console, "_pane_secret_target", lambda *a, **k: object(),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        multi_console.add_sessions(
            db, config,  # type: ignore[arg-type]
            console_name="c1", session_specs=["s1+2"],
        )

    # DB write must not have happened.
    assert db.get_console_session("c1", "s1") is None, (
        "eager-resolve must fire BEFORE the console_sessions DB insert "
        "when any spec requests shells"
    )
    db.close()


def test_console_add_sessions_without_shells_does_not_eager_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``console add-sessions s1 s2`` (no +N) opens no new shells in the
    DB-write path; it only registers DB rows. The live-attach wrappers
    join existing tmux servers without consuming secrets. Eager-resolve
    must NOT fire."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    db._conn.commit()

    monkeypatch.setattr(multi_console, "_live_target", lambda *a, **k: None)

    resolve_called: list[bool] = []

    def _track_resolve(*args: object, **kwargs: object) -> dict[str, str]:
        resolve_called.append(True)
        return {}

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _track_resolve)

    config = SimpleNamespace()
    multi_console.add_sessions(
        db, config,  # type: ignore[arg-type]
        console_name="c1", session_specs=["s1"],
    )

    assert resolve_called == [], (
        "add-sessions without +N must not eager-resolve; wrappers only "
        "join existing sessions"
    )
    db.close()


def test_restore_session_window_missing_branch_eager_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When restore_session's window-missing path rebuilds via
    _add_session_window, it opens new shells -- so eager-resolve must
    fire BEFORE the rebuild. Regression test for the PR-review finding
    that this branch bypassed the eager-resolve wired further down."""
    from agentworks.sessions import multi_console

    db = _seed_basic_db(tmp_path)
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode) "
        "VALUES ('s1', 'ws1', 'default', 'admin')"
    )
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('c1', 'vm1')")
    # Two configured shells so the rebuild would open new panes.
    db._conn.execute(
        "INSERT INTO console_sessions (console_name, session_name, shells, position) "
        "VALUES ('c1', 's1', "
        "'[{\"cwd\":null,\"admin\":true},{\"cwd\":null,\"admin\":false}]', 0)"
    )
    db._conn.commit()

    fake_vm = SimpleNamespace(name="vm1", admin_username="admin", tailscale_host="x")
    # `tmux list-windows` returns names NOT including 's1' (window missing).
    fake_target = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(ok=True, returncode=0, stdout="other-window", stderr=""),
    )
    monkeypatch.setattr(
        multi_console, "_prepare_vm_target_for_attach",
        lambda *a, **k: (fake_vm, fake_target),
    )
    monkeypatch.setattr(multi_console, "keep_vm_active", lambda *a, **k: _NullCM())
    monkeypatch.setattr(
        multi_console, "_console_tmux_exists", lambda *a, **k: True,
    )
    monkeypatch.setattr(
        multi_console, "_restore_session_secret_targets",
        lambda *a, **k: [object(), object()],
    )

    add_called: list[bool] = []
    monkeypatch.setattr(
        multi_console,
        "_add_session_window",
        lambda *a, **k: add_called.append(True),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    monkeypatch.setattr("agentworks.secrets.resolve_for_command", _explode)

    config = SimpleNamespace(
        named_console=SimpleNamespace(tmux_layout="aw-session-vertical"),
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        multi_console.restore_session(
            db, config,  # type: ignore[arg-type]
            console_name="c1", session_name="s1",
        )

    assert add_called == [], (
        "eager-resolve must fire BEFORE _add_session_window in the "
        "window-missing rebuild branch"
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
