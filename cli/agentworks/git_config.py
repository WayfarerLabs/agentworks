"""Shared Git configuration reconciliation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution.requests import Command

if TYPE_CHECKING:
    from agentworks.execution.files import Files


def ensure_safe_directory_wildcard(files: Files) -> None:
    """Reconcile the global safe-directory wildcard of ``files``' identity to one exact value.

    Every ``safe.directory`` value that is exactly ``*`` is replaced by one,
    and any other value is kept.
    """
    argv = ("git", "config", "--global", "--fixed-value", "--replace-all", "safe.directory", "*", "*")
    files.access.run(Command(argv, deadline=files.deadline), check=True)
