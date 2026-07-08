"""``GitCredentialConfig``: the operator-declared git-credential dataclass,
plus the ``credential_references`` helper.

Moved out of ``agentworks.config`` so the ``git_credentials`` domain owns
its declared-resource type next to the provider implementations and the
kinds (``agentworks.git_credentials.kinds``). ``config.py`` keeps only
the legacy TOML loader that constructs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import (
        ReferenceEntry,
        ResourceReference,
    )


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


@dataclass(frozen=True)
class GitCredentialConfig:
    name: str
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
    provider_config: dict[str, object] = field(default_factory=dict)
    description: str | None = None
    # Secret name for the auth token. Default ``"git-token-<name>"`` is
    # computed in ``__post_init__`` (the per-credential default depends
    # on the credential's own name, which a class-level literal can't
    # express). Operators may override with a custom secret name; the
    # framework's ``"secret"`` kind then resolves the value. Bare-string
    # only per Phase 1c's pattern; no ``{ secret = "..." }``
    # polymorphism.
    token: str = ""
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def __post_init__(self) -> None:
        # Frozen dataclasses can still ``object.__setattr__`` during
        # construction. The default ``""`` sentinel triggers the
        # name-interpolated default; an operator-typed string survives
        # unchanged.
        if not self.token:
            object.__setattr__(self, "token", f"git-token-{self.name}")

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import SecretReference

        source = ("git-credential", self.name)
        refs: list[ResourceReference] = [
            SecretReference(
                name=self.token,
                kind="secret",
                usage="the auth token",
                source=source,
            ),
            # Phase 2b.1: the ``provider`` field references a known
            # provider kind; framework miss policy catches typos.
            _ResourceReq(
                name=self.provider,
                kind="git-credential-provider",
                usage="the provider",
                source=source,
            ),
        ]
        # Capability-implied references: the provider validates its
        # config block and returns the references it implies; this
        # resource (the config block's owner) attributes them to itself.
        from agentworks.git_credentials import GIT_CREDENTIAL_PROVIDER_REGISTRY

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
