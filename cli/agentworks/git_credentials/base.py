"""Base interface for git credential providers.

Phase 1d of the Resource Registry SDD: providers now own only the
type-specific formatting (``credential_lines``) and authn pre-flight
(``verify_auth`` / ``auth_hint``). Token resolution moved to the
framework -- each ``GitCredentialConfig`` emits a ``SecretRequirement``
for its ``token`` field; the resolver chain (env-var / 1Password /
prompt / ...) handles the lookup. The previous provider-side env-var
helpers and prompt method are gone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class GitCredentialProvider(ABC):
    """Interface for configuring git credentials on VMs.

    Each provider knows how to format the credential line(s) for
    ``~/.git-credentials`` and how to pre-flight authn (e.g., warn
    the operator before provisioning if their CLI / browser state
    isn't ready to mint a token). Tokens themselves come from the
    framework's resolver chain, not from this class.
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
    def verify_auth(self) -> bool:
        """Check if authentication is possible (e.g. CLI tools present).

        For prompt-based providers, this always returns True.
        """

    @abstractmethod
    def auth_hint(self) -> str:
        """Return a human-readable hint for how to authenticate."""

    @abstractmethod
    def credential_lines(self, token: str) -> list[str]:
        """Return lines for ~/.git-credentials.

        Each line is a URL in the format: https://user:token@host
        """
