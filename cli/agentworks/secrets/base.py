"""Core types for the agentworks secret system.

Backends are the door (runtime-model LLD of the resource-manifests SDD):
every runtime secret operation is a method on ``SecretBackendDecl``,
which invokes its provider (the raw capability) through the provider
API. See ``docs/adrs/0013-cli-side-secret-injection.md`` for why values
never persist on the VM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    # Type-only imports to avoid the cycle: agentworks.resources.kinds.secret
    # imports SecretDecl from this module to write its synthesize(); having
    # this module import Origin / ReferenceEntry at runtime would loop.
    # `from __future__ import annotations` keeps the field types as strings,
    # so the runtime imports are unnecessary.
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference
    from agentworks.secrets.providers import SecretProvider

MappingValue = str | dict[str, object] | Literal[False]
"""One entry in ``SecretDecl.backend_mappings``: an identifier override
(string or structured), or ``False`` for an explicit opt-out."""


@dataclass(frozen=True)
class SecretDecl:
    """A declared secret. Values are never stored here; only the existence,
    description, and per-backend identifier overrides.

    ``backend_mappings`` is keyed by BACKEND NAME (the ``secret-backend``
    resource's name, e.g. ``"env-var"``, ``"op-work"``) -- never by
    provider. Two backends sharing one provider get independent
    mappings. The built-in backends' names coincide with their
    providers' names by naming choice, never relied on in code. Value
    forms per FRD R4:

    - ``str``: backend's identifier for this secret (env var name, op:// URI, etc.).
    - ``dict[str, object]``: structured identifier (for backends whose ID has
      multiple fields, e.g. 1Password ``{vault, item, field}``).
    - ``False``: opt out; skip this backend for this secret regardless of any
      default convention the backend would otherwise apply.
    - key absent: use the backend's default convention if it has one, else
      soft-skip (backend reports as "no mapping" via ``would_attempt``).
    """

    name: str
    description: str
    hint: str | None = None
    backend_mappings: dict[str, MappingValue] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)
    # Registry-layer fields: framework attaches at publish (``origin``) and
    # ``finalize`` (``usage``). Both default to "not yet attached" for
    # direct-construction call sites (tests, framework synthesize paths).
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class SecretBackendDecl:
    """A secret backend: the exposed resource for a secret provider.

    Backends are THE DOOR to secrets -- every runtime operation
    (``would_attempt``, ``describe_lookup``, ``resolve``) is a method
    here, and only these methods call the provider API. ``name`` is the
    only identity runtime surfaces use; ``provider`` names the raw
    capability (a field, not an identity); ``config`` carries the
    provider-specific fields, validated by the provider at manifest
    decode. Multiple backends may share one provider.
    """

    name: str
    provider: str
    description: str = ""
    config: dict[str, object] = field(default_factory=dict)
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        from agentworks.resources.reference import ResourceReference

        return [
            ResourceReference(
                name=self.provider,
                kind="secret-provider",
                usage="the secret provider",
                source=("secret-backend", self.name),
            )
        ]

    # -- The door: runtime operations -----------------------------------

    def _capability(self) -> SecretProvider:
        from agentworks.errors import ConfigError
        from agentworks.secrets.providers import PROVIDER_REGISTRY

        capability = PROVIDER_REGISTRY.get(self.provider)
        if capability is None:
            # The registry graph validates provider references at
            # finalize; reaching here means a row exists without code
            # (a registration bug), or the decl never went through a
            # registry at all.
            raise ConfigError(
                f'secret-backend "{self.name}" names provider '
                f"{self.provider!r}, which has no registered implementation"
            )
        return capability

    def mapping_for(self, secret: SecretDecl) -> MappingValue | None:
        """This backend's entry in the secret's ``backend_mappings``,
        keyed by BACKEND NAME. ``None`` when absent (provider default
        convention applies, if it has one)."""
        return secret.backend_mappings.get(self.name)

    @property
    def interactive(self) -> bool:
        """Whether resolution interacts with the operator (prompt).
        Inspection previews must not call ``resolve`` on interactive
        backends -- probing would BE the interaction."""
        return self._capability().interactive

    def would_attempt(self, secret: SecretDecl) -> bool:
        """Does this backend's config apply to this secret? The explicit
        opt-out (``mapping is False``) is handled here, generically; the
        provider decides the rest (default convention vs. soft-skip).
        Never verifies that resolution would succeed."""
        mapping = self.mapping_for(secret)
        if mapping is False:
            return False
        return self._capability().would_attempt(self.config, secret, mapping)

    def describe_lookup(self, secret: SecretDecl) -> str | None:
        """Human-readable identifier this backend would use (env var
        name, op:// URI, ...); ``None`` for backends with no static
        identifier. Pure config-derived; never probes."""
        mapping = self.mapping_for(secret)
        if mapping is False:
            return None
        return self._capability().describe_lookup(self.config, secret, mapping)

    def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Batch-resolve through the provider. Callers pre-filter by
        ``would_attempt``, and the door enforces the opt-out
        structurally: a ``False`` mapping never reaches the provider,
        so a forgotten pre-filter cannot resolve an opted-out secret.
        Secrets the provider has no value for are simply absent from
        the result (soft miss). A persistent-store provider raises
        ``SecretMappingError`` when an explicit mapping definitively
        has no value (hard miss; halts the chain)."""
        wants: list[tuple[SecretDecl, MappingValue | None]] = [
            (s, mapping)
            for s in secrets
            if (mapping := self.mapping_for(s)) is not False
        ]
        if not wants:
            return {}
        return self._capability().batch_get(self.config, wants)

    def validate_config(self) -> None:
        """Re-run the provider's config schema over this backend's
        config (decode already ran it for manifest-declared backends;
        this covers hand-published rows and future non-manifest
        publishers). Raises ``ConfigError`` on schema violations."""
        self._capability().validate_config(self.name, self.config)


DEFAULT_BACKEND_CHAIN: tuple[str, ...] = ("env-var", "prompt")
"""Default backend chain when ``[secret_config].backends`` is absent.

Resolves declared secrets from operator-side env (``AW_SECRET_<NAME>``) first,
then prompts interactively. The chain is operator-overridable via an explicit
``[secret_config]`` block; an explicit empty list ``backends = []`` disables
resolution entirely (operators who don't use secrets pay nothing either way).
"""


@dataclass(frozen=True)
class SecretConfig:
    """Top-level [secret_config] table. Pure config, never published to
    the resource Registry: the chain is a SETTING that names resources
    (like a future active-plugins list would), consumed by the secrets
    subsystem when it validates (``validate_chain``, at
    ``build_registry``) and when it resolves (``resolve_secrets``).

    ``backends`` is dual-role: presence activates the backend, list
    order is the resolution precedence. A declared backend absent from
    this list is dormant (never consulted).

    Default value is ``DEFAULT_BACKEND_CHAIN`` (``env-var``, then ``prompt``).
    The default applies when the operator's TOML has no ``[secret_config]``
    table OR has the table without a ``backends`` key. An explicit
    ``backends = []`` disables resolution entirely.
    """

    backends: tuple[str, ...] = DEFAULT_BACKEND_CHAIN
    declared_at: SourceLocation = field(default_factory=synthesized)
