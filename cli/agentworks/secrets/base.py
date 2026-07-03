"""Core types, protocol, and base class for the agentworks secret system.

See ``docs/sdd/2026-06-05-env-and-secrets/`` and
``docs/adrs/0013-cli-side-secret-injection.md`` for background on why
values never persist on the VM and why prompt is just another
SecretSource in the chain.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    # Type-only imports to avoid the cycle: agentworks.resources.kinds.secret
    # imports SecretDecl from this module to write its synthesize(); having
    # this module import Origin / ReferenceEntry at runtime would loop.
    # `from __future__ import annotations` keeps the field types as strings,
    # so the runtime imports are unnecessary.
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


@dataclass(frozen=True)
class SecretDecl:
    """A declared secret. Values are never stored here; only the existence,
    description, and per-backend identifier overrides.

    ``backend_mappings`` is keyed by backend kind (e.g. ``"env-var"``,
    ``"onepassword"``). Value forms per FRD R4:

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
    backend_mappings: dict[str, str | dict[str, object] | Literal[False]] = field(
        default_factory=dict
    )
    declared_at: SourceLocation = field(default_factory=synthesized)
    # Registry-layer fields: framework attaches at publish (``origin``) and
    # ``finalize`` (``usage``). Both default to "not yet attached" for
    # direct-construction call sites (tests, framework synthesize paths).
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


@dataclass(frozen=True)
class SecretBackendDecl:
    """A named, configured instantiation of a secret provider (Phase 3 of
    the resource-manifests SDD). Declared via ``secret-backend``
    manifests; the built-in ``env-var`` / ``prompt`` backends ship as
    bundled manifests in this shape.

    ``provider`` names the code capability
    (``agentworks.secrets.providers.PROVIDER_REGISTRY``); ``config``
    carries the provider-specific fields, validated by the provider at
    manifest decode. Multiple backends may share one provider.
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


@dataclass(frozen=True)
class SecretBackendConfig:
    """Connection / global config for one secret backend.

    Concrete backends carry their own dataclass subclasses with additional
    fields (account, vault, etc.). The ``kind`` field matches the
    ``[secret_backends.<kind>]`` key. This field is also the Resource's
    framework-registry name (the Registry stores rows under
    ``("secret-backend", kind)``), so SecretBackendConfig is keyed by
    ``kind`` rather than a separate ``name`` field unlike other Resource
    types.
    """

    kind: str
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()


DEFAULT_BACKEND_CHAIN: tuple[str, ...] = ("env-var", "prompt")
"""Default backend chain when ``[secret_config].backends`` is absent.

Resolves declared secrets from operator-side env (``AW_SECRET_<NAME>``) first,
then prompts interactively. The chain is operator-overridable via an explicit
``[secret_config]`` block; an explicit empty list ``backends = []`` disables
resolution entirely (operators who don't use secrets pay nothing either way).
"""


@dataclass(frozen=True)
class SecretConfig:
    """Top-level [secret_config] table.

    ``backends`` is dual-role: presence enables the backend, list order is the
    resolution precedence. A backend declared in ``[secret_backends.*]`` but
    absent from this list is dormant (its source is never instantiated).

    Default value is ``DEFAULT_BACKEND_CHAIN`` (``env-var``, then ``prompt``).
    The default applies when the operator's TOML has no ``[secret_config]``
    table OR has the table without a ``backends`` key. An explicit
    ``backends = []`` disables resolution entirely.
    """

    backends: tuple[str, ...] = DEFAULT_BACKEND_CHAIN
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        """The active chain as graph edges: one ``secret-backend``
        reference per entry, in precedence order. The secret-backend
        kind's error miss policy turns an unknown chain name into a
        uniform finalize-time ``ConfigError`` -- no bespoke check.

        The hardcoded ``("secret-config", "default")`` source matches
        the kind's singleton shape (SecretConfig carries no name field);
        if the kind ever plurifies, add a name field and derive the
        source from it (the admin-template pattern).
        """
        from agentworks.resources.reference import ResourceReference

        return [
            ResourceReference(
                name=backend,
                kind="secret-backend",
                usage="active backend chain",
                source=("secret-config", "default"),
            )
            for backend in self.backends
        ]


class SecretSource(Protocol):
    """Structural type contract for backends that produce secret values.

    Every backend implements this protocol; ``PromptSource`` is just one
    instance whose ``get`` happens to interact with the operator instead of
    reading from a vault. The resolver iterates a configured chain of sources
    in precedence order; first to return a non-None value wins.

    This is a type-only protocol: implementations do not need to inherit from
    it. Most concrete sources inherit from ``SecretSourceBase`` to pick up the
    default ``batch_get``, but a class that structurally implements the four
    members below satisfies the protocol regardless.
    """

    kind: str

    def would_attempt(self, secret: SecretDecl) -> bool:
        """Does this source's CONFIG apply to this secret?

        Determined from config alone (the secret's ``backend_mappings`` plus
        this source's default-convention behavior). Does NOT verify that
        resolution will succeed; ``EnvVarSource.would_attempt(s)`` is True
        even when the env var isn't set. Used at config-load time to surface
        unreachable secrets and by ``agw doctor`` for soft-skip diagnostics.
        """
        ...

    def get(self, secret: SecretDecl) -> str | None:
        """Resolve the secret if this source can. Two miss modes:

        - **Soft miss** (return ``None``): "I don't have a value; try the next
          source in the chain." The conventional shape for env-style backends
          (env-var, prompt): an env var that isn't set is just-not-set; there's
          no signal that the operator misconfigured anything. Falling through
          is the right behavior.
        - **Hard miss** (raise ``SecretMappingError``): "the operator told me
          exactly where to look and that location definitively has no value."
          The conventional shape for persistent-store backends (1Password,
          Vault) where the mapping is an explicit identifier (``op://...``,
          vault path). The resolver halts the chain on this exception so a
          misconfigured store doesn't quietly fall through to a prompt that
          masks the real config error. A future per-backend ``strict_on_miss``
          toggle on ``[secret_backends.<kind>]`` could opt persistent stores
          back into soft-miss fall-through; not wired today since no backend
          that would honor it ships in this surface.

        Transport / authentication failures (vault locked mid-batch, network
        down) raise ``ConnectivityError`` or ``ExternalError`` rather than
        ``SecretMappingError`` -- the former are ephemeral, the latter is a
        config-shape signal.

        "Not configured for this secret at all" -- i.e. no default convention
        and no explicit mapping -- is signaled via ``would_attempt`` returning
        False and never calls ``get``; that's separate from a miss.
        """
        ...

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Batch resolve. Backends that authenticate (1Password, Vault)
        override to amortize that cost across the batch. ``PromptSource``
        overrides to emit all prompts in one operator interaction.
        """
        ...

    def describe_lookup(self, secret: SecretDecl) -> str | None:
        """Human-readable identifier this source would use to look up
        ``secret`` (post-mapping resolution): env var name, vault path,
        ``op://`` URI, etc. Returns None for sources with no static
        identifier -- prompt always attempts but its "lookup" is the
        operator typing at command time.

        Pure config-derived; never probes the backend. Used by
        ``agw secret list`` to render the per-(secret, backend) table
        cell so operators can see what each backend would look up
        without needing to compute the convention by hand. The renderer
        composes the cell as:

        - ``would_attempt(secret) == False`` -> ``disabled``
        - ``would_attempt(secret) == True`` and identifier is None -> ``enabled``
        - identifier is a non-empty string -> the identifier itself
        """
        ...


class SecretSourceBase(ABC):
    """Default base class for SecretSource implementations.

    Provides a default ``batch_get`` that loops ``get``. Concrete sources
    inherit from this for the shared default and implement ``would_attempt``
    and ``get`` (plus override ``batch_get`` when the backend benefits from
    amortizing per-batch cost).

    ``SecretSource`` remains the type contract; this base class is purely a
    sharing convenience. Code that does not need the default ``batch_get`` can
    implement ``SecretSource`` structurally without inheriting from this base.
    """

    kind: str

    @abstractmethod
    def would_attempt(self, secret: SecretDecl) -> bool:
        ...

    @abstractmethod
    def get(self, secret: SecretDecl) -> str | None:
        ...

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        out: dict[str, str] = {}
        for s in secrets:
            value = self.get(s)
            if value is not None:
                out[s.name] = value
        return out

    def describe_lookup(self, secret: SecretDecl) -> str | None:  # noqa: ARG002 - default
        """Default: no static identifier. Override in sources that have one."""
        return None
