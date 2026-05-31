"""Tests for the CLI's top-level error wrapper (PR 1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from agentworks.cli import _record_unhandled_error
from agentworks.output import AgentworksError
from agentworks.ssh import SSHError


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
