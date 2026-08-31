"""``_GitCredentialKind`` and ``_GitCredentialProviderKind``: framework
strategies for the ``"git-credential"`` and ``"git-credential-provider"``
kinds, plus the ``GitCredentialProviderEntry`` capability row and the
provider kind's ``CapabilityKindDescriptor``.

Both live in the ``git_credentials`` domain package next to the provider
implementations; ``agentworks.resources.kinds.__init__`` imports this
module so the kinds self-register into ``KIND_REGISTRY`` at load.

``GitCredentialKind`` uses miss policy ``error`` -- it does NOT
synthesize. Operators must explicitly declare every
``git-credential`` manifest they reference from an admin-template or
agent-template. A typo'd reference errors at config load via the framework's
miss-policy dispatch, with the reference source surfaced. The kind is
intentionally minimal: validating "the name is published" is the whole job.
Provider-declared resource references are derived structurally from each
provider's configuration model.

``GitCredentialProviderKind`` gives the framework a name-keyed marker so
``spec.provider.name`` typos surface uniformly.
Provider implementations live in ``agentworks.git_credentials``; the
companion publisher there adds one ``GitCredentialProviderEntry`` row per
known provider, built-in with source ``"agentworks.git_credentials"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.capabilities.descriptor import (
    CapabilityKindDescriptor,
    ConfigContract,
    HostSurface,
)
from agentworks.capabilities.git_credential.base import GitCredentialProvider
from agentworks.git_credentials.credential import GitCredentialConfig
from agentworks.resources.graph import Readiness
from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError
from agentworks.schema import AgwModel
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.declared_resource import DeclaredResource
    from agentworks.origin import Origin
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True)
class GitCredentialProviderEntry:
    """A name-keyed marker for one git credential provider implementation
    (e.g., ``"github"``, ``"azdo"``).

    The actual provider class (``GitHubCredentialProvider`` in core,
    ``AzDOCredentialProvider`` in the ``azure`` plugin at
    ``agentworks.plugins.azure.azdo``) lives beside its capability module;
    this row is what a git-credential's ``spec.provider.name`` resolves against
    in the framework.

    Inbound references live on the dependency graph
    (``Registry.graph.dependents_of``), not on this row.
    """

    name: str
    origin: Origin | None = None


@dataclass(frozen=True)
class _GitCredentialKind:
    """Implementation of ``ResourceKind`` for ``"git-credential"``."""

    kind: str = "git-credential"
    model: type[DeclaredResource] = GitCredentialConfig
    description: str = "Declared git credentials"
    prose: TopicProse = TopicProse(
        title="Git credentials",
        overview="""
        A git-credential tells one provider how to produce HTTPS credentials and where
        they apply. `spec.provider` selects the provider capability and carries its
        complete config; admin and agent templates refer to the credential by name.

        Providers may return a final stored username/password or a managed helper that
        acquires one at Git runtime. Agentworks combines every declared credential into
        one path-aware helper configuration and rebuilds that state on each user init.
        """,
    )
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None  # ignored under "error"
    category: Literal["declarable", "capability"] = "declarable"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(
        self,
        references: Sequence[ResourceReference],
    ) -> None:
        # Unreachable under the "error" miss policy: the Registry's
        # finalize pass raises ConfigError before dispatching to
        # synthesize for error-policy kinds. The method exists to
        # satisfy the Protocol; honors the empty-references
        # contract by raising the typed framework error so a
        # hypothetical future change that gives the kind a reserved
        # default has an obvious landing pad.
        raise NoUnreferencedDefaultError(
            "the git_credentials kind has no reserved default name; "
            "synthesize is never invoked under the error miss policy"
        )


@dataclass(frozen=True)
class _GitCredentialProviderKind:
    """Implementation of ``ResourceKind`` for ``"git-credential-provider"``."""

    kind: str = "git-credential-provider"
    description: str = "Capability for producing scoped Git HTTPS credential material"
    prose: TopicProse = TopicProse(
        title="Git credential providers",
        overview="""
        A git-credential-provider owns credential acquisition, final Git username and
        password construction, and translation of forge concepts into generic HTTPS
        path scopes.

        Providers are code, and a git-credential document selects one by writing its
        name inside `spec.provider`. The keys allowed beside that name are the
        provider's own, which is why each documents its own config.
        """,
    )
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "capability"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        # Unreachable under the error miss policy; honors the
        # empty-references contract via the typed framework error so a
        # future change that gives the kind a reserved default has an
        # obvious landing pad.
        raise NoUnreferencedDefaultError(
            "the git_credential_provider kind has miss_policy='error'; "
            "synthesize should never be invoked (the framework raises "
            "ConfigError first)"
        )


KIND_REGISTRY["git-credential"] = _GitCredentialKind()
KIND_REGISTRY["git-credential-provider"] = _GitCredentialProviderKind()


def _registry() -> dict[str, Any]:
    from agentworks.capabilities.git_credential import GIT_CREDENTIAL_PROVIDER_REGISTRY

    return GIT_CREDENTIAL_PROVIDER_REGISTRY


def _entry(name: str, impl: Any, origin: Origin | None) -> GitCredentialProviderEntry:
    # The impl's ``description`` is deliberately not carried: this row is
    # name-and-origin only, and giving it one would change row content.
    return GitCredentialProviderEntry(name=name, origin=origin)


def _readiness(name: str, impl: Any) -> Readiness:
    """Always ready: a credential provider has no host-support concept (it
    talks to a forge over the network, which is a runtime concern)."""
    return Readiness.ready()


GIT_CREDENTIAL_PROVIDER_DESCRIPTOR = CapabilityKindDescriptor(
    kind="git-credential-provider",
    contract_version=3,
    implementation_contract=GitCredentialProvider,
    registry=_registry,
    required_operations=frozenset({"credential_material", "credential_scopes"}),
    # Empty: GitCredentialProvider supplies every non-operation member a
    # subclass needs.
    required_attributes=frozenset(),
    entry_factory=_entry,
    readiness=_readiness,
    publisher_source="agentworks.capabilities.git_credential",
    config_schema=ConfigContract(base=AgwModel, discriminator="name"),
    manifest_section=HostSurface(
        host_kind="git-credential",
        naming_field="provider",
    ),
)
"""The git-credential-provider record in the capability-kind descriptor
table (``agentworks.capabilities.descriptor``)."""
