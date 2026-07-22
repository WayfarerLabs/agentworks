"""The secret-backend capability registry.

Three distinct pieces meet in the secret system, and keeping them
distinct is the design (ADR 0016):

- CONFIG: ``[secret_config].backends``, the active chain. A setting,
  not a resource.
- RESOURCES: backends are capability resources -- read-only
  ``secret-backend`` rows, one per registered capability, so the chain
  and per-secret ``backend_mappings`` validate through the framework's
  uniform machinery and the backends list/describe like every other
  resource.
- IMPLEMENTATIONS: this module. ``SECRET_BACKEND_REGISTRY`` holds the
  code behind those rows (``env-var``, ``prompt``, ``onepassword``;
  later plugin-registered backends). Capability kinds have no
  declarable form; ``SecretBackend`` is an ordinary well-defined API
  abstracting where secrets actually come from, consumed by the
  resolution loop (``agentworks.secrets.resolve``).

There is no instantiation layer between the chain and the capability
(ADR 0016): resources and config reference backends directly,
many-to-one. That makes secret-backend the one capability whose
consumers name it directly, with no intermediate declarable to hold
shared config: contrast vm-platform (fronted by vm-site) and
git-credential-provider (fronted by git-credential), where a declarable
resource homes the capability config and the many consumers reference
it. Here the per-secret ``backend_mappings`` (keyed by backend name) is
the only config surface, so it substitutes for that missing per-instance
layer. That substitution holds only while backends carry no account-level
config: env-var, prompt, and the onepassword CLI backend (which reads the
operator's ambient ``op`` state) need none. When a backend needs config
SHARED across many secrets (a store account, a transport, a Connect
host), the per-secret mapping is the wrong home for it (vastly
many-to-one): that is the signal to graduate the backend to a declarable
instance kind, the secret-backend analog of vm-site. The graduation is
additive (ADR 0016 sanctions it for, e.g., multiple 1Password accounts),
so nothing here needs it until then.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentworks.secrets.env_var import EnvVarBackend
from agentworks.secrets.onepassword import OnePasswordBackend
from agentworks.secrets.prompt import PromptBackend

if TYPE_CHECKING:
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import MappingValue, SecretDecl


class SecretBackend(Protocol):
    """The secret-domain capability: a pluggable store of secret values.

    Backends are STATELESS. All methods MUST be cheap and
    side-effect-free except ``batch_get`` (which resolves values and,
    for the prompt backend, interacts with the operator). Expensive
    setup (store sessions, CLI subprocesses) belongs inside
    ``batch_get``, amortized across the batch.

    The ``mapping`` parameter is the secret's ``backend_mappings`` entry
    for this backend (string identifier, structured dict, or absent).
    The generic ``False`` opt-out never reaches a backend -- the
    resolution loop handles it.

    Miss contract for ``batch_get``: a secret the backend has no value
    for is simply ABSENT from the result (soft miss -- the resolve loop
    falls through to the next backend). A persistent-store backend
    raises ``SecretMappingError`` when an explicit mapping definitively
    has no value (hard miss -- halts the chain so a misconfigured store
    doesn't quietly fall through to a prompt that masks the real config
    error). Transport / auth failures raise ``ConnectivityError`` /
    ``ExternalError``.

    ``interactive`` marks a backend whose resolution may involve operator
    interaction: ``batch_get`` can block on the operator (the prompt
    backend asks for the value; the onepassword backend may trigger a
    biometric or re-auth through ``op``). Inspection previews never probe
    an interactive backend, since probing would BE that interaction; they
    report it optimistically on ``would_attempt`` alone.

    A resolution that needs an operator present cannot run headless, so a
    fully non-interactive path resolves by dropping interactive backends
    from its chain. A future 1Password transport that authenticates without
    a human (Connect, a service account) would not be interactive; that is
    a separate backend or config, not this one.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def interactive(self) -> bool: ...

    def validate_mapping(
        self,
        owner: str,
        mapping: MappingValue,
    ) -> None:
        """Validate one ``backend_mappings`` value addressed to this
        backend -- capability-owned config in its per-secret host.
        Invoked by ``validate_chain`` for active-chain backends so a
        malformed mapping fails at ``build_registry`` with config
        vocabulary instead of at first resolution. The generic ``False``
        opt-out never reaches this. ``owner`` is display context.

        REQUIRED, not defaulted: Protocol bodies are not inherited by
        structural implementers, so every registered backend must
        implement this (a backend with no mapping vocabulary rejects
        everything, as prompt does).

        NOTE: this invoked-validation API may be deprecated in favor of
        capabilities pushing a declarative config schema definition at
        registration time, letting the core engine validate (and derive
        any implied references) without invoking the capability.
        """
        ...

    def would_attempt(
        self,
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> bool: ...

    def describe_lookup(
        self,
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> str | None: ...

    def batch_get(
        self,
        wants: list[tuple[SecretDecl, MappingValue | None]],
    ) -> dict[str, str]: ...


SECRET_BACKEND_REGISTRY: dict[str, SecretBackend] = {
    "env-var": EnvVarBackend(),
    "prompt": PromptBackend(),
    "onepassword": OnePasswordBackend(),
}
"""The capability registry. Future plugins register here (and publish
their own capability resources with plugin origins)."""


def publish_to(registry: Registry) -> None:
    """Publish one ``secret-backend`` capability resource per registered
    backend, ``built-in`` origin. Read-only rows: the chain and
    per-secret mappings validate against them uniformly, and the
    backends list/describe like every other resource.
    """
    from agentworks.resources import Origin
    from agentworks.secrets.kinds import SecretBackendEntry

    origin = Origin.built_in(source="agentworks.secrets")
    for name, backend in SECRET_BACKEND_REGISTRY.items():
        registry.add(
            "secret-backend",
            name,
            SecretBackendEntry(name=name, description=backend.description),
            origin,
        )
