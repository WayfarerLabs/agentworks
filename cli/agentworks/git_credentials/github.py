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

import re
from datetime import date
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError
from agentworks.git_credentials.base import (
    GitCredentialProvider,
    HelperEntry,
    TokenInfo,
    token_config_reference,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.resources.reference import ConfigReference

_SCOPE_FIELDS = {"repos", "owner", "token"}

# GitHub owner/repo name charset. Interpolated verbatim into gitconfig
# section headers and store URLs, so anything outside this set (quotes,
# whitespace, ...) would corrupt the VM's git config at first use.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validated_scope(
    owner_ctx: str, config: Mapping[str, object]
) -> tuple[tuple[str, ...], str | None]:
    """Shared shape validation for the github ``provider_config`` blob.

    Returns ``(repos, owner)``; at most one is non-empty/non-None.
    ``repos`` is always a list in the config (even for one repo -- a
    fine-grained PAT may cover several selected repos, and the plural
    field makes that visible). Raises ``ConfigError`` with
    ``owner_ctx`` framing on any violation.
    """
    unknown = sorted(set(config) - _SCOPE_FIELDS)
    if unknown == ["repo"]:
        raise ConfigError(
            f"{owner_ctx}: unknown github provider field 'repo'; the "
            f"field is 'repos' (a list, even for one repo)"
        )
    if unknown:
        raise ConfigError(
            f"{owner_ctx}: unknown github provider field(s): {', '.join(unknown)}"
        )
    repos_raw = config.get("repos")
    org = config.get("owner")
    if repos_raw is not None and org is not None:
        raise ConfigError(
            f"{owner_ctx}: repos and owner are mutually exclusive (a "
            f"fine-grained PAT is scoped to one or the other)"
        )

    def _valid_repo(value: object) -> bool:
        return (
            isinstance(value, str)
            and value.count("/") == 1
            and all(_NAME_RE.match(part) for part in value.split("/"))
        )

    repos: tuple[str, ...] = ()
    if repos_raw is not None:
        if (
            not isinstance(repos_raw, list)
            or not repos_raw
            or not all(_valid_repo(entry) for entry in repos_raw)
        ):
            raise ConfigError(
                f'{owner_ctx}.repos must be a non-empty list of '
                f'"owner/name" strings (GitHub name characters only)'
            )
        repos = tuple(dict.fromkeys(repos_raw))  # preserve order, drop repeats
    if org is not None and (not isinstance(org, str) or not _NAME_RE.match(org)):
        raise ConfigError(
            f"{owner_ctx}.owner must be a GitHub user/org name (no slash)"
        )
    return (repos, org if isinstance(org, str) else None)


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

    Optionally scoped via ``provider_config``: ``repos: ["owner/name", ...]``
    (the fine-grained PAT's selected repos) or ``owner: "org"`` (an
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
        return (token_config_reference(owner, config),)

    def __init__(
        self,
        config_name: str,
        description: str | None = None,
        *,
        secret_name: str | None = None,
        repos: Sequence[str] = (),
        owner: str | None = None,
    ) -> None:
        super().__init__(config_name, description, secret_name=secret_name)
        self._repos = tuple(repos)
        self._owner = owner

    def acquire_token(
        self, secrets: Mapping[str, str], *, verify: bool = True
    ) -> TokenInfo:
        """Read the PAT from ``secrets`` and (when ``verify``) check it
        against ``GET /user``.

        200 -> verified TokenInfo enriched with the login and (for
        fine-grained PATs) the ``github-authentication-token-expiration``
        header. 401 -> definitive rejection. Anything else (rate
        limits, 5xx, network failure) -> indeterminate: warn, return
        unverified. ``verify=False`` returns the token unverified with
        no network call.
        """
        import json

        from agentworks import output
        from agentworks.errors import TokenRejectedError
        from agentworks.git_credentials.base import _http_probe

        resolved_secret = secrets[self.secret_name]
        if not verify:
            return TokenInfo(token=resolved_secret)
        try:
            status, body, headers = _http_probe(
                "https://api.github.com/user",
                {
                    "Authorization": f"Bearer {resolved_secret}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "agentworks",
                },
            )
        except OSError as exc:
            output.warn(
                f"could not verify git credential {self._config_name!r} "
                f"(network: {exc}); continuing unverified"
            )
            return TokenInfo(token=resolved_secret)
        if status == 401:
            raise TokenRejectedError(
                f"GitHub rejected the token for git credential "
                f"{self._config_name!r} (secret {self.secret_name!r})",
                entity_kind="git-credential",
                entity_name=self._config_name,
                hint=(
                    "Check the secret's value: expired, revoked, or "
                    "mistyped? Set [defaults] verify_git_tokens = false "
                    "to skip verification."
                ),
            )
        if status != 200:
            output.warn(
                f"could not verify git credential {self._config_name!r} "
                f"(GitHub answered {status}); continuing unverified"
            )
            return TokenInfo(token=resolved_secret)
        login: str | None = None
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("login"), str):
                login = parsed["login"]
        except (ValueError, UnicodeDecodeError):
            pass
        expires = _parse_expiration(
            headers.get("github-authentication-token-expiration")
        )
        return TokenInfo(
            token=resolved_secret, login=login, expires_at=expires, verified=True
        )

    @property
    def store_username(self) -> str:
        # Scoped: the credential's resource name doubles as the store
        # username -- the join key the gitconfig context sections select
        # by (GitHub accepts any username with a PAT, verified against
        # fine-grained tokens). Unscoped keeps the released value.
        if self._repos or self._owner:
            return self._config_name
        return "x-access-token"

    def credential_lines(self, token: str) -> list[str]:
        return [f"https://{self.store_username}:{token}@github.com"]

    def helper_entry(self) -> HelperEntry:
        return HelperEntry(
            host="github.com",
            username=self.store_username,
            repos=self._repos,
            owner=self._owner,
        )
