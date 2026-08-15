"""GitHub git credential provider: formats credentials for ``~/.git-credentials``.

Token resolution lives in the framework; this class formats the store
line and, for scoped credentials (fine-grained PATs), contributes its
scopes (``repos`` / ``owner``) to the generated credential helper that
selects the right credential per repo (issue #166). Selection lives
entirely in that helper: the managed include sets
``credential.useHttpPath = true`` so every query carries the remote
path, and the helper picks the most specific credential (exact repo,
then owner, then the host default). See ``build_credential_materials``
in ``git_credentials/__init__.py`` and ``docs/guides/resources.md`` for
the full model.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.git_credential.base import (
    GitCredentialProvider,
    HelperEntry,
    TokenAcquiringConfig,
)
from agentworks.topics import TopicProse

# GitHub owner/repo name charset. Interpolated verbatim into gitconfig
# section headers and store URLs, so anything outside this set (quotes,
# whitespace, ...) would corrupt the VM's git config at first use, which
# is why the charset is a constraint rather than a courtesy.
_NAME = r"[A-Za-z0-9._-]+"

GitHubName = Annotated[str, Field(pattern=rf"^{_NAME}$")]
"""A GitHub user or organization name."""

GitHubRepo = Annotated[str, Field(pattern=rf"^{_NAME}/{_NAME}$")]
"""A GitHub repository, as ``owner/name``."""


class GitHubConfig(TokenAcquiringConfig):
    """Scope for a GitHub personal access token.

    ``repos`` contributes exact-repository scopes and ``owner`` contributes
    every repository under one owner. When both are present their scopes
    are combined. An unscoped credential declares neither and serves the
    host by default.
    """

    name: Literal["github"]
    """The provider this config is for."""

    repos: list[GitHubRepo] = Field(default_factory=list, examples=[["my-org/my-repo"]])
    """Specific repositories a fine-grained PAT covers, as ``owner/name``.
    Empty means no repository-specific scope."""

    owner: GitHubName | None = Field(default=None, examples=["my-org"])
    """A user or organization whose repositories this credential covers.
    Omit for no owner-wide scope."""


def _parse_expiration(raw: str | None) -> date | None:
    """The header value looks like ``2026-10-01 17:24:32 UTC``; take the
    date prefix, tolerating absence and format drift."""
    if not raw or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


class GitHubCredentialProvider(GitCredentialProvider):
    """Configures git credentials for GitHub via a personal access token.

    Optionally scoped in the ``spec.provider`` table: ``repos: ["owner/name", ...]``
    (the fine-grained PAT's selected repos), ``owner: "org"`` (every repo
    under that owner, including ad hoc clones), or both as the union of
    those scopes. Unscoped credentials keep the released host-level line
    verbatim.
    """

    contract_version: ClassVar[int] = 2
    name: ClassVar[str] = "github"
    description: ClassVar[str] = "GitHub personal access token"
    config_model: ClassVar[type[GitHubConfig]] = GitHubConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="GitHub",
        overview="""
        Authenticates git operations against GitHub with a personal access token, taken
        from the secret this credential names.

        A classic token needs no scoping. A fine-grained token does: `repos` pins the
        credential to specific repositories, and `owner` covers everything under one
        user or organization, including repositories cloned ad hoc that no workspace
        declared. When both are present, the credential covers their union.

        Declaring several credentials is normal. The managed credential helper picks the
        one whose scope matches each remote, and an unscoped credential is the fallback.
        """,
    )

    @property
    def config(self) -> GitHubConfig:
        """This credential's validated github provider config."""
        return self._config_as(GitHubConfig)

    def _verify_token(self, token: str) -> None:
        """Check the PAT against ``GET /user``: 200 announces the login
        and (for fine-grained PATs) the expiry; 401 is a definitive
        rejection; anything else is indeterminate (warn, continue)."""
        import json

        from agentworks import output

        result = self._probe_pat(
            "https://api.github.com/user",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "agentworks",
            },
            reject_statuses=(401,),
            host_label="GitHub",
        )
        if result is None:
            return
        body, headers = result
        login: str | None = None
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("login"), str):
                login = parsed["login"]
        except (ValueError, UnicodeDecodeError):
            pass
        expires = _parse_expiration(headers.get("github-authentication-token-expiration"))
        extras = []
        if login:
            extras.append(f"login {login}")
        if expires is not None:
            extras.append(f"expires {expires.isoformat()}")
        suffix = f" ({', '.join(extras)})" if extras else ""
        output.detail(f"Verified git token for git-credential/{self.owner_name}{suffix}")

    @property
    def store_username(self) -> str:
        # Scoped: the credential's resource name doubles as the store
        # username, the join key the credential helper selects by (GitHub
        # accepts any username with a PAT, verified against fine-grained
        # tokens). Unscoped keeps the released value.
        if self.config.repos or self.config.owner:
            return self.owner_name
        return "x-access-token"

    def review_remote(self, url: str) -> list[str]:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != "github.com":
            return []
        # GitHub's store username is the credential's resource name (what
        # the helper selects by), never something an operator would type.
        # So ANY embedded username makes git hand it to the helper, whose
        # fast path serves by it and skips the helper's path-based
        # per-repo/owner selection.
        if parts.username:
            return [
                f"the git remote {url!r} embeds a username, which overrides "
                f"agentworks git credential scoping for github.com (the helper "
                f"serves by the embedded username); use a plain https remote"
            ]
        return []

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://{self.store_username}:{token}@github.com"]

    def helper_entry(self) -> HelperEntry:
        return HelperEntry(
            host="github.com",
            username=self.store_username,
            repos=tuple(dict.fromkeys(self.config.repos)),
            owner=self.config.owner,
        )
