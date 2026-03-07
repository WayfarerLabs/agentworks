"""Base interface for git host providers."""

from __future__ import annotations

from abc import ABC, abstractmethod


class GitHostProvider(ABC):
    """Interface for registering/removing SSH keys with a git host."""

    @abstractmethod
    def verify_auth(self) -> bool:
        """Check if authentication is valid for this provider."""

    @abstractmethod
    def auth_hint(self) -> str:
        """Return a human-readable hint for how to authenticate."""

    @abstractmethod
    def register_key(self, vm_name: str, public_key: str) -> str:
        """Register an SSH public key and return the remote key ID."""

    @abstractmethod
    def test_key_present(self, remote_key_id: str) -> bool:
        """Check if a key is still registered."""

    @abstractmethod
    def remove_key(self, remote_key_id: str) -> None:
        """Remove a previously registered key."""
