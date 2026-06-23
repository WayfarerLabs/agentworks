"""Shared fixtures and helpers for transport-package tests.

The four per-transport test modules (`test_ssh.py`, `test_lima.py`,
`test_remote_lima.py`, `test_wsl2.py`) all mock ``subprocess.run`` and
inspect argv. They each need a way to spin up a ``CompletedProcess``
mock for success and failure. Centralizing the helpers here keeps the
per-transport tests focused on the argv they're asserting.
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
