"""Tests for ``SSHLogger`` close-time exception capture.

When ``close()`` is called from inside an ``except`` block (the pattern
used by every operation-level handler in ``vms/initializer.py`` and
elsewhere), the in-flight exception's traceback should be appended to
the per-op log. That keeps operation-level errors out of the shared
``error.log`` at the top of the config dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.ssh import SSHLogger

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def logger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SSHLogger]:
    """Build a fresh ``SSHLogger`` rooted in ``tmp_path`` (not the user's
    real config dir) so the test doesn't pollute on-disk state."""
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    yield SSHLogger("vm1", "test-op")


def test_close_appends_traceback_when_called_inside_except(logger: SSHLogger) -> None:
    """``close()`` introspects ``sys.exc_info()``. An exception in flight
    at close time gets its traceback appended to the log file before the
    footer. This is what routes operation-level errors away from the
    central ``error.log`` and into the per-op log instead."""
    try:
        raise RuntimeError("synthetic operation failure")
    except RuntimeError:
        logger.close()

    text = logger.path.read_text()
    assert "EXCEPTION:" in text
    assert "RuntimeError: synthetic operation failure" in text
    # Footer still emits after the traceback.
    assert "# Finished:" in text


def test_close_without_exception_omits_traceback_block(logger: SSHLogger) -> None:
    """A bare ``close()`` (no exception in flight) writes the footer
    only -- the traceback block is gated on ``sys.exc_info()``."""
    logger.close()

    text = logger.path.read_text()
    assert "EXCEPTION:" not in text
    assert "# Finished:" in text


def test_close_redacts_secrets_from_traceback(logger: SSHLogger) -> None:
    """Secrets registered via ``add_redaction`` must not leak into the
    appended traceback (the operator's Tailscale auth key or git PAT
    might appear in a ``str(exc)`` payload, which would otherwise hit
    the per-op log as plaintext)."""
    secret = "tskey-auth-supersecret-12345"
    logger.add_redaction(secret)
    try:
        raise RuntimeError(f"tailscale up failed with key={secret}")
    except RuntimeError:
        logger.close()

    text = logger.path.read_text()
    assert secret not in text
    assert "[REDACTED]" in text
