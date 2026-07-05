"""Base interface for git credential providers.

Providers own only the type-specific formatting (``credential_lines``).
Token resolution lives in the framework -- each ``GitCredentialConfig``
emits a ``SecretReference`` for its ``token`` field; the active backend
chain (env-var / 1Password / prompt / ...) handles the lookup, and the
token secret's health reports through the doctor Secrets group and
``agw secret describe git-token-<name>`` like any other secret.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class GitCredentialProvider(ABC):
    """Interface for configuring git credentials on VMs.

    Each provider knows how to format the credential line(s) for
    ``~/.git-credentials``. Tokens themselves come from the framework's
    backend chain, not from this class.
    """

    def __init__(self, config_name: str, description: str | None = None) -> None:
        self._config_name = config_name
        self._description = description

    @property
    def display_name(self) -> str:
        """Human-readable name: 'key (description)' or just 'key'."""
        if self._description:
            return f"{self._config_name} ({self._description})"
        return self._config_name

    @abstractmethod
    def credential_lines(self, token: str) -> list[str]:
        """Return lines for ~/.git-credentials.

        Each line is a URL in the format: https://user:token@host
        """
