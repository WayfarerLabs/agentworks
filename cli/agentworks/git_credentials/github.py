"""GitHub git credential provider -- prompt for a personal access token."""

from __future__ import annotations

from agentworks import output
from agentworks.git_credentials.base import GitCredentialProvider


class GitHubCredentialProvider(GitCredentialProvider):
    """Configures git credentials for GitHub via a personal access token."""

    def verify_auth(self) -> bool:
        return True

    def auth_hint(self) -> str:
        return "Create a PAT at https://github.com/settings/tokens (repo scope)"

    def _prompt_token(self, vm_name: str) -> str:
        return output.prompt_secret(f"  GitHub PAT for '{self.display_name}'", hint=self.auth_hint())

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://x-access-token:{token}@github.com"]
