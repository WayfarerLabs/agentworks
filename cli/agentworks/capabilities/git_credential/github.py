"""GitHub Git credential provider."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.git_credential.base import (
    CredentialPayload,
    GitCredentialProvider,
    HttpsCredentialScope,
    ManagedHelper,
    StoredCredential,
    require_line_safe_credential_input,
)
from agentworks.schema import AgwModel, NonEmptyStr, SecretRef
from agentworks.topics import TopicProse

_NAME = r"[A-Za-z0-9._-]+"

GitHubName = Annotated[str, Field(pattern=rf"^{_NAME}$")]
"""A GitHub user or organization name."""

GitHubRepo = Annotated[str, Field(pattern=rf"^{_NAME}/{_NAME}$")]
"""A GitHub repository as ``owner/name``."""


class GitHubSecretSource(AgwModel):
    """Acquire the final credential from a declared secret."""

    mode: Literal["secret"]
    """Resolve provider input through Agentworks' declared secret system."""
    secret: Annotated[
        NonEmptyStr,
        SecretRef(usage="the GitHub credential input", default_template="git-token-{owner_name}"),
    ]
    """The secret input this provider reads; defaults to `git-token-<credential-name>`."""


class GitHubCliSource(AgwModel):
    """Acquire a fresh credential from the target user's GitHub CLI."""

    mode: Literal["gh-cli"]
    """Acquire each credential from the target user's active GitHub CLI identity."""


GitHubSource = Annotated[GitHubSecretSource | GitHubCliSource, Field(discriminator="mode")]


class GitHubConfig(AgwModel):
    """GitHub credential acquisition and HTTPS scope."""

    name: Literal["github"]
    """Select the built-in GitHub credential provider."""
    source: GitHubSource
    """How this provider acquires its credential."""
    repos: list[GitHubRepo] = Field(default_factory=list, examples=[["my-org/my-repo"]])
    """Exact `owner/repository` scopes this credential may serve."""
    owner: GitHubName | None = Field(default=None, examples=["my-org"])
    """An optional user or organization scope covering its repositories."""


def _parse_expiration(raw: str | None) -> date | None:
    if not raw or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


_GH_HELPER = b"""#!/bin/sh
set -u
command -v gh >/dev/null 2>&1 || exit 1
token=$(GH_PROMPT_DISABLED=1 gh auth token --hostname github.com 2>/dev/null) || exit 1
case "$token" in
    ''|*'
'*) exit 1 ;;
esac
printf 'username=x-access-token\\npassword=%s\\n\\n' "$token"
"""

_GH_FAILURE_HINT = (
    "agentworks: GitHub CLI credential acquisition failed; check that 'gh' is installed and on PATH "
    "for this user, then authenticate the intended github.com identity"
)

_GH_READINESS = """
command -v gh >/dev/null 2>&1 || exit 20
GH_PROMPT_DISABLED=1 gh auth status --active --hostname github.com >/dev/null 2>&1 || exit 21
"""


class GitHubCredentialProvider(GitCredentialProvider):
    """Produces stored or GitHub-CLI-backed credentials for github.com."""

    contract_version: ClassVar[int] = 3
    name: ClassVar[str] = "github"
    description: ClassVar[str] = "GitHub HTTPS credentials from a secret or GitHub CLI"
    config_model: ClassVar[type[GitHubConfig]] = GitHubConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="GitHub",
        overview="""
        Authenticates HTTPS git operations against GitHub. An explicit `source`
        selects either a declared secret or the active target-user GitHub CLI identity.
        Enabled runup checks that CLI identity read-only and warns without blocking
        helper installation.

        `repos` pins the credential to specific repositories, while `owner` covers every
        repository under one user or organization. An unscoped credential is the
        github.com fallback.
        """,
    )

    @property
    def config(self) -> GitHubConfig:
        return self._config_as(GitHubConfig)

    def credential_scopes(self) -> tuple[HttpsCredentialScope, ...]:
        paths = [tuple(repo.split("/", 1)) for repo in self.config.repos]
        if self.config.owner is not None:
            paths.append((self.config.owner,))
        if not paths:
            paths.append(())
        return tuple(HttpsCredentialScope("github.com", path) for path in dict.fromkeys(paths))

    @property
    def _username(self) -> str:
        return self.owner_name if self.config.repos or self.config.owner else "x-access-token"

    def validate_inputs(self, ctx: RunContext) -> None:
        source = self.config.source
        if isinstance(source, GitHubSecretSource):
            self._secret_input(ctx, source)

    @staticmethod
    def _secret_input(ctx: RunContext, source: GitHubSecretSource) -> str:
        return require_line_safe_credential_input(ctx.secret(source.secret), secret_name=source.secret)

    def runup(self, ctx: RunContext) -> None:
        source = self.config.source
        if isinstance(source, GitHubCliSource):
            self._check_cli_readiness(ctx)
            return
        token = self._secret_input(ctx, source)
        self._verify_token(token, secret_name=source.secret)

    def _check_cli_readiness(self, ctx: RunContext) -> None:
        from agentworks import output
        from agentworks.errors import StateError
        from agentworks.ssh import SSHError

        target = ctx.admin_target() or ctx.agent_target()
        if target is None:
            raise StateError("GitHub CLI runup requires a current user target")
        try:
            result = target.run(_GH_READINESS, check=False, timeout=10)
        except SSHError:
            output.warn(f"Could not check GitHub CLI readiness for git-credential/{self.owner_name}")
            return
        if result.returncode == 0:
            output.detail(f"Verified GitHub CLI readiness for git-credential/{self.owner_name}")
        elif result.returncode == 20:
            output.warn(
                f"GitHub CLI credentials are currently unavailable for git-credential/{self.owner_name}; "
                "the target user must install 'gh' on PATH and run 'gh auth login'"
            )
        else:
            output.warn(
                f"GitHub CLI credentials are currently unavailable for git-credential/{self.owner_name}; "
                "the target user must run 'gh auth login' for the intended active github.com identity"
            )

    def _verify_token(self, token: str, *, secret_name: str) -> None:
        import json

        from agentworks import output

        result = self._probe_static_credential(
            "https://api.github.com/user",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "agentworks",
            },
            secret_name=secret_name,
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

    def credential_material(self, ctx: RunContext) -> CredentialPayload:
        source = self.config.source
        if isinstance(source, GitHubSecretSource):
            password = self._secret_input(ctx, source)
            payload: StoredCredential | ManagedHelper = StoredCredential(self._username, password)
        else:
            payload = ManagedHelper(_GH_HELPER, _GH_FAILURE_HINT)
        return payload

    def review_remote(self, url: str) -> list[str]:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != "github.com":
            return []
        if parts.username:
            return [
                f"the git remote {url!r} embeds a username; use a plain HTTPS remote "
                "so Agentworks can select the configured github.com credential by path"
            ]
        return []


if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
