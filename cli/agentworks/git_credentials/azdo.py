"""Azure DevOps git credential provider -- formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework (Phase 1d); this class just
formats the URL line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.git_credentials.base import GitCredentialProvider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.reference import ConfigReference


class AzDOCredentialProvider(GitCredentialProvider):
    """Configures git credentials for Azure DevOps via a personal access token."""

    provider_name = "azdo"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        org = config.get("org")
        if not isinstance(org, str) or not org:
            raise ConfigError(
                f"{owner}.org is required for the azdo provider and must "
                f"be a non-empty string"
            )
        unknown = sorted(set(config) - {"org"})
        if unknown:
            raise ConfigError(
                f"{owner}: unknown azdo provider field(s): {', '.join(unknown)}"
            )
        return ()

    def __init__(self, config_name: str, org: str, description: str | None = None) -> None:
        super().__init__(config_name, description=description)
        self._org = org

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://{self._org}:{token}@dev.azure.com/{self._org}"]
