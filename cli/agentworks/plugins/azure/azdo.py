"""Azure DevOps git credential provider: formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework; this class validates the org,
checks the PAT against the org endpoint at the ``runup`` stage, and
formats the URL line.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.git_credential.base import (
    GitCredentialProvider,
    HelperEntry,
    TokenSourcedConfig,
)

AzDOOrg = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+$")]
"""An Azure DevOps organization name. Constrained because it is
interpolated into the generated credential helper and into the store
URL, so anything outside this charset would corrupt them."""


class AzDOConfig(TokenSourcedConfig):
    """Scope for an Azure DevOps personal access token."""

    name: Literal["azdo"]
    """The provider this config is for."""

    org: AzDOOrg
    """The Azure DevOps organization this credential serves."""


class AzDOCredentialProvider(GitCredentialProvider):
    """Configures git credentials for Azure DevOps via a personal access token."""

    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "azdo"
    description: ClassVar[str] = "Azure DevOps personal access token"
    config_model: ClassVar[type[AzDOConfig]] = AzDOConfig

    @property
    def config(self) -> AzDOConfig:
        """This credential's validated azdo provider config."""
        return self._config_as(AzDOConfig)

    def _verify_token(self, token: str) -> None:
        """Check the PAT against the org's connectionData endpoint: 200
        announces success; 401 (and 203, AzDO's sign-in-page answer for
        bad PATs on some routes) is a definitive rejection; anything
        else is indeterminate (warn, continue)."""
        import base64

        from agentworks import output

        basic = base64.b64encode(f":{token}".encode()).decode()
        result = self._probe_pat(
            f"https://dev.azure.com/{self.config.org}/_apis/connectionData",
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
        output.detail(f"Verified git token for git-credential/{self.owner_name}")

    @property
    def store_username(self) -> str:
        return self.config.org

    def helper_entry(self) -> HelperEntry:
        # The org doubles as the owner scope: AzDO remote paths start
        # with the org segment, so multiple orgs route naturally.
        return HelperEntry(host="dev.azure.com", username=self.config.org, owner=self.config.org)

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
        if parts.username and parts.username != self.config.org:
            return [
                f"the git remote {url!r} embeds username {parts.username!r}, "
                f"not the {self.config.org!r} org, so the credential helper will not "
                f"serve it; use https://dev.azure.com/{self.config.org}/... "
                f"(the org prefix is optional)"
            ]
        return []

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://{self.config.org}:{token}@dev.azure.com/{self.config.org}"]
