"""Azure DevOps git credential provider: formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework; this class validates the org,
checks the PAT against the org endpoint at the ``runup`` stage, and
formats the URL line.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentworks.capabilities.git_credential.base import (
    GitCredentialProvider,
    HelperEntry,
    token_config_reference,
)
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.reference import ConfigReference
    from agentworks.secrets.resolver import Resolver


_ORG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class AzDOCredentialProvider(GitCredentialProvider):
    """Configures git credentials for Azure DevOps via a personal access token."""

    name = "azdo"
    description = "Azure DevOps personal access token"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        org = config.get("org")
        if not isinstance(org, str) or not _ORG_RE.match(org):
            raise ConfigError(
                f"{owner}.org is required for the azdo provider and must "
                f"be an organization name (letters, digits, dot, dash, "
                f"underscore); it is interpolated into the generated "
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
        owner_name: str,
        config: Mapping[str, object] | None = None,
        resolver: Resolver | None = None,
        *,
        description: str | None = None,
    ) -> None:
        super().__init__(
            owner_name, config or {}, resolver, description=description
        )
        # validate_config ran at construct and guarantees a str org.
        org = self.config.get("org")
        assert isinstance(org, str)
        self._org = org

    def _verify_token(self, token: str) -> None:
        """Check the PAT against the org's connectionData endpoint: 200
        announces success; 401 (and 203, AzDO's sign-in-page answer for
        bad PATs on some routes) is a definitive rejection; anything
        else is indeterminate (warn, continue)."""
        import base64

        from agentworks import output

        basic = base64.b64encode(f":{token}".encode()).decode()
        result = self._probe_pat(
            f"https://dev.azure.com/{self._org}/_apis/connectionData",
            {
                "Authorization": f"Basic {basic}",
                "Accept": "application/json",
                "User-Agent": "agentworks",
            },
            reject_statuses=(401, 203),
            host_label="Azure DevOps",
        )
        if result is None:
            return
        output.detail(
            f"Verified git token for git-credential/{self.owner_name}", indent=2
        )

    @property
    def store_username(self) -> str:
        return self._org

    def helper_entry(self) -> HelperEntry:
        # The org doubles as the owner scope: AzDO remote paths start
        # with the org segment, so multiple orgs route naturally.
        return HelperEntry(
            host="dev.azure.com", username=self._org, owner=self._org
        )

    def review_remote(self, url: str) -> list[str]:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != "dev.azure.com":
            return []
        # AzDO uses the org as the store username AND the owner scope, so a
        # standard 'https://{org}@dev.azure.com/{org}/...' remote resolves
        # correctly (the embedded org is exactly what the helper serves by).
        # Only a username that is NOT the org bypasses resolution: the helper
        # serves by it and finds no matching line.
        if parts.username and parts.username != self._org:
            return [
                f"the git remote {url!r} embeds username {parts.username!r}, "
                f"not the {self._org!r} org, so the credential helper will not "
                f"serve it; use https://dev.azure.com/{self._org}/... "
                f"(the org prefix is optional)"
            ]
        return []

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://{self._org}:{token}@dev.azure.com/{self._org}"]
