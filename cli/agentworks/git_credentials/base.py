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
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from agentworks.resources.reference import ConfigReference


def _http_probe(
    url: str, headers: dict[str, str], *, timeout: float = 5.0
) -> tuple[int, bytes, dict[str, str]]:
    """GET ``url``; returns (status, body, lowercased-headers).

    HTTP error statuses are returned, not raised; network-level
    failures raise ``OSError`` (URLError subclasses it) for the caller
    to treat as indeterminate.
    """
    from urllib import error, request

    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return (
                resp.status,
                resp.read(),
                {k.lower(): v for k, v in resp.headers.items()},
            )
    except error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return (exc.code, body, {k.lower(): v for k, v in exc.headers.items()})


@dataclass(frozen=True)
class HelperEntry:
    """What the credential helper needs to select this credential by
    remote URL: the host it serves, the username on its store line
    (the key back into the managed store file), and its scopes --
    ``repos`` match the remote path exactly, ``owner`` matches its
    first segment. No scopes = the host's default candidate.
    """

    host: str
    username: str
    repos: tuple[str, ...] = ()
    owner: str | None = None


@dataclass(frozen=True)
class TokenInfo:
    """The provider-acquired token plus what acquisition learned.

    ``verified`` means the provider confirmed the token against its
    service; ``login`` and ``expires_at`` are best-effort extras the
    verification response exposed (displayed by provisioning output and
    doctor -- deliberately NOT wired to the advisory
    ``metadata.expires``, which is general resource metadata).
    """

    token: str
    login: str | None = None
    expires_at: date | None = None
    verified: bool = False


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

    def acquire_token(self, resolved_secret: str) -> TokenInfo:
        """Turn the resolved token secret into THE token to provision.

        The transformation seam: today the implementations verify the
        mapped secret's value against the provider's API and return it
        enriched (login, expiry); tomorrow a minting provider can
        override to EXCHANGE a bootstrap secret for a fresh token --
        "today validates, tomorrow fetches" -- without touching the
        framework's secret resolution, which stays upstream (eager
        resolve at manager entry, prompt fallback, doctor prediction).

        Error policy (maintainer ruling): a DEFINITIVE rejection by the
        service raises ``TokenRejectedError`` -- callers invoke this at
        provisioning ENTRY, before anything is created, so failing is
        safe; if acquisition ever moves mid-flow, the caller must
        downgrade to warn. Network indeterminacy (timeouts, DNS, 5xx)
        never raises: warn and return unverified.

        Base behavior: identity, unverified (providers without a
        verification endpoint).
        """
        return TokenInfo(token=resolved_secret)

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

    @abstractmethod
    def helper_entry(self) -> HelperEntry:
        """This credential's selection entry for the generated helper.

        The helper receives (host, path) per query -- ``useHttpPath``
        is set globally in the managed include -- and picks the most
        specific credential: exact repo, then owner (first path
        segment), then the host's default (an entry without scopes),
        then the first store line for the host (legacy semantics, which
        also keeps ``vm add-git-credential`` additions serving).
        """

    @abstractmethod
    def credential_lines(self, token: str) -> list[str]:
        """Return lines for ~/.git-credentials.

        Each line is a URL in the format: https://user:token@host
        """
