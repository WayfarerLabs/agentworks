"""``_GitCredentialKind`` and ``_GitCredentialProviderKind``: framework
strategies for the ``"git-credential"`` and ``"git-credential-provider"``
kinds, plus the ``GitCredentialProviderEntry`` capability row.

Both live in the ``git_credentials`` domain package next to the provider
implementations; ``agentworks.resources.kinds.__init__`` imports this
module so the kinds self-register into ``KIND_REGISTRY`` at load.

``GitCredentialKind`` uses miss policy ``error`` -- it does NOT
synthesize. Operators must explicitly declare every
``[git_credentials.<name>]`` they reference (from
``admin.git_credentials`` / ``agent_templates.*.git_credentials``). A
typo'd reference errors at config load via the framework's miss-policy
dispatch, with the reference source surfaced. The kind is intentionally
minimal: validating "the name is published" is the whole job. Token
resolution happens through the secret kind (each
``GitCredentialConfig`` emits a ``SecretReference`` for its token at
finalize time).

``GitCredentialProviderKind`` gives the framework a name-keyed marker so
``[git_credentials.<name>].provider = "..."`` typos surface uniformly.
Provider implementations live in ``agentworks.git_credentials``; the
companion publisher there adds one ``GitCredentialProviderEntry`` row per
known provider, built-in with source ``"agentworks.git_credentials"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentworks.resources.kind import KIND_REGISTRY, NoUnreferencedDefaultError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


@dataclass(frozen=True)
class GitCredentialProviderEntry:
    """A name-keyed marker for one git credential provider implementation
    (e.g., ``"github"``, ``"azdo"``).

    The actual provider class (``GitHubCredentialProvider``,
    ``AzDOCredentialProvider``) lives in
    ``agentworks.git_credentials.<name>``; this row is what
    ``[git_credentials.<name>].type = "..."`` resolves against in the
    framework.
    """

    name: str
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class _GitCredentialKind:
    """Implementation of ``ResourceKind`` for ``"git-credential"``."""

    kind: str = "git-credential"
    description: str = "Declared git credentials"
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
        # satisfy the Protocol; honors the Phase 2a empty-references
        # contract (FRD R3) by raising the typed framework error so a
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
    description: str = "Capability for provisioning git credentials based on the provider (github, azdo)"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None
    category: Literal["declarable", "capability"] = "capability"
    builtin_override: Literal["allow", "reserved"] = "reserved"

    def synthesize(self, references: Sequence[ResourceReference]) -> Any:
        # Unreachable under the error miss policy; honors the Phase 2a
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
