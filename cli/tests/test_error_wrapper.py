"""Tests for the CLI's top-level error wrapper (PR 1)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import click
import pytest
import typer

from agentworks.cli import _record_unhandled_error
from agentworks.output import AgentworksError
from agentworks.ssh import SSHError

from .conftest import stub_build_registry, stub_session_resolvers, stub_vm_gates


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """SimpleNamespace configs don't carry publish_to; Phase 2a's
    manager-entry hoist is no-op'd via the shared helper."""
    stub_build_registry(monkeypatch)


def test_ssh_error_is_agentworks_error() -> None:
    """SSHError must be an AgentworksError subclass so main()'s wrapper catches
    it (otherwise SSH timeouts leak as tracebacks)."""
    assert issubclass(SSHError, AgentworksError)
    assert isinstance(SSHError("boom"), AgentworksError)


def test_record_unhandled_error_writes_traceback_with_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_record_unhandled_error_handles_unusable_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_main_wrapper_catches_unhandled_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_main_wrapper_lets_click_exceptions_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_main_wrapper_lets_vendored_abort_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raw ``typer.Abort`` raised from a command is handled by typer's own
    standalone runner inside ``app()`` (prints ``Aborted.`` and exits 1), so it
    must never reach our wrapper's generic Exception clause or error.log
    (issue #320)."""
    from agentworks import cli as cli_mod

    test_app = typer.Typer()

    @test_app.callback()
    def _cb() -> None:
        pass

    @test_app.command("bail")
    def bail() -> None:
        raise typer.Abort()

    monkeypatch.setattr(cli_mod, "app", test_app)
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "bail"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    # Typer's own abort handling: message + exit 1, upstream of our wrapper.
    assert exc_info.value.code == 1
    assert "Aborted." in capsys.readouterr().err
    assert not (tmp_path / "logs" / "error.log").exists()


@pytest.mark.parametrize(
    ("exc_factory", "expected_code"),
    [
        pytest.param(lambda: typer.Exit(code=5), 5, id="vendored-exit"),
        pytest.param(lambda: click.exceptions.Exit(code=3), 3, id="real-exit"),
        # A clean Exit(0) must stay 0, not be coerced to a nonzero code.
        pytest.param(lambda: typer.Exit(code=0), 0, id="vendored-exit-zero"),
        pytest.param(lambda: click.exceptions.Exit(code=0), 0, id="real-exit-zero"),
    ],
)
def test_main_wrapper_maps_escaped_exit_to_its_carried_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_factory: Callable[[], BaseException],
    expected_code: int,
) -> None:
    """An Exit (vendored or real) that escapes ``app()`` itself must exit with
    its carried code, not fall into the generic Exception clause and error.log
    (issue #320).

    No known path delivers one today (typer's standalone runner converts its
    vendored Exit to SystemExit inside ``app()``), so the escape is simulated
    by replacing ``app`` with a plain callable that raises directly. This pins
    the defensive clause in ``_entry.py`` so a framework change, or a raise
    from code running outside ``app()``, degrades to a clean exit."""
    from agentworks import cli as cli_mod

    def fake_app() -> None:
        raise exc_factory()

    monkeypatch.setattr(cli_mod, "app", fake_app)
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "whatever"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    assert exc_info.value.code == expected_code
    assert not (tmp_path / "logs" / "error.log").exists()


@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(typer.Abort, id="vendored-abort"),
        pytest.param(click.exceptions.Abort, id="real-abort"),
    ],
)
def test_main_wrapper_maps_escaped_abort_to_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc_factory: Callable[[], BaseException],
) -> None:
    """An Abort (vendored or real) that escapes ``app()`` must print
    ``Aborted.`` and exit 1, mirroring typer's own standalone Abort handling
    and the UserAbort clause, with no traceback and no error.log entry
    (issue #320). Real ctrl-C exits 130 via typer's KeyboardInterrupt-to-
    Exit(130) conversion, so SIGINT parity does not depend on this clause.
    Same simulated-escape setup as the Exit test above."""
    from agentworks import cli as cli_mod

    def fake_app() -> None:
        raise exc_factory()

    monkeypatch.setattr(cli_mod, "app", fake_app)
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "whatever"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "Aborted." in err
    assert "Traceback" not in err
    assert not (tmp_path / "logs" / "error.log").exists()


def test_main_wrapper_renders_click_usage_error_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invalid ``click.Choice`` value must render as Click's own one-line
    ``Error:`` message (not a Rich traceback), exit with the usage exit code
    (2, not our generic 1), and write nothing to ``error.log`` (a bad choice
    is user input, not a bug to debug later).

    The trigger mirrors the real CLI: an option typed with a raw
    ``click.Choice`` (``click_type=``). Typer's Rich error formatter does not
    intercept those (it catches only typer's vendored ``click`` exceptions), so
    the real-``click`` ``BadParameter`` propagates out of ``app()`` and reaches
    ``main()``'s top-level catch, which is exactly the path this test pins.
    """
    from agentworks import cli as cli_mod
    from agentworks.cli import _entry

    # A raw click.Choice option nested under a subcommand group reproduces the
    # real surface (e.g. `completion show --shell`): the invalid value raises a
    # BadParameter that Typer does not render itself, so it propagates to main().
    sub_app = typer.Typer()

    @sub_app.command("mig")
    def mig(
        toml: Annotated[str, typer.Option("--toml", click_type=click.Choice(["comment", "delete"]))] = "comment",
    ) -> None:
        pass

    test_app = typer.Typer()

    @test_app.callback()
    def _cb() -> None:
        pass

    test_app.add_typer(sub_app, name="res")

    # A bad choice must never reach record_unhandled_error / error.log.
    record_calls: list[BaseException] = []
    monkeypatch.setattr(_entry, "record_unhandled_error", lambda exc: record_calls.append(exc))

    monkeypatch.setattr(cli_mod, "app", test_app)
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["agentworks", "res", "mig", "--toml", "bogus"])
    monkeypatch.setenv("AGW_DEBUG", "")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    # Click's usage exit code (2), NOT our generic 1.
    assert exc_info.value.code == 2

    err = capsys.readouterr().err
    # Click's own one-line message, listing the valid choices. No Rich traceback.
    assert "Error:" in err
    assert "'bogus' is not one of 'comment', 'delete'." in err
    assert "Traceback" not in err

    # No traceback was logged: a bad choice is user input, not a bug.
    assert record_calls == []
    assert not (tmp_path / "logs" / "error.log").exists()


def test_main_wrapper_handles_keyboard_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _domain_error_app() -> typer.Typer:
    """A minimal app whose one command raises a clean domain error, so we
    can drive main()'s ERROR-role rendering through the real catch."""
    from agentworks.errors import NotFoundError

    test_app = typer.Typer()

    @test_app.callback()
    def _cb() -> None:
        pass

    @test_app.command("boom")
    def boom() -> None:
        raise NotFoundError("vm 'x' not found")

    return test_app


def test_domain_error_renders_red_error_prefix_on_a_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On a TTY the entry catch routes domain errors through
    ``output.error``, so the handler renders a red ``Error:`` prefix with
    the message in the default color (mirroring the yellow ``Warning:``)."""
    from agentworks import cli as cli_mod

    monkeypatch.setattr(cli_mod, "app", _domain_error_app())
    monkeypatch.setattr("sys.argv", ["agentworks", "boom"])
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert err == f"{click.style('Error:', fg='red')} vm 'x' not found\n"


def test_domain_error_renders_plain_off_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Off a TTY the same error renders byte-plain (matching today's
    ``Error: <msg>``), with no ANSI leakage."""
    from agentworks import cli as cli_mod

    monkeypatch.setattr(cli_mod, "app", _domain_error_app())
    monkeypatch.setattr("sys.argv", ["agentworks", "boom"])
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert err == "Error: vm 'x' not found\n"
    assert "\x1b" not in err


def test_create_session_rolls_back_on_keyboard_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()

    # Skip the VM-running probe entirely.
    stub_vm_gates(monkeypatch)

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
    from agentworks.agents import grants as agent_grants
    from agentworks.db import Database
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

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

    stub_vm_gates(monkeypatch)

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
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_grants, "add_to_workspace_group", lambda *a, **k: None)

    remove_calls: list[tuple[str, str]] = []

    def _track_remove(vm, config, db, linux_user, ws_name, **kwargs):  # type: ignore[no-untyped-def]
        remove_calls.append((linux_user, ws_name))

    monkeypatch.setattr(agent_grants, "remove_from_workspace_group", _track_remove)

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
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/home/me/ws1', 'ws-ws1')"
    )
    db._conn.commit()

    stub_vm_gates(monkeypatch)

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


def test_create_session_surfaces_dead_workload_output_through_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the manager: a session workload that dies instantly
    (the 2026-08-03 incident: `codex -a on-failure` rejected by clap in
    milliseconds) must surface its own captured output in the operator-visible
    failure, roll back the session's partial state, and never run the grant
    machinery against the dead socket. Drives the REAL tmux.create_session
    against a scripted agent transport whose pane_dead probe answers dead."""
    from agentworks.agents import grants as agent_grants
    from agentworks.agents import manager as agent_mgr
    from agentworks.db import Database
    from agentworks.errors import StateError
    from agentworks.sessions import manager as session_manager
    from agentworks.sessions import tmux as tmux_mod

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

    stub_vm_gates(monkeypatch)

    clap_error = "error: invalid value 'on-failure' for '--ask-for-approval <APPROVAL_POLICY>'"

    class _Result:
        def __init__(self, ok: bool = True, stdout: str = "") -> None:
            self.ok = ok
            self.returncode = 0 if ok else 1
            self.stdout = stdout
            self.stderr = ""

    class _AdminTarget:
        def run(self, *args: object, **kwargs: object) -> _Result:
            return _Result()

    agent_commands: list[str] = []

    class _DeadPaneAgentTarget:
        """Agent transport whose tmux server holds a dead pane: the liveness
        probe answers dead and capture-pane returns the workload's output."""

        def run(self, cmd: str, *args: object, **kwargs: object) -> _Result:
            agent_commands.append(cmd)
            if cmd.startswith("test -e "):
                return _Result(ok=False)
            if "#{pane_dead}" in cmd:
                return _Result(ok=True, stdout="1 2\n")
            if "capture-pane" in cmd:
                return _Result(ok=True, stdout=f"{clap_error}\n\nFor more information, try '--help'.\n")
            return _Result()

    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: _AdminTarget())
    monkeypatch.setattr("agentworks.sessions.manager.transport", lambda *a, **k: _AdminTarget())
    monkeypatch.setattr("agentworks.transports.agent_transport", lambda *a, **k: _DeadPaneAgentTarget())
    monkeypatch.setattr(agent_mgr, "_assert_agent_ssh_works", lambda *a, **k: None)
    monkeypatch.setattr(tmux_mod, "deploy_restricted_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_grants, "add_to_workspace_group", lambda *a, **k: None)
    monkeypatch.setattr(agent_grants, "remove_from_workspace_group", lambda *a, **k: None)

    warns: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", lambda msg, **k: warns.append(str(msg)))

    stub_session_resolvers(monkeypatch)

    config = SimpleNamespace(session=SimpleNamespace(history_limit=50000))

    with pytest.raises(StateError) as excinfo:
        session_manager.create_session(
            db,
            config,  # type: ignore[arg-type]
            name="s1",
            workspace="ws1",
            template_name=None,
            agent="a1",
        )

    # The typed error carries the workload's own output verbatim, plus the
    # exit status from the combined pane_dead probe.
    msg = str(excinfo.value)
    assert "exited immediately after launch (status 2)" in msg
    assert clap_error in msg

    # The manager's pre-rollback warn (the operator-visible bridge line)
    # carries the same captured output.
    reason_warns = [w for w in warns if "rolling back. Reason:" in w]
    assert reason_warns and clap_error in reason_warns[0]

    # Rollback ran: no session row, no grant left behind.
    assert db.get_session("s1") is None
    assert not db.has_any_grant("a1", "ws1")

    # The grant machinery never touched the dead socket.
    assert not any("server-access" in c for c in agent_commands)
    assert not any("chmod g+rwx" in c for c in agent_commands)
    db.close()
