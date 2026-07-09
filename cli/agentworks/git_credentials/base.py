"""Base interface for git credential providers.

Providers own the type-specific formatting (``credential_lines``) and
the validation of their own ``provider_config`` block
(``validate_config``). Token resolution lives in the framework -- each
``GitCredentialConfig`` emits a ``SecretReference`` for its ``token``
field; the active backend chain (env-var / 1Password / prompt / ...)
handles the lookup, and the token secret's health reports through the
doctor Secrets group and ``agw secret describe git-token-<name>`` like
any other secret.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.reference import ConfigReference


class GitCredentialProvider(ABC):
    """Interface for configuring git credentials on VMs.

    Each provider knows how to format the credential line(s) for
    ``~/.git-credentials``. Tokens themselves come from the framework's
    backend chain, not from this class.
    """

    provider_name: ClassVar[str]

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        """Validate ``config`` (the ``provider_config`` block owned by
        ``owner``) and return the resource references it implies.

        Invoked at each source's blob boundary (manifest decode with
        ``file:line`` framing; the TOML loader) and by the owning
        resource's ``referenced_resources()`` at finalize. ``owner`` is
        display context for error messages.

        Base behavior: accepts no configuration. Subclasses with config
        override wholesale.

        NOTE: this invoked-validation API may be deprecated in favor of
        capabilities pushing a declarative config schema definition at
        registration time (fields typed as resource references to
        specific kinds, with usage information), letting the core
        engine validate and derive references without invoking the
        capability.
        """
        if config:
            display = getattr(cls, "provider_name", cls.__name__)
            raise ConfigError(
                f"{owner}: the {display} provider accepts no "
                f"configuration; got {sorted(config)}"
            )
        return ()

    def __init__(
        self,
        config_name: str,
        description: str | None = None,
        *,
        secret_name: str | None = None,
    ) -> None:
        self._config_name = config_name
        self._description = description
        # The secret holding this credential's token (for diagnostics:
        # the helper's rejection message names it). None only in legacy
        # construction paths that never reach the helper generator.
        self._secret_name = secret_name

    @property
    def secret_name(self) -> str:
        return self._secret_name or f"git-token-{self._config_name}"

    @property
    def store_username(self) -> str:
        """The username on this credential's store line -- the join key
        the credential helper and context sections select by."""
        return self._config_name

    @property
    def display_name(self) -> str:
        """Human-readable name: 'key (description)' or just 'key'."""
        if self._description:
            return f"{self._config_name} ({self._description})"
        return self._config_name

    def gitconfig_sections(self) -> list[tuple[str, str]]:
        """``(context_url, username)`` pairs for scoped credentials.

        Each pair becomes a provisioned
        ``[credential "<context_url>"]\\nusername = <username>`` section:
        git injects the username for remotes matching the context
        (longest-prefix match on slash boundaries), and the
        username-tagged store line supplies the token. Empty (the
        default) means the credential is unscoped -- its store line is
        the host-level fallback and must be emitted BEFORE scoped lines
        (username-less queries take the first matching line; scoped
        queries carry an injected username that filters lines).
        """
        return []

    @abstractmethod
    def credential_lines(self, token: str) -> list[str]:
        """Return lines for ~/.git-credentials.

        Each line is a URL in the format: https://user:token@host
        """
