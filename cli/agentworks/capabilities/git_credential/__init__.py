"""The ``git-credential-provider`` capability: code-side handles for
each ``[git_credentials.<name>].provider`` value.

Each provider implementation (``GitHubCredentialProvider`` in core,
``AzDOCredentialProvider`` in the opt-in ``azure`` system plugin) is a
``Capability`` (see ``capabilities/README.md``): it declares the shape of
its own config block, authenticates its token at the ``runup`` stage, and
produces the credential materials as its op. The consuming resource
(``GitCredentialConfig``) and the materials assembly that writes them to
a VM live in the ``git_credentials`` domain, not here; capabilities
depend only on the framework, never on their consuming domain.

The ``azdo`` provider now ships in the ``azure`` system plugin
(``agentworks.plugins.azure``); its adapter re-seats it into
``GIT_CREDENTIAL_PROVIDER_REGISTRY`` at import, so credential resolution
still finds it by registry name, while its ROW publishes with a
``system-plugin`` origin (the built-in publisher skips it).
"""

from __future__ import annotations

from agentworks.capabilities.git_credential.base import (
    GitCredentialProvider,
    HelperEntry,
    StoredToken,
    TokenAcquiringConfig,
    TokenAcquisition,
    TokenSourcedConfig,
)
from agentworks.capabilities.git_credential.github import GitHubCredentialProvider

__all__ = [
    "GIT_CREDENTIAL_PROVIDER_REGISTRY",
    "GitCredentialProvider",
    "GitHubCredentialProvider",
    "HelperEntry",
    "StoredToken",
    "TokenAcquiringConfig",
    "TokenAcquisition",
    "TokenSourcedConfig",
]


# The capability registry (the canonical provider list): provider name
# -> implementation class. The core reaches each provider's DECLARED
# config model through this dict to validate a blob and to extract the
# references it implies; descriptor rows publish from it.
# ``azdo`` is re-seated here by the ``azure`` system plugin at import.
GIT_CREDENTIAL_PROVIDER_REGISTRY: dict[str, type[GitCredentialProvider]] = {
    "github": GitHubCredentialProvider,
}
