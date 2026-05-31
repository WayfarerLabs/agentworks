"""Tests for the CLI's top-level error wrapper (PR 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    """If the log path can't be created, the helper must not raise -- the
    user's one-line error is more important than the persisted log."""
    # Point CONFIG_DIR at a file path so the mkdir + open both fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", blocker)

    # Should return the (uncreated) path without raising.
    try:
        raise ValueError("won't fit")
    except ValueError as exc:
        log_path = _record_unhandled_error(exc)

    assert log_path.name == "error.log"
