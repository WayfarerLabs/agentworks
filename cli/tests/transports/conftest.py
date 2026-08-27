"""Shared fixtures and helpers for transport-package tests.

The four per-transport test modules (`test_ssh.py`, `test_lima.py`,
`test_remote_lima.py`, `test_wsl2.py`) all mock ``subprocess.run`` and
inspect argv. They each need a way to spin up a ``CompletedProcess``
mock for success and failure. Centralizing the helpers here keeps the
per-transport tests focused on the argv they're asserting.

Two pairs, because production runs child processes in two modes. A call
that only captures output stays in text mode and yields ``str`` streams;
use :func:`ok_completed` / :func:`fail_completed` for those (the ``copy``
paths). A call that writes stdin runs in byte mode so no platform newline
rewriting can touch the payload (see ``agentworks.subprocess_io``) and
yields ``bytes`` streams; use :func:`ok_binary_completed` /
:func:`fail_binary_completed` for those (the ``run`` paths). Handing a
transport the wrong pair fails the way production would if the mode and
the decoding disagreed.
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


def ok_binary_completed(stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a byte-mode ``CompletedProcess``-shaped mock for exit 0.

    Arguments stay ``str`` for readability at the call site; the streams are
    encoded because a byte-mode ``subprocess.run`` hands back ``bytes``.
    """
    cp = MagicMock()
    cp.returncode = 0
    cp.stdout = stdout.encode("utf-8")
    cp.stderr = stderr.encode("utf-8")
    return cp


def fail_binary_completed(returncode: int = 1, stderr: str = "boom") -> MagicMock:
    """Build a byte-mode ``CompletedProcess``-shaped mock for non-zero exit."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = b""
    cp.stderr = stderr.encode("utf-8")
    return cp
