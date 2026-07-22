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

import re
from datetime import date
from typing import TYPE_CHECKING

from agentworks.capabilities.git_credential.base import (
    GitCredentialProvider,
    HelperEntry,
    token_config_reference,
)
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    ``repos`` is always a list in the config (even for one repo, a
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

    name = "github"
    description = "GitHub personal access token"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        _validated_scope(owner, config)
        return (token_config_reference(owner, config),)

    def __init__(
        self,
        owner_name: str,
        config: Mapping[str, object] | None = None,
        *,
        description: str | None = None,
    ) -> None:
        super().__init__(owner_name, config or {}, description=description)
        # Scope shape re-parsed from the bound config (validate_config
        # already ran at construct, so this cannot raise).
        self._repos, self._owner = _validated_scope(
            self._owner_display, self.config
        )

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
        expires = _parse_expiration(
            headers.get("github-authentication-token-expiration")
        )
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
        if self._repos or self._owner:
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
            repos=self._repos,
            owner=self._owner,
        )
