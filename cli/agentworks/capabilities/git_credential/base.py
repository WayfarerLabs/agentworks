"""Base interface for git credential providers.

A git credential provider is a capability (see ``capabilities/README.md``):
it DECLARES the shape of its own config block as a model,
including the secret its token comes from, checks that token against the
host at the post-resolve ``runup`` stage, and produces the credential
materials (``credential_lines`` / ``helper_entry``) as its op. Token
resolution itself lives in the framework: each provider declares a
``SecretReference`` for its token, the active source chain (env-var /
1Password / prompt / ...) resolves it, and the token secret's health
reports through the doctor Secrets group and ``agw secret describe
git-token-<name>`` like any other secret.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import Field

from agentworks.capabilities.base import Capability
from agentworks.schema import (
    AgwModel,
    NonEmptyStr,
    ScalarShorthand,
    SecretRef,
    UnionScalarShorthand,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext


class TokenSourcedConfig(AgwModel):
    """Version 1 provider config, retained only for registration errors.

    Third-party version 1 providers may still import this formerly public
    base. Keeping the old shape importable lets registration reject their
    declared contract version with the migration message. The version 2
    descriptor refuses this base, so a provider cannot claim version 2
    while retaining the outer scalar/null token contract.
    """

    token: Annotated[NonEmptyStr, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]
    """The version 1 secret-name field."""


def _http_probe(url: str, headers: dict[str, str], *, timeout: float = 5.0) -> tuple[int, bytes, dict[str, str]]:
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


class StoredToken(AgwModel):
    """Obtain this credential's token from a stored secret.

    This is deliberately one arm of a real tagged union even though it is
    the only arm today. Minting arrives later as an additive arm; its
    scopes, repositories, permissions, and other creation parameters
    belong to the credential domain, never to this secret reference.
    """

    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="secret")
    """A bare ``token: <name>`` is this arm with ``secret`` filled."""

    mode: Literal["stored"]
    """Selects stored-secret token acquisition."""

    secret: Annotated[NonEmptyStr, SecretRef(usage="the auth token", default_template="git-token-{owner_name}")]
    """The secret holding this credential's personal access token."""


TokenAcquisition = Annotated[
    StoredToken,
    UnionScalarShorthand(discriminator="mode", arm=StoredToken),
]
"""How a git credential obtains its token, as a one-arm tagged union."""


class TokenAcquiringConfig(AgwModel):
    """The config every token-acquiring git credential provider shares."""

    # Stored is the only legal default because omission historically
    # sourced a stored secret. A future arm must be operator-selected and
    # must never replace this default. Raw data is intentional: the owner
    # boundary fills the stored arm's templated ``secret`` before pydantic
    # validates it, which a constructed instance could not express.
    token: TokenAcquisition = Field(default={"mode": "stored"})  # type: ignore[assignment]
    """How this credential obtains its token. Defaults to the stored arm;
    a bare secret name is that arm's shorthand."""


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
    resource names a provider in one tagged ``spec.provider`` table and
    the rest of that table IS this config, so the instance does the real
    work. It is constructed by the composition roots as
    ``cls(credential_name, config, description=...)``, with the
    capability's OWN keys and not the tag: bound to one declared
    credential, never
    resolved secret values (see the ``Capability`` lifecycle). The
    declared token secret joins the operation's boundary union through
    the holding node's ``secret_refs`` and its value arrives through
    the context at ``runup`` / op time.

    Subclasses (``GitHubCredentialProvider``, ``AzDOCredentialProvider``)
    declare their ``config_model`` (the token secret and any scope
    fields), implement ``_verify_token`` (the authenticated probe), and
    implement the ops ``helper_entry`` / ``credential_lines``.
    """

    owner_kind: ClassVar[str] = "git-credential"

    def __init__(
        self,
        owner_name: str,
        config: Mapping[str, object] | None = None,
        *,
        description: str | None = None,
    ) -> None:
        # An omitted config is an EMPTY one, not a missing one: an
        # unscoped credential writes nothing but the tag and its token
        # secret comes from the owner template. Defaulted here
        # rather than on each provider, which is where the two shipped
        # ones used to spell it.
        super().__init__(owner_name, config or {})
        # Display sugar for the consuming resource's name; not part of
        # the capability's config (which is the tagged table's other keys).
        self._description = description

    @property
    def config(self) -> TokenAcquiringConfig:
        """This credential's validated provider config."""
        return self._config_as(TokenAcquiringConfig)

    @property
    def secret_name(self) -> str:
        """The token secret this credential sources its PAT from
        (default ``git-token-<name>``). Named by the helper's rejection
        diagnosis and read from the context at ``runup``.

        A plain field read: the model layer resolved the default at
        validation, so there is nothing left here to fall back to. The
        fallback this replaced was the last consumer-side defaulting of a
        modeled field on this path (FR15).
        """
        return self.config.token.secret

    @property
    def store_username(self) -> str:
        """The username on this credential's store line, the join key the
        credential helper selects by. Default: the credential's own name;
        subclasses override where the host dictates otherwise."""
        return self.owner_name

    def runup(self, ctx: RunContext) -> None:
        """Authenticated readiness (the ``runup`` lifecycle stage):
        confirm the resolved PAT authorizes against the host before it is
        written to any VM.

        Post-resolve and read-only: it reads the token from the context's
        resolved secrets (``ctx.secret(name)``) and does a single
        authenticated GET. A definitive rejection raises
        ``TokenRejectedError`` (safe: runup runs before any VM/user
        mutation); network indeterminacy or any other non-success warns
        and continues unverified, so a transient outage never blocks work
        a valid token would have done. Operators skip this whole stage via
        the composition root (which gates the call on ``[defaults]``); it
        is not this method's job to consult that flag. A context with no
        resolved secrets at all (inspection only?) is the accessor's
        typed ``ConfigError``.
        """
        self._verify_token(ctx.secret(self.secret_name))

    def review_remote(self, url: str) -> list[str]:
        """Advisory review of a declared repo remote URL against THIS
        credential's resolution semantics. Config-only: no token, no
        network, no per-user wiring; it reads only this instance's own
        config (its host and scope), which is exactly why the judgment
        lives on the instance and not in core config, only the instance
        knows its host (including a future enterprise host) and how it
        selects credentials.

        Return advisory strings when the URL is served by this
        credential's host and something about it would defeat resolution;
        return ``[]`` when the URL is not this credential's concern or is
        fine. The default abstains; providers override with their own
        host and username semantics.
        """
        return []

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
            output.warn(f"could not verify git credential {self.owner_name!r} (network: {exc}); continuing unverified")
            return None
        if status in reject_statuses:
            raise TokenRejectedError(
                f"{host_label} rejected the token for git credential {self.owner_name!r} (secret {self.secret_name!r})",
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
