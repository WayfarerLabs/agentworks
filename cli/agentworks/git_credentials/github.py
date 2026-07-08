"""GitHub git credential provider -- formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework (Phase 1d); this class formats
the store line and, for scoped credentials (fine-grained PATs), the
gitconfig credential-context sections that select the right credential
per repo (issue #166). Selection rides git's own machinery: a context
section injects a per-credential username (longest-prefix match, slash
boundaries -- verified against git 2.39), and the username-tagged store
line supplies the token. No ``credential.useHttpPath`` anywhere: with
it enabled, path-less store lines stop matching path-carrying queries,
which would break every unscoped credential.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.git_credentials.base import GitCredentialProvider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.reference import ConfigReference

_SCOPE_FIELDS = {"repo", "owner"}


def _validated_scope(
    owner_ctx: str, config: Mapping[str, object]
) -> tuple[str | None, str | None]:
    """Shared shape validation for the github ``provider_config`` blob.

    Returns ``(repo, owner)``; at most one is non-None. Raises
    ``ConfigError`` with ``owner_ctx`` framing on any violation.
    """
    unknown = sorted(set(config) - _SCOPE_FIELDS)
    if unknown:
        raise ConfigError(
            f"{owner_ctx}: unknown github provider field(s): {', '.join(unknown)}"
        )
    repo = config.get("repo")
    org = config.get("owner")
    if repo is not None and org is not None:
        raise ConfigError(
            f"{owner_ctx}: repo and owner are mutually exclusive (a "
            f"fine-grained PAT is scoped to one or the other)"
        )
    if repo is not None:
        if (
            not isinstance(repo, str)
            or repo.count("/") != 1
            or not all(part for part in repo.split("/"))
        ):
            raise ConfigError(
                f'{owner_ctx}.repo must be an "owner/name" string'
            )
    if org is not None and (not isinstance(org, str) or not org or "/" in org):
        raise ConfigError(
            f"{owner_ctx}.owner must be a GitHub user/org name (no slash)"
        )
    return (repo if isinstance(repo, str) else None, org if isinstance(org, str) else None)


class GitHubCredentialProvider(GitCredentialProvider):
    """Configures git credentials for GitHub via a personal access token.

    Optionally scoped via ``provider_config``: ``repo: "owner/name"``
    (a single-repo fine-grained PAT) or ``owner: "org"`` (an
    owner-scoped PAT covering any repo under that owner, including
    repos cloned ad hoc that no workspace declared). Unscoped
    credentials keep the released host-level line verbatim.
    """

    provider_name = "github"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        _validated_scope(owner, config)
        return ()

    def __init__(
        self,
        config_name: str,
        description: str | None = None,
        *,
        repo: str | None = None,
        owner: str | None = None,
    ) -> None:
        super().__init__(config_name, description)
        self._repo = repo
        self._owner = owner

    def credential_lines(self, token: str) -> list[str]:
        if self._repo or self._owner:
            # Scoped: the credential's resource name doubles as the
            # store username -- the join key the gitconfig context
            # sections select by. GitHub ignores the username when the
            # password is a PAT.
            return [f"https://{self._config_name}:{token}@github.com"]
        return [f"https://x-access-token:{token}@github.com"]

    def gitconfig_sections(self) -> list[tuple[str, str]]:
        if self._repo:
            # Cover both remote spellings: agents clone with and
            # without the .git suffix, and context matching is
            # slash-boundary-exact, so "repo" does not prefix-match
            # "repo.git".
            return [
                (f"https://github.com/{self._repo}", self._config_name),
                (f"https://github.com/{self._repo}.git", self._config_name),
            ]
        if self._owner:
            return [(f"https://github.com/{self._owner}/", self._config_name)]
        return []
