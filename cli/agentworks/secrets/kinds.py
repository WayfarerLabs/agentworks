"""Framework strategy for the declarable ``secret`` resource kind.

``SecretDecl`` lives in :mod:`agentworks.secrets.base`; the capability-owned
``secret-backend`` strategy lives beside its implementation contract under
:mod:`agentworks.capabilities.secret_backend.kinds`.

``SecretKind`` uses the ``auto-declare`` miss policy with no name
restriction: any name a ``SecretReference`` references is
auto-synthesized when not operator-declared. The synthesized
``SecretDecl`` carries an empty ``description``; operators are warned
that auto-declared secrets should be promoted to explicit
``secret`` manifests so they can carry a description.

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.origin import Origin
from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError
from agentworks.secrets.base import SecretDecl
from agentworks.secrets.sources import SECRET_SOURCE_KIND_NAME, SecretSourceDecl
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.declared_resource import DeclaredResource
    from agentworks.errors import ConfigError
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry


SECRET_KIND_NAME = "secret"
"""Single source of truth for the ``"secret"`` kind identifier. Callers
that need to render or compare against the kind name import this rather
than re-typing the literal; a hypothetical rename then flows through
every site by construction."""


@dataclass(frozen=True)
class _SecretKind:
    """Implementation of ``ResourceKind`` for ``"secret"``. Module-private;
    callers reach this through ``KIND_REGISTRY["secret"]``.
    """

    kind: str = SECRET_KIND_NAME
    model: type[DeclaredResource] = SecretDecl
    description: str = "Declared secrets and their backend mappings"
    prose: TopicProse = TopicProse(
        title="Secrets",
        overview="""
        A secret is a NAME, not a value. Declaring one says a value by that name exists,
        what it is for, and (optionally) what each configured source calls it; the value
        itself is produced through that source at command time and never stored by
        agentworks.

        Anything that needs a secret refers to it by name: an `env` table writes
        `{secret: npm-token}`, and a capability config field that names a secret (a git
        credential's token, a platform's client secret) takes the name too. A referenced
        secret that nothing declared is auto-declared, so declaring one is how you give
        it a description and a hint, which are the text an operator reads when they are
        asked to type the value in.

        Every `backend_mappings` key is a secret-source name. Its value overrides what
        that source calls this secret, or `false` opts out of the source entirely. Run
        `agw resource list --kind secret-source` to see the configured sources.
        """,
    )
    miss_policy: Literal["auto-declare", "error"] = "auto-declare"
    auto_declare_names: frozenset[str] | None = None  # None = any name accepted
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> SecretDecl:
        """Build a ``SecretDecl`` for an auto-declared secret. ``references``
        is non-empty in normal operation (the Registry calls ``synthesize``
        only when an incoming reference triggered the miss policy) and
        ordered by config-load walk order.

        Only ``origin`` (auto-declared, source = first matching
        reference's source) is attached here. ``usage`` is centralized
        in ``Registry.finalize``'s post-stabilization pass so the kind
        doesn't need to know the final reference map -- a synthesized
        Resource that goes on to publish references of its own may
        gather later incoming edges that this initial call can't see.

        Raises ``NoUnreferencedDefaultError`` if called with
        ``references=()`` -- the secret kind has no concept of an
        unreferenced default (``auto_declare_names = None``), so the
        framework never calls this path; the explicit error is defensive
        in case the kind's auto-declare configuration ever changes.
        """
        if not references:
            raise NoUnreferencedDefaultError(
                "the secret kind has no reserved default name; synthesize requires at least one reference"
            )
        first = references[0]
        return SecretDecl(
            name=first.name,
            description="",
            # The DECLARER: an inheriting template publishes its merged
            # declaration's secrets, so the row that WROTE the name is the
            # provenance an operator can act on.
            origin=Origin.auto_declared(source=first.declarer),
        )


KIND_REGISTRY[SECRET_KIND_NAME] = _SecretKind()


@dataclass(frozen=True)
class _SecretSourceKind:
    """Framework strategy for declarable configured secret sources."""

    kind: str = SECRET_SOURCE_KIND_NAME
    model: type[DeclaredResource] = SecretSourceDecl
    description: str = "Configured instances of secret backend implementations"
    prose: TopicProse = TopicProse(
        title="Secret sources",
        overview="""
        A secret-source gives one backend implementation a configured name. The
        backend table selects exactly one secret-backend and carries that backend's
        per-source settings. Declare multiple sources when accounts or stores need
        different configuration.

        Agentworks publishes env-var and prompt source declarations under their usual
        names. An operator declaration with either name replaces that built-in row;
        the surviving row's origin records the override.
        """,
    )
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "allow"

    def synthesize(self, references: Sequence[ResourceReference]) -> None:
        raise NoUnreferencedDefaultError(
            "the secret-source kind has miss_policy='error'; synthesize should never be dispatched"
        )

    def missing_reference_error(
        self,
        *,
        name: str,
        registry: Registry,
        referrer: ResourceReference,
    ) -> ConfigError | None:
        """Offer the domain-specific direct-backend migration diagnostic."""
        from agentworks.secrets.sources import direct_backend_source_error

        return direct_backend_source_error(name=name, registry=registry, referrer=referrer)


KIND_REGISTRY[SECRET_SOURCE_KIND_NAME] = _SecretSourceKind()
