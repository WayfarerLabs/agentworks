"""The ``git-credential-provider`` capability: code-side handles for
each ``[git_credentials.<name>].provider`` value.

Each provider implementation (``GitHubCredentialProvider`` in core,
``AzDOCredentialProvider`` in the opt-in ``azure`` system plugin) is a
``Capability`` (see ``capabilities/README.md``): it validates its
``provider_config``, authenticates its token at the ``runup`` stage, and
produces the credential materials as its op. The consuming resource
(``GitCredentialConfig``) and the materials assembly that writes them to
a VM live in the ``git_credentials`` domain, not here; capabilities
depend only on the framework, never on their consuming domain.

The ``azdo`` provider now ships in the ``azure`` system plugin
(``agentworks.plugins.azure``); its adapter re-seats it into
``GIT_CREDENTIAL_PROVIDER_REGISTRY`` at import, so credential resolution
still finds it by registry name, while its ROW publishes with a
``system-plugin`` origin (see ``publish_to``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.capabilities.git_credential.base import (
    GitCredentialProvider,
    HelperEntry,
    credential_name_from_owner,
    default_token_secret,
    token_dependency,
    validate_token_field,
)
from agentworks.capabilities.git_credential.github import GitHubCredentialProvider

if TYPE_CHECKING:
    from agentworks.resources import Registry

__all__ = [
    "GIT_CREDENTIAL_PROVIDER_REGISTRY",
    "GitCredentialProvider",
    "GitHubCredentialProvider",
    "HelperEntry",
    "credential_name_from_owner",
    "default_token_secret",
    "publish_to",
    "token_dependency",
    "validate_token_field",
]


# The capability registry (the canonical provider list): provider name
# -> implementation class. ``dependencies`` (implied references) and
# ``validate`` (blob validation) are invoked through this dict at each
# source's blob boundary and at finalize; descriptor rows publish from it.
# ``azdo`` is re-seated here by the ``azure`` system plugin at import.
GIT_CREDENTIAL_PROVIDER_REGISTRY: dict[str, type[GitCredentialProvider]] = {
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

    A provider seated by a system plugin (``azdo`` via the ``azure`` plugin)
    keeps its impl in ``GIT_CREDENTIAL_PROVIDER_REGISTRY`` so credential
    resolution can construct it, but its row is published by
    ``plugins.publish_plugins`` with a ``system-plugin`` origin. Skip those
    names here so the plugin is the sole publisher of the row; publishing it
    here too would collide (built-in vs system-plugin) at ``Registry.add``.
    """
    from agentworks.capabilities.git_credential.kinds import (
        GitCredentialProviderEntry,
    )
    from agentworks.plugins.registration import plugin_seated_names
    from agentworks.resources import Origin

    seated_by_plugin = plugin_seated_names("git-credential-provider")
    code_origin = Origin.built_in(source="agentworks.capabilities.git_credential")
    for type_name in sorted(GIT_CREDENTIAL_PROVIDER_REGISTRY):
        if type_name in seated_by_plugin:
            continue
        registry.add(
            "git-credential-provider",
            type_name,
            GitCredentialProviderEntry(name=type_name),
            code_origin,
        )
