"""Base interface for git credential providers.

A git credential provider is a capability (see ``capabilities/README.md``):
it validates its own ``provider_config`` block (``validate_config``),
declares the secret its token comes from, checks that token against the
host at the post-resolve ``runup`` stage, and produces the credential
materials (``credential_lines`` / ``helper_entry``) as its op. Token
resolution itself lives in the framework: each provider declares a
``SecretReference`` for its token, the active backend chain (env-var /
1Password / prompt / ...) resolves it, and the token secret's health
reports through the doctor Secrets group and ``agw secret describe
git-token-<name>`` like any other secret.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from agentworks.capabilities.base import Capability

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.resources.reference import ConfigReference
    from agentworks.secrets.resolver import Resolver


def _http_probe(
    url: str, headers: dict[str, str], *, timeout: float = 5.0
) -> tuple[int, bytes, dict[str, str]]:
    """GET ``url``; returns (status, body, lowercased-headers).

    HTTP error statuses are returned, not raised; network-level
    failures raise ``OSError`` (URLError subclasses it) for the caller
    to treat as indeterminate.
    """
    from urllib import error, request

    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return (
                resp.status,
                resp.read(),
                {k.lower(): v for k, v in resp.headers.items()},
            )
    except error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return (exc.code, body, {k.lower(): v for k, v in exc.headers.items()})


def credential_name_from_owner(owner: str) -> str:
    """The credential name from a standardized ``git-credential/<name>``
    owner. Resource names cannot contain ``/`` (FRD R13), so the split
    is exact."""
    return owner.split("/", 1)[1] if "/" in owner else owner


def default_token_secret(credential_name: str) -> str:
    """The per-credential default token secret name."""
    return f"git-token-{credential_name}"


def token_config_reference(
    owner: str, config: Mapping[str, object]
) -> ConfigReference:
    """The token-secret reference a token-sourcing provider implies from
    its ``provider_config``: the ``token`` field names the secret
    (default ``git-token-<name>``). Shared by github and azdo (both
    source a PAT from a mapped secret today). A minting provider would
    instead declare its bootstrap secret(s) here (or none).
    """
    from agentworks.errors import ConfigError
    from agentworks.resources.reference import ConfigReference

    raw = config.get("token")
    if raw is not None and (not isinstance(raw, str) or not raw):
        raise ConfigError(
            f"{owner}.token must be a non-empty secret name (a string)"
        )
    name = raw if isinstance(raw, str) and raw else default_token_secret(
        credential_name_from_owner(owner)
    )
    return ConfigReference(kind="secret", name=name, usage="the auth token")


@dataclass(frozen=True)
class HelperEntry:
    """What the credential helper needs to select this credential by
    remote URL: the host it serves, the username on its store line
    (the key back into the managed store file), and its scopes --
    ``repos`` match the remote path exactly, ``owner`` matches its
    first segment. No scopes = the host's default candidate.
    """

    host: str
    username: str
    repos: tuple[str, ...] = ()
    owner: str | None = None


class GitCredentialProvider(Capability):
    """Capability: configures git credentials for one host on VMs.

    A thin-wrapper capability (``git-credential`` over
    ``git-credential-provider``): the ``git-credential`` consuming
    resource names a provider and supplies its ``provider_config``, and
    the instance does the real work. It is constructed by the site/agent
    composition roots as ``cls(credential_name, provider_config,
    resolver, description=...)``: bound to one declared credential plus
    the operation's resolver (never resolved secret values; see the
    ``Capability`` lifecycle). The declared token secret registers on the
    resolver at construct and its value arrives via the operation's
    single resolve pass at the preflight boundary.

    Subclasses (``GitHubCredentialProvider``, ``AzDOCredentialProvider``)
    override ``validate_config`` (declaring the token secret and any
    scope shape), ``_verify_token`` (the authenticated probe), and the
    ops ``helper_entry`` / ``credential_lines``.
    """

    owner_kind: ClassVar[str] = "git-credential"

    def __init__(
        self,
        owner_name: str,
        config: Mapping[str, object],
        resolver: Resolver | None = None,
        *,
        description: str | None = None,
    ) -> None:
        super().__init__(owner_name, config, resolver)
        # Display sugar for the consuming resource's name; not part of
        # the capability's config (which is provider_config alone).
        self._description = description

    @property
    def secret_name(self) -> str:
        """The token secret this credential sources its PAT from: the
        one secret its ``validate_config`` declared (default
        ``git-token-<name>``). Named by the helper's rejection
        diagnosis and read from the resolver at ``runup``."""
        if self._secret_refs:
            return self._secret_refs[0].name
        return default_token_secret(self.owner_name)

    @property
    def store_username(self) -> str:
        """The username on this credential's store line, the join key
        the credential helper and context sections select by. Default:
        the credential's own name; subclasses override where the host
        dictates otherwise."""
        return self.owner_name

    @property
    def display_name(self) -> str:
        """Human-readable name: 'key (description)' or just 'key'."""
        if self._description:
            return f"{self.owner_name} ({self._description})"
        return self.owner_name

    def runup(self, ctx: RunContext) -> None:
        """Authenticated readiness (the ``runup`` lifecycle stage):
        confirm the resolved PAT authorizes against the host before it is
        written to any VM.

        Post-resolve and read-only: it reads the token from the context's
        resolved secrets (``ctx.secrets``) and does a single authenticated
        GET. A definitive rejection raises ``TokenRejectedError`` (safe:
        runup runs before any VM/user mutation); network indeterminacy or
        any other non-success warns and continues unverified, so a
        transient outage never blocks work a valid token would have done.
        Operators skip this whole stage via the composition root (which
        gates the call on ``[defaults]``); it is not this method's job to
        consult that flag.
        """
        from agentworks.errors import ConfigError

        if ctx.secrets is None:
            raise ConfigError(
                f"{self._owner_display}: cannot check the token without "
                f"resolved secrets in the run context (inspection only?)"
            )
        self._verify_token(ctx.secrets.get(self.secret_name))

    def _probe_pat(
        self,
        url: str,
        headers: dict[str, str],
        *,
        reject_statuses: tuple[int, ...],
        host_label: str,
    ) -> tuple[bytes, dict[str, str]] | None:
        """Shared authenticated probe for a PAT-sourcing provider.

        Returns ``(body, lowercased-headers)`` on HTTP 200. Raises
        ``TokenRejectedError`` on a ``reject_statuses`` code (a
        definitive rejection). Returns ``None`` after warning on network
        indeterminacy or any other non-200 (the token is unconfirmed,
        not known-bad).
        """
        from agentworks import output
        from agentworks.errors import TokenRejectedError

        try:
            status, body, resp_headers = _http_probe(url, headers)
        except OSError as exc:
            output.warn(
                f"could not verify git credential {self.owner_name!r} "
                f"(network: {exc}); continuing unverified"
            )
            return None
        if status in reject_statuses:
            raise TokenRejectedError(
                f"{host_label} rejected the token for git credential "
                f"{self.owner_name!r} (secret {self.secret_name!r})",
                entity_kind="git-credential",
                entity_name=self.owner_name,
                hint=(
                    "Check the secret's value: expired, revoked, or "
                    "mistyped? Set [defaults] runup_git_credentials = false "
                    "to skip verification."
                ),
            )
        if status != 200:
            output.warn(
                f"could not verify git credential {self.owner_name!r} "
                f"({host_label} answered {status}); continuing unverified"
            )
            return None
        return (body, resp_headers)

    @abstractmethod
    def _verify_token(self, token: str) -> None:
        """Authenticated probe of the resolved ``token`` (via
        :meth:`_probe_pat`). Raise ``TokenRejectedError`` on definitive
        rejection; warn (never raise) on indeterminacy; announce success
        with any enrichment (login, expiry)."""

    @abstractmethod
    def helper_entry(self) -> HelperEntry:
        """This credential's selection entry for the generated helper.

        The helper receives (host, path) per query (``useHttpPath``
        is set globally in the managed include), and picks the most
        specific credential: exact repo, then owner (first path
        segment), then the host's default (an entry without scopes),
        then the first store line for the host (legacy semantics, which
        also keeps ``vm add-git-credential`` additions serving).
        """

    @abstractmethod
    def credential_lines(self, token: str) -> list[str]:
        """Return lines for ~/.git-credentials.

        Each line is a URL in the format: https://user:token@host
        """
