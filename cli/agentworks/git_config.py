"""Shared Git configuration reconciliation helpers."""

from __future__ import annotations

from typing import Protocol


class _CommandTarget(Protocol):
    def run(self, command: str) -> object: ...


def ensure_safe_directory_wildcard(target: _CommandTarget) -> None:
    """Reconcile the global safe-directory wildcard to one exact value."""
    target.run("git config --global --fixed-value --replace-all safe.directory '*' '*'")
