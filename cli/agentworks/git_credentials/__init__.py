"""Git credential providers.

Each provider implementation (``GitHubCredentialProvider``,
``AzDOCredentialProvider``) is the code-side handle for one
``[git_credentials.<name>].type = "..."`` value. The framework's
``git_credential_provider`` kind (Phase 2b.1) holds one row per known
provider so a typo in the operator's ``type`` field surfaces as a
clean miss-policy error at ``build_registry`` time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources import Registry


# Known provider type identifiers. Order is non-meaningful (alphabetical
# for readability); the framework looks rows up by name.
PROVIDER_TYPES: tuple[str, ...] = ("azdo", "github")


def publish_to(registry: Registry) -> None:
    """Publish the known git credential provider types into the registry.

    Each entry lands as a ``GitCredentialProviderEntry`` row, code-declared
    with source ``"agentworks.git_credentials"``. Phase 2b.1.
    """
    from agentworks.resources import Origin
    from agentworks.resources.kinds.git_credential_provider import (
        GitCredentialProviderEntry,
    )

    code_origin = Origin.code_declared(source="agentworks.git_credentials")
    for type_name in PROVIDER_TYPES:
        registry.add(
            "git_credential_provider",
            type_name,
            GitCredentialProviderEntry(name=type_name),
            code_origin,
        )
