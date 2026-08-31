"""Provider contract for declarative Git HTTPS credentials.

Providers own acquisition and forge-specific scope translation. Core gives a
provider only its declared runtime inputs and receives final credential
material: either a username/password pair to store or a provider-authored
helper to invoke at Git runtime.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from agentworks.capabilities.base import Capability

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from agentworks.capabilities.base import RunContext


def _http_probe(url: str, headers: dict[str, str], *, timeout: float = 5.0) -> tuple[int, bytes, dict[str, str]]:
    """GET ``url`` and return status, body, and lower-cased headers."""
    from urllib import error, request

    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return (
                resp.status,
                resp.read(),
                {key.lower(): value for key, value in resp.headers.items()},
            )
    except error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return (exc.code, body, {key.lower(): value for key, value in exc.headers.items()})


def require_line_safe_credential_input(value: str, *, secret_name: str) -> str:
    """Validate a secret before a provider uses it in line-oriented auth."""
    from agentworks.secrets.line_safety import (
        LineOrientedSecretUse,
        require_line_safe_secret,
    )

    return require_line_safe_secret(
        value,
        use=LineOrientedSecretUse.GIT_CREDENTIAL,
        secret_name=secret_name,
    )


@dataclass(frozen=True)
class HttpsCredentialScope:
    """One exact HTTPS host and optional segment-aware path prefix."""

    host: str
    path_prefix: tuple[str, ...] = ()


@dataclass(frozen=True)
class StoredCredential:
    """A final Git username/password response for private storage."""

    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class ManagedHelper:
    """A provider-authored runtime helper and its value-safe failure hint."""

    program: bytes = field(repr=False)
    failure_hint: str


CredentialPayload = StoredCredential | ManagedHelper


class GitCredentialProvider(Capability):
    """Capability that declares scopes and produces final credential material.

    A provider validates its complete configuration, declares any secret
    references through that model, performs provider-specific runup, and
    translates forge concepts into generic HTTPS scopes. It never writes a
    target user's files or Git configuration.
    """

    owner_kind: ClassVar[str] = "git-credential"

    def __init__(
        self,
        owner_name: str,
        config: Mapping[str, object] | None = None,
        *,
        description: str | None = None,
    ) -> None:
        super().__init__(owner_name, config or {})
        self._description = description

    @property
    def config(self) -> BaseModel:
        """This credential's validated provider-owned configuration."""
        return self._config

    def review_remote(self, url: str) -> list[str]:
        """Return provider-owned advisories for a declared remote URL."""
        return []

    @abstractmethod
    def credential_scopes(self) -> tuple[HttpsCredentialScope, ...]:
        """Return the static HTTPS scopes derived from provider config."""

    def validate_inputs(self, ctx: RunContext) -> None:
        """Validate resolved provider inputs at the pre-mutation boundary."""

    def _probe_static_credential(
        self,
        url: str,
        headers: dict[str, str],
        *,
        secret_name: str,
        reject_statuses: tuple[int, ...],
        host_label: str,
    ) -> tuple[bytes, dict[str, str]] | None:
        """Run one provider-owned authenticated probe of static input."""
        from agentworks import output
        from agentworks.errors import TokenRejectedError

        try:
            status, body, response_headers = _http_probe(url, headers)
        except OSError as exc:
            output.warn(f"could not verify git credential {self.owner_name!r} (network: {exc}); continuing unverified")
            return None
        if status in reject_statuses:
            raise TokenRejectedError(
                f"{host_label} rejected the credential for git credential {self.owner_name!r} (secret {secret_name!r})",
                entity_kind="git-credential",
                entity_name=self.owner_name,
                hint=(
                    "Check the secret's value: expired, revoked, or mistyped? "
                    "Set [defaults] runup_git_credentials = false to skip verification."
                ),
            )
        if status != 200:
            output.warn(
                f"could not verify git credential {self.owner_name!r} "
                f"({host_label} answered {status}); continuing unverified"
            )
            return None
        return (body, response_headers)

    @abstractmethod
    def credential_material(self, ctx: RunContext) -> CredentialPayload:
        """Acquire this provider's final stored credential or helper."""
