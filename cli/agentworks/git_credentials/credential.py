"""``GitCredentialConfig``: the operator-declared git-credential row,
plus the ``credential_references`` helper.

Moved out of ``agentworks.config`` so the ``git_credentials`` domain owns
its declared-resource type. The provider capability it references (and
its kind, ``agentworks.capabilities.git_credential.kinds``) lives in the
capabilities subtree; this consuming resource depends on it, not the
reverse. The ``agentworks.config`` package keeps only the legacy TOML
loader that constructs it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import model_validator

from agentworks.declared_resource import DeclaredResource
from agentworks.schema import CapabilityBlock, RefOwner

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.graph import DependencyState, FinalizeContext, Readiness
    from agentworks.resources.reference import ResourceReference


def credential_references(
    git_credentials: list[str] | None,
    source: tuple[str, str],
    declarers: Mapping[str, tuple[str, str]] | None = None,
) -> list[ResourceReference]:
    """Emit a ``ResourceReference`` of kind ``"git-credential"`` per
    name in ``git_credentials``. Used by ``AdminConfig.dependencies``
    and ``AgentTemplate.dependencies`` to feed the
    ``GitCredentialKind``'s error miss policy: a typo'd or undeclared
    name errors at finalize naming the template that wrote the name.

    ``declarers`` maps a credential name to the template that declared
    it, for an inheriting owner passing its MERGED list (FR17). Absent, or
    missing a name, means the owner declared it.
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
            declared_by=(declarers or {}).get(cred_name),
        )
        for cred_name in git_credentials
    ]


class GitCredentialConfig(DeclaredResource):
    """A declared git credential: which provider fronts it, and that
    provider's own configuration."""

    provider: CapabilityBlock
    """The git-credential-provider fronting this credential: one table
    whose ``name`` selects the provider and whose remaining keys are that
    provider's own config (azdo's ``org``; github's ``repos`` / ``owner``;
    the ``token`` secret each provider sources its PAT from, defaulting to
    ``git-token-<name>``)."""

    @model_validator(mode="before")
    @classmethod
    def _steer_a_top_level_token(cls, data: Any) -> Any:
        """``token`` is the mistake operators make coming from the flat
        TOML shape. As a plain unknown key it would name the valid field
        without saying where the token goes."""
        if isinstance(data, dict) and "token" in data:
            raise ValueError(
                "'token' is provider config now: move it into the spec.provider table "
                "(its 'name' key selects the provider)"
            )
        return data

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        from agentworks.resources.reference import (
            ResourceReference as _ResourceReq,
        )
        from agentworks.resources.reference import sourced_references

        source = ("git-credential", self.name)
        # The ``provider`` field references a known provider
        # kind; framework miss policy catches typos.
        refs: list[ResourceReference] = [
            _ResourceReq(
                name=self.provider.name,
                kind="git-credential-provider",
                usage="the provider",
                source=source,
            ),
        ]
        # Everything the credential references (its token secret and any
        # other resource the provider's config names) is read structurally
        # off the provider's DECLARED model, by the core: no provider code
        # runs here. Total and non-throwing, so a malformed blob
        # contributes no edges rather than sinking the walk. This resource
        # (the config block's owner) attributes them to itself via the
        # shared conversion.
        from agentworks.capabilities.config import capability_config_references

        refs.extend(
            sourced_references(
                capability_config_references(
                    kind="git-credential-provider",
                    config=self.provider.tagged,
                    owner=RefOwner(kind="git-credential", name=self.name),
                ),
                source,
            )
        )
        return refs

    def not_ready(self, deps: Mapping[tuple[str, str], DependencyState]) -> Readiness:
        """This credential's readiness verdict, propagated from its SINGLE
        provider dependency's enablement (mirroring ``VMSiteDecl.not_ready``,
        the vm-site propagation model, R14).

        A ``git-credential`` fronts exactly one provider, so, like a vm-site
        over its platform, it is not-ready when that provider is disabled: the
        fold hands the provider's :class:`DependencyState` for free (the
        ``git-credential -> git-credential-provider`` edge already exists in
        ``dependencies``), and this hook returns a blocked verdict carrying the
        mark's remediation reason (e.g. "enable plugin `<name>`"), falling back
        to "enable its unit" when no source supplied one. The provider itself
        has no host-support axis (a git-credential-provider node is always
        ready), so there is no not-ready readiness to propagate: only the
        opt-in (enablement) axis matters here. An enabled provider leaves the
        credential ready.
        """
        from agentworks.resources.graph import Enablement, Readiness

        provider = deps[("git-credential-provider", self.provider.name)]
        if provider.enablement is Enablement.disabled:
            tail = provider.disabled_reason or "enable its unit"
            return Readiness.blocked(
                f"depends on git-credential-provider '{self.provider.name}', which is disabled; {tail}"
            )
        return Readiness.ready()

    def validate_config(self, context: FinalizeContext) -> None:
        """Throwing shape check for the provider config block, run by
        the finalize ``validate`` pass. Mirrors ``dependencies``:
        the CORE validates the blob against the named provider's declared
        model, and no provider code runs. An unknown provider is tolerated
        here (the framework miss policy reports it); a seated provider's
        blob is validated.

        The error this raises already carries its own file/line framing
        (the schema error bridge), so the finalize pass leaves it alone
        rather than appending an origin a second time.
        """
        from agentworks.capabilities.config import validate_capability_config

        validate_capability_config(
            kind="git-credential-provider",
            config=self.provider.tagged,
            owner=RefOwner(kind="git-credential", name=self.name),
            location=self.error_location,
        )
