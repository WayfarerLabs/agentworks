"""Tests for the CLI's top-level error wrapper (PR 1)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from agentworks.cli import _record_unhandled_error
from agentworks.output import AgentworksError
from agentworks.ssh import SSHError

from .conftest import stub_session_resolvers


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """SimpleNamespace configs in this module don't carry publish_to;
    bypass the Phase 2a manager-entry hoist for the mock-config style.
    """
    monkeypatch.setattr("agentworks.bootstrap.build_registry", lambda config: None)


def test_ssh_error_is_agentworks_error() -> None:
    """SSHError must be an AgentworksError subclass so main()'s wrapper catches
    it (otherwise SSH timeouts leak as tracebacks)."""
    assert issubclass(SSHError, AgentworksError)
    assert isinstance(SSHError("boom"), AgentworksError)


def test_record_unhandled_error_writes_traceback_with_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error log records timestamp, argv, and the full traceback so a
    user-visible one-liner can be backed by a debuggable artifact."""
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "vm", "create", "broken"])

    try:
        raise RuntimeError("synthetic failure")
    except RuntimeError as exc:
        log_path = _record_unhandled_error(exc)

    assert log_path == tmp_path / "logs" / "error.log"
    text = log_path.read_text()
    # Separator + timestamp line.
    assert "=" * 40 in text
    # Argv captured.
    assert "argv: agentworks vm create broken" in text
    # Traceback present with the exception type and message.
    assert "RuntimeError: synthetic failure" in text
    assert "Traceback" in text


def test_record_unhandled_error_handles_unusable_log_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the log path can't be created, the helper must return None (signal
    to the caller) without raising. The user's one-line error is more
    important than the persisted log."""
    # Point CONFIG_DIR at a file path so the mkdir + open both fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", blocker)

    try:
        raise ValueError("won't fit")
    except ValueError as exc:
        result = _record_unhandled_error(exc)

    assert result is None


def test_main_wrapper_catches_unhandled_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register a throwaway command that raises an arbitrary Exception and
    confirm main()'s top-level wrapper prints a clean one-liner + log path
    instead of leaking a traceback."""
    from agentworks import cli as cli_mod

    # Build a fresh Typer app so we don't pollute the real one. The wrapper
    # under test lives inside main(); we drive it by patching the module-level
    # `app` to our minimal one and invoking main() directly. A no-op callback
    # is required so Typer treats the app as a subcommand group rather than
    # inlining the single command's params as top-level args.
    test_app = typer.Typer()

    @test_app.callback()
    def _cb() -> None:
        pass

    @test_app.command("kaboom")
    def kaboom() -> None:
        raise RuntimeError("synthetic blowup")

    monkeypatch.setattr(cli_mod, "app", test_app)
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "kaboom"])
    # Force debug off even if AGW_DEBUG happens to be set in the test env.
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    assert exc_info.value.code == 1
    # The traceback should be in error.log.
    log_path = tmp_path / "logs" / "error.log"
    assert log_path.exists()
    assert "RuntimeError: synthetic blowup" in log_path.read_text()


def test_main_wrapper_lets_click_exceptions_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """typer.Exit / Click ClickException must NOT be caught by our broad
    handler -- they own their own rendering and exit codes."""
    from agentworks import cli as cli_mod

    test_app = typer.Typer()

    @test_app.callback()
    def _cb() -> None:
        pass

    @test_app.command("bail")
    def bail() -> None:
        raise typer.Exit(code=7)

    monkeypatch.setattr(cli_mod, "app", test_app)
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "bail"])

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    # Exit code 7 propagated from typer.Exit, not 1 from our wrapper.
    assert exc_info.value.code == 7
    # And no error.log entry from our wrapper.
    log_path = tmp_path / "logs" / "error.log"
    assert not log_path.exists()


def test_main_wrapper_handles_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KeyboardInterrupt from inside a command exits cleanly with code 130.

    The Ctrl-C contract: no traceback, no error.log entry (KI isn't a bug to
    be debugged later), conventional SIGINT exit code. Typer itself converts
    KI to ``click.Exit(130)`` before our top-level wrapper sees it -- this
    test pins that contract so a future framework change can't silently
    regress it.
    """
    from agentworks import cli as cli_mod

    test_app = typer.Typer()

    @test_app.callback()
    def _cb() -> None:
        pass

    @test_app.command("interrupt")
    def interrupt() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "app", test_app)
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "interrupt"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    assert exc_info.value.code == 130
    # No traceback logged -- a Ctrl-C isn't a bug to debug later.
    assert not (tmp_path / "logs" / "error.log").exists()


def test_create_session_rolls_back_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spot-check the per-op rollback pattern: a KeyboardInterrupt raised from
    inside the long-running SSH-driven part of ``create_session`` must trigger
    the DB rollback (delete_session) and re-raise the KI unchanged.

    Future drift between the KI branch and the sibling ``except Exception``
    branch should fail this test, not slip past code review."""
    from agentworks.db import Database
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

    db_path = tmp_path / "test.db"
    db = Database(db_path)
    # Minimal fixture: VM with tailscale_host + workspace.
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'admin', '100.64.0.5')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()

    # Skip the VM-running probe entirely.
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running",
        lambda *args, **kwargs: None,
    )

    class _Result:
        ok = True
        returncode = 0
        stdout = ""
        stderr = ""

    class _Target:
        def run(self, *args: object, **kwargs: object) -> _Result:
            return _Result()

    fake_factory = lambda vm, config, **kwargs: _Target()  # noqa: E731
    # Patch both locations: manager imports ``transport`` eagerly at module
    # load, so the agentworks.transports-side patch alone wouldn't take effect.
    monkeypatch.setattr("agentworks.transports.transport", fake_factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", fake_factory)
    # deploy_restricted_config does its own SSH writes -- skip them.
    monkeypatch.setattr(
        session_manager,
        "_build_session_command",
        lambda *args, **kwargs: "true",
    )
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *args, **kwargs: None)

    # The inner SSH operation raises KI mid-way through.
    def _explode(*args: object, **kwargs: object) -> tuple[None, None]:
        raise KeyboardInterrupt

    monkeypatch.setattr(tmux_mod, "create_session", _explode)

    stub_session_resolvers(monkeypatch)

    # Stand-in Config: only the few attributes the code path under test reads.
    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(KeyboardInterrupt):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name=None,
            agent_name=None,
            admin=True,
        )

    # Rollback ran: the session row that was inserted before create_tmux_session
    # ran is gone.
    assert db.get_session("s1") is None
    db.close()


def test_create_session_releases_group_membership_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KI rollback for the agent path must revoke the implicit grant AND remove
    the agent from the workspace's Linux group, so DB and on-VM authorization
    don't drift. Without this, a Ctrl-C during ``session create --agent`` would
    leave the agent with VM-side group membership but no DB grant backing it."""
    from agentworks.agents import manager as agent_mgr
    from agentworks.db import Database
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

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

    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running",
        lambda *args, **kwargs: None,
    )

    class _Result:
        ok = True
        returncode = 0
        stdout = ""
        stderr = ""

    class _Target:
        def run(self, *args: object, **kwargs: object) -> _Result:
            return _Result()

    fake_factory = lambda vm, config, **kwargs: _Target()  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", fake_factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", fake_factory)
    # agent_transport is constructed in session_manager.create_session for
    # agent-mode sessions (FRD R1, direct target-user SSH). Stub it too so
    # the SimpleNamespace config doesn't need an `operator` attribute.
    agent_factory = lambda vm, config, agent, **kwargs: _Target()  # noqa: E731
    monkeypatch.setattr("agentworks.transports.agent_transport", agent_factory)
    monkeypatch.setattr(
        session_manager, "_build_session_command", lambda *args, **kwargs: "true"
    )
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_mgr, "_add_to_workspace_group", lambda *a, **k: None)

    remove_calls: list[tuple[str, str]] = []

    def _track_remove(vm, config, db, linux_user, ws_name, **kwargs):  # type: ignore[no-untyped-def]
        remove_calls.append((linux_user, ws_name))

    monkeypatch.setattr(agent_mgr, "_remove_from_workspace_group", _track_remove)

    def _explode(*args: object, **kwargs: object) -> tuple[None, None]:
        raise KeyboardInterrupt

    monkeypatch.setattr(tmux_mod, "create_session", _explode)

    stub_session_resolvers(monkeypatch)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(KeyboardInterrupt):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name=None,
            agent="a1",
        )

    assert db.get_session("s1") is None
    assert not db.has_any_grant("a1", "ws1")
    assert remove_calls == [("aw-a1", "ws1")]
    db.close()


def test_create_session_rollback_failure_does_not_mask_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safe-rollback contract: if a cleanup step itself raises, the
    original KeyboardInterrupt must still propagate, and the user must see
    a warning about the failed cleanup. Without this, a DB lock or SSH
    error during rollback would replace the user's Ctrl-C with an opaque
    error exit, breaking the SIGINT exit-code contract."""
    from agentworks.db import Database
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

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

    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running",
        lambda *args, **kwargs: None,
    )

    class _Result:
        ok = True
        returncode = 0
        stdout = ""
        stderr = ""

    class _Target:
        def run(self, *args: object, **kwargs: object) -> _Result:
            return _Result()

    fake_factory = lambda vm, config, **kwargs: _Target()  # noqa: E731
    monkeypatch.setattr("agentworks.transports.transport", fake_factory)
    monkeypatch.setattr("agentworks.sessions.manager.transport", fake_factory)
    monkeypatch.setattr(
        session_manager, "_build_session_command", lambda *args, **kwargs: "true"
    )
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *args, **kwargs: None)

    def _explode(*args: object, **kwargs: object) -> tuple[None, None]:
        raise KeyboardInterrupt

    monkeypatch.setattr(tmux_mod, "create_session", _explode)

    # Poison the rollback: db.delete_session raises during cleanup.
    cleanup_attempts: list[str] = []
    original_delete = db.delete_session

    def _failing_delete_session(name: str) -> None:
        cleanup_attempts.append(name)
        # Restore the original so the assertion below can observe state;
        # in reality db.delete_session might fail repeatedly.
        db.delete_session = original_delete  # type: ignore[method-assign]
        raise RuntimeError("simulated DB lock during rollback")

    db.delete_session = _failing_delete_session  # type: ignore[method-assign]

    stub_session_resolvers(monkeypatch)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    # The KI from create_tmux_session must surface, NOT the RuntimeError
    # from the failing rollback step. That is the masking guarantee.
    with pytest.raises(KeyboardInterrupt):
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name=None,
            agent_name=None,
            admin=True,
        )

    # The poisoned delete_session was called (confirming we did exercise
    # the failing path) and then restored, but the session row was never
    # actually removed because the first delete attempt raised.
    assert cleanup_attempts == ["s1"]
    db.close()
