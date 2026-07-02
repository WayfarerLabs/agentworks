"""Framework strategy for the ``git_credential_provider`` kind: the
``type`` field on ``[git_credentials.<name>]`` references one of the
known provider implementations (``"github"``, ``"azdo"``).

The kind uses the error miss policy. Provider implementations
themselves live in ``agentworks.git_credentials`` -- this module just
gives the framework a name-keyed marker so typos surface uniformly.

The companion publisher in ``agentworks.git_credentials`` adds one row
per known provider, built-in with source
``"agentworks.git_credentials"``.
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
class _GitCredentialProviderKind:
    """Implementation of ``ResourceKind`` for ``"git-credential-provider"``."""

    kind: str = "git-credential-provider"
    miss_policy: Literal["auto-declare", "error"] = "error"
    auto_declare_names: frozenset[str] | None = None

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


KIND_REGISTRY["git-credential-provider"] = _GitCredentialProviderKind()
