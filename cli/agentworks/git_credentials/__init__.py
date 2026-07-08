"""Git credential providers.

Each provider implementation (``GitHubCredentialProvider``,
``AzDOCredentialProvider``) is the code-side handle for one
``[git_credentials.<name>].provider = "..."`` value (``type`` is the
accepted legacy alias). The framework's ``git-credential-provider``
kind (Phase 2b.1) holds one row per known provider so a typo in the
operator's ``provider`` field surfaces as a clean miss-policy error at
``build_registry`` time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.git_credentials.azdo import AzDOCredentialProvider
from agentworks.git_credentials.base import GitCredentialProvider
from agentworks.git_credentials.github import GitHubCredentialProvider

if TYPE_CHECKING:
    from agentworks.resources import Registry


# The capability registry: provider name -> implementation class.
# ``validate_config`` (blob validation + implied references) is invoked
# through this dict at each source's blob boundary and at finalize.
GIT_CREDENTIAL_PROVIDER_REGISTRY: dict[str, type[GitCredentialProvider]] = {
    "azdo": AzDOCredentialProvider,
    "github": GitHubCredentialProvider,
}

# Known provider type identifiers (registry keys, sorted).
PROVIDER_TYPES: tuple[str, ...] = tuple(sorted(GIT_CREDENTIAL_PROVIDER_REGISTRY))


def publish_to(registry: Registry) -> None:
    """Publish the known git credential provider types into the registry.

    Each entry lands as a ``GitCredentialProviderEntry`` row, built-in
    with source ``"agentworks.git_credentials"``. Phase 2b.1.

    Unlike the catalog kinds, this kind has no
    operator-override path today: ``Config.publish_to`` publishes
    ``git_credentials`` entries (the per-credential config), not
    ``git-credential-provider`` rows. The kind is read-only from the
    operator's perspective; a future SDD that wants to let operators
    register new provider types would add an operator-publish path.
    """
    from agentworks.resources import Origin
    from agentworks.resources.kinds.git_credential_provider import (
        GitCredentialProviderEntry,
    )

    code_origin = Origin.built_in(source="agentworks.git_credentials")
    for type_name in sorted(GIT_CREDENTIAL_PROVIDER_REGISTRY):
        registry.add(
            "git-credential-provider",
            type_name,
            GitCredentialProviderEntry(name=type_name),
            code_origin,
        )
