"""Shared helpers for transport-package tests.

The SSH transport's tests mock ``subprocess.run`` and inspect argv, so they
need ``subprocess.CompletedProcess``-shaped mocks for success and failure.
``SSHTransport.run`` stays in text mode and hands a sensitive payload to
``agentworks.ssh.run`` rather than writing stdin itself, so its mocks carry
``str`` streams.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def ok_completed(stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a ``subprocess.CompletedProcess``-shaped mock for exit 0."""
    cp = MagicMock()
    cp.returncode = 0
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def fail_completed(returncode: int = 1, stderr: str = "boom") -> MagicMock:
    """Build a ``subprocess.CompletedProcess``-shaped mock for non-zero exit."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = ""
    cp.stderr = stderr
    return cp
