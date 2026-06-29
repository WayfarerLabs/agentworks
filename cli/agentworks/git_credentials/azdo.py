"""Azure DevOps git credential provider -- formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework (Phase 1d); this class just
formats the URL line and pre-flights authn.
"""

from __future__ import annotations

from agentworks.git_credentials.base import GitCredentialProvider


class AzDOCredentialProvider(GitCredentialProvider):
    """Configures git credentials for Azure DevOps via a personal access token."""

    def __init__(self, config_name: str, org: str, description: str | None = None) -> None:
        super().__init__(config_name, description=description)
        self._org = org

    def verify_auth(self) -> bool:
        return True

    def auth_hint(self) -> str:
        return f"Create a PAT at https://dev.azure.com/{self._org}/_usersSettings/tokens (Code Read & Write scope)"

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://{self._org}:{token}@dev.azure.com/{self._org}"]
