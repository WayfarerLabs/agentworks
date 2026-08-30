"""The ``git-credential-provider`` capability: code-side handles for
each git-credential manifest's ``spec.provider.name`` value.

Each provider implementation (``GitHubCredentialProvider`` in core,
``AzDOCredentialProvider`` in the opt-in ``azure`` system plugin) is a
``Capability`` (see ``capabilities/README.md``): it declares the shape of
its own config block, optionally checks provider inputs at ``runup``, and
produces final credential material. The consuming resource and the core
reconciler live in the ``git_credentials`` domain, not here; capabilities
depend only on the framework, never on their consuming domain.

The ``azdo`` provider now ships in the ``azure`` system plugin
(``agentworks.plugins.azure``); its adapter re-seats it into
``GIT_CREDENTIAL_PROVIDER_REGISTRY`` at import, so credential resolution
still finds it by registry name, while its ROW publishes with a
``system-plugin`` origin (the built-in publisher skips it).
"""

from __future__ import annotations

from agentworks.capabilities.git_credential.base import (
    CredentialPayload,
    GitCredentialProvider,
    HttpsCredentialScope,
    ManagedHelper,
    StoredCredential,
)
from agentworks.capabilities.git_credential.github import (
    GitHubCliSource,
    GitHubCredentialProvider,
    GitHubSecretSource,
)

__all__ = [
    "GIT_CREDENTIAL_PROVIDER_REGISTRY",
    "CredentialPayload",
    "GitCredentialProvider",
    "GitHubCliSource",
    "GitHubCredentialProvider",
    "GitHubSecretSource",
    "HttpsCredentialScope",
    "ManagedHelper",
    "StoredCredential",
]


# The capability registry (the canonical provider list): provider name
# -> implementation class. The core reaches each provider's DECLARED
# config model through this dict to validate a blob and to extract the
# references it implies; descriptor rows publish from it.
# ``azdo`` is re-seated here by the ``azure`` system plugin at import.
GIT_CREDENTIAL_PROVIDER_REGISTRY: dict[str, type[GitCredentialProvider]] = {
    "github": GitHubCredentialProvider,
}
