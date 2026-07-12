"""Azure DevOps git credential provider -- formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework (Phase 1d); this class just
formats the URL line.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.git_credentials.base import (
    GitCredentialProvider,
    HelperEntry,
    TokenInfo,
    token_config_reference,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.reference import ConfigReference


_ORG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class AzDOCredentialProvider(GitCredentialProvider):
    """Configures git credentials for Azure DevOps via a personal access token."""

    provider_name = "azdo"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        org = config.get("org")
        if (
            not isinstance(org, str)
            or not _ORG_RE.match(org)
        ):
            raise ConfigError(
                f"{owner}.org is required for the azdo provider and must "
                f"be an organization name (letters, digits, dot, dash, "
                f"underscore) -- it is interpolated into the generated "
                f"credential helper"
            )
        unknown = sorted(set(config) - {"org", "token"})
        if unknown:
            raise ConfigError(
                f"{owner}: unknown azdo provider field(s): {', '.join(unknown)}"
            )
        return (token_config_reference(owner, config),)

    def __init__(
        self,
        config_name: str,
        org: str,
        description: str | None = None,
        *,
        secret_name: str | None = None,
    ) -> None:
        super().__init__(config_name, description=description, secret_name=secret_name)
        self._org = org

    def acquire_token(
        self, secrets: Mapping[str, str], *, verify: bool = True
    ) -> TokenInfo:
        """Read the PAT from ``secrets`` and (when ``verify``) check it
        against the org's connectionData endpoint.

        200 -> verified. 401 (and 203, AzDO's sign-in-page answer for
        bad PATs on some routes) -> definitive rejection. Anything else
        -> indeterminate: warn, return unverified. ``verify=False``
        returns the token unverified with no network call.
        """
        import base64

        from agentworks import output
        from agentworks.errors import TokenRejectedError
        from agentworks.git_credentials.base import _http_probe

        resolved_secret = secrets[self.secret_name]
        if not verify:
            return TokenInfo(token=resolved_secret)
        basic = base64.b64encode(f":{resolved_secret}".encode()).decode()
        try:
            status, _body, _headers = _http_probe(
                f"https://dev.azure.com/{self._org}/_apis/connectionData",
                {
                    "Authorization": f"Basic {basic}",
                    "Accept": "application/json",
                    "User-Agent": "agentworks",
                },
            )
        except OSError as exc:
            output.warn(
                f"could not verify git credential {self._config_name!r} "
                f"(network: {exc}); continuing unverified"
            )
            return TokenInfo(token=resolved_secret)
        if status in (401, 203):
            raise TokenRejectedError(
                f"Azure DevOps rejected the token for git credential "
                f"{self._config_name!r} (secret {self.secret_name!r})",
                entity_kind="git-credential",
                entity_name=self._config_name,
                hint=(
                    "Check the secret's value: expired, revoked, or "
                    "mistyped? Set [defaults] verify_git_tokens = false "
                    "to skip verification."
                ),
            )
        if status != 200:
            output.warn(
                f"could not verify git credential {self._config_name!r} "
                f"(Azure DevOps answered {status}); continuing unverified"
            )
            return TokenInfo(token=resolved_secret)
        return TokenInfo(token=resolved_secret, verified=True)

    @property
    def store_username(self) -> str:
        return self._org

    def helper_entry(self) -> HelperEntry:
        # The org doubles as the owner scope: AzDO remote paths start
        # with the org segment, so multiple orgs route naturally.
        return HelperEntry(
            host="dev.azure.com", username=self._org, owner=self._org
        )

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://{self._org}:{token}@dev.azure.com/{self._org}"]
