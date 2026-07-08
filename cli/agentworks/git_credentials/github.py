"""GitHub git credential provider -- formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework (Phase 1d); this class just
formats the URL line.
"""

from __future__ import annotations

from agentworks.git_credentials.base import GitCredentialProvider


class GitHubCredentialProvider(GitCredentialProvider):
    """Configures git credentials for GitHub via a personal access token."""

    provider_name = "github"

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://x-access-token:{token}@github.com"]
