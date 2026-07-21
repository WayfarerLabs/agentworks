"""``GitCredentialConfig``: the operator-declared git-credential dataclass,
plus the ``credential_references`` helper.

Moved out of ``agentworks.config`` so the ``git_credentials`` domain owns
its declared-resource type. The provider capability it references (and
its kind, ``agentworks.capabilities.git_credential.kinds``) lives in the
capabilities subtree; this consuming resource depends on it, not the
reverse. ``config.py`` keeps only the legacy TOML loader that constructs
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.declared_resource import DeclaredResource

if TYPE_CHECKING:
    from agentworks.resources.reference import ResourceReference


def credential_references(
    git_credentials: list[str] | None,
    source: tuple[str, str],
) -> list[ResourceReference]:
    """Emit a ``ResourceReference`` of kind ``"git-credential"`` per
    name in ``git_credentials``. Used by ``AdminConfig.referenced_resources``
    and ``AgentTemplate.referenced_resources`` to feed the
    ``GitCredentialKind``'s error miss policy: a typo'd or undeclared
    name errors at finalize with the reference source pointing at the
    declaring Resource.
    """
    from agentworks.resources.reference import ResourceReference

    if not git_credentials:
        return []
    return [
        ResourceReference(
            name=cred_name,
            kind="git-credential",
            usage="the git credential",
            source=source,
        )
        for cred_name in git_credentials
    ]


@dataclass(frozen=True, kw_only=True)
class GitCredentialConfig(DeclaredResource):
    # The internal representation follows the YAML manifest shape (ADR
    # 0016): field name ``provider``, matching ``spec.provider``. Only
    # the TOML section still spells ``type`` (with ``provider`` as the
    # preferred alias); the loader maps at its boundary.
    provider: str
    # Provider-owned configuration (azdo's org), nested per the
    # provider_config pattern (ADR 0016). The flat TOML section is the
    # ONLY place org lives at the top level; this loader nests it at
    # the boundary, so the internal representation matches the YAML
    # manifest shape.
    # Provider-owned configuration (azdo's org; github's repos/owner;
    # and the ``token`` secret name that every current provider sources
    # its PAT from, default ``git-token-<name>``, owned by the
    # provider's ``validate_config`` since sourcing is provider-specific
    # (a future minting provider declares a bootstrap secret, or none).
    # The flat TOML section is the ONLY place these live at the top
    # level; the loader nests them here so the internal representation
    # matches the YAML manifest shape.
    provider_config: dict[str, object] = field(default_factory=dict)

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import SecretReference

        source = ("git-credential", self.name)
        # The ``provider`` field references a known provider
        # kind; framework miss policy catches typos.
        refs: list[ResourceReference] = [
            _ResourceReq(
                name=self.provider,
                kind="git-credential-provider",
                usage="the provider",
                source=source,
            ),
        ]
        # Everything the credential references (its token secret and
        # any other provider-declared resources) comes from the
        # provider validating its config block and returning the
        # references it implies; this resource (the config block's
        # owner) attributes them to itself.
        from agentworks.capabilities.git_credential import (
            GIT_CREDENTIAL_PROVIDER_REGISTRY,
        )

        capability = GIT_CREDENTIAL_PROVIDER_REGISTRY.get(self.provider)
        if capability is not None:
            for cref in capability.validate_config(
                f"git-credential/{self.name}", self.provider_config
            ):
                ref_cls = SecretReference if cref.kind == "secret" else _ResourceReq
                refs.append(
                    ref_cls(
                        name=cref.name,
                        kind=cref.kind,
                        usage=cref.usage,
                        source=source,
                    )
                )
        return refs
