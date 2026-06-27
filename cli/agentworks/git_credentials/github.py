"""GitHub git credential provider -- formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework (Phase 1d); this class just
formats the URL line and pre-flights authn.
"""

from __future__ import annotations

from agentworks.git_credentials.base import GitCredentialProvider


class GitHubCredentialProvider(GitCredentialProvider):
    """Configures git credentials for GitHub via a personal access token."""

    def verify_auth(self) -> bool:
        return True

    def auth_hint(self) -> str:
        return "Create a PAT at https://github.com/settings/tokens (repo scope)"

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://x-access-token:{token}@github.com"]
