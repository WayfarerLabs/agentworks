"""Base interface for git credential providers."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


def env_var_for_credential(name: str) -> str:
    """Derive the environment variable name for a credential config name.

    e.g. "github" -> "GIT_CREDENTIALS_GITHUB"
         "azdo-ifc" -> "GIT_CREDENTIALS_AZDO_IFC"
    """
    return "GIT_CREDENTIALS_" + name.upper().replace("-", "_")


class GitCredentialProvider(ABC):
    """Interface for configuring git credentials on VMs.

    Each provider knows how to obtain a token (via prompt or CLI) and
    produce the credential line(s) for ~/.git-credentials.

    Token resolution order:
      1. GIT_CREDENTIALS_<NAME> environment variable
      2. Interactive prompt (via _prompt_token)
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

    def obtain_token(self, vm_name: str) -> str:
        """Obtain a credential token: env var first, then prompt."""
        import typer

        env_name = env_var_for_credential(self._config_name)
        token = os.environ.get(env_name)
        if token:
            typer.echo(f"  Git credential '{self.display_name}' found in environment")
            return token
        return self._prompt_token(vm_name)

    @abstractmethod
    def _prompt_token(self, vm_name: str) -> str:
        """Interactively prompt for a token."""

    @abstractmethod
    def credential_lines(self, token: str) -> list[str]:
        """Return lines for ~/.git-credentials.

        Each line is a URL in the format: https://user:token@host
        """
