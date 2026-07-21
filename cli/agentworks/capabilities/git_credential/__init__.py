"""The ``git-credential-provider`` capability: code-side handles for
each ``[git_credentials.<name>].provider`` value.

Each provider implementation (``GitHubCredentialProvider``,
``AzDOCredentialProvider``) is a ``Capability`` (see
``capabilities/README.md``): it validates its ``provider_config``,
authenticates its token at the ``runup`` stage, and produces the
credential materials as its op. The consuming resource
(``GitCredentialConfig``) and the materials assembly that writes them to
a VM live in the ``git_credentials`` domain, not here; capabilities
depend only on the framework, never on their consuming domain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.git_credential.azdo import AzDOCredentialProvider
from agentworks.capabilities.git_credential.base import (
    GitCredentialProvider,
    HelperEntry,
    credential_name_from_owner,
    default_token_secret,
    token_config_reference,
)
from agentworks.capabilities.git_credential.github import GitHubCredentialProvider

if TYPE_CHECKING:
    from agentworks.resources import Registry

__all__ = [
    "GIT_CREDENTIAL_PROVIDER_REGISTRY",
    "AzDOCredentialProvider",
    "GitCredentialProvider",
    "GitHubCredentialProvider",
    "HelperEntry",
    "credential_name_from_owner",
    "default_token_secret",
    "publish_to",
    "token_config_reference",
]


# The capability registry (the canonical provider list): provider name
# -> implementation class. ``validate_config`` (blob validation +
# implied references) is invoked through this dict at each source's
# blob boundary and at finalize; descriptor rows publish from it.
GIT_CREDENTIAL_PROVIDER_REGISTRY: dict[str, type[GitCredentialProvider]] = {
    "azdo": AzDOCredentialProvider,
    "github": GitHubCredentialProvider,
}


def publish_to(registry: Registry) -> None:
    """Publish the known git credential provider types into the registry.

    Each entry lands as a ``GitCredentialProviderEntry`` row, built-in
    with source ``"agentworks.capabilities.git_credential"``.

    Unlike the apt / install-command kinds, this kind has no
    operator-override path today: ``Config.publish_to`` publishes
    ``git_credentials`` entries (the per-credential config), not
    ``git-credential-provider`` rows.
    The kind is read-only from the operator's perspective; a future SDD
    that wants to let operators register new provider types would add an
    operator-publish path.
    """
    from agentworks.capabilities.git_credential.kinds import (
        GitCredentialProviderEntry,
    )
    from agentworks.resources import Origin

    code_origin = Origin.built_in(source="agentworks.capabilities.git_credential")
    for type_name in sorted(GIT_CREDENTIAL_PROVIDER_REGISTRY):
        registry.add(
            "git-credential-provider",
            type_name,
            GitCredentialProviderEntry(name=type_name),
            code_origin,
        )
