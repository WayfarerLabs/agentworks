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
  code behind those rows: the two core backends (``env-var``,
  ``prompt``) plus any plugin-registered backends seated at import
  (``onepassword`` ships as the ``onepassword`` system plugin, whose
  adapter seats its instance here; its ROW, unlike the core two, is
  published by ``plugins.publish_plugins`` with a ``system-plugin``
  origin, so the built-in publisher skips it). Capability kinds have no
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

from typing import TYPE_CHECKING, Any, Protocol

from agentworks.secrets.env_var import EnvVarBackend
from agentworks.secrets.prompt import PromptBackend

if TYPE_CHECKING:
    from agentworks.resources.graph import Readiness
    from agentworks.schema import AgwRootModel
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

    contract_version: int
    """The secret-backend contract version this implementation is written
    against, compared at registration to the version the kind's descriptor
    declares supported. REQUIRED as a class attribute, not defaulted:
    Protocol bodies are not inherited by structural implementers, so unlike
    the ``Capability`` ABC kinds (whose base carries the default) every
    backend spells it."""

    config_model: type[AgwRootModel[Any]]
    """The model this backend's per-secret ``backend_mappings`` value is
    validated against, declared as a class attribute exactly as
    ``contract_version`` is.

    A ROOT model, and that is the kind's contract rather than this
    backend's choice: a mapping value may be a bare string (env-var's is
    an env var name), which no ``BaseModel`` can be. The core validates
    against it and derives whatever references it implies; no backend
    code runs for either.

    The generic ``False`` opt-out is NOT part of what a model expresses:
    the resolve loop strips it before any backend sees a mapping, so an
    arm for it would declare a value that cannot arrive."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def interactive(self) -> bool: ...

    def not_ready(self) -> Readiness:
        """Why this backend cannot resolve on this host, or ready when it
        can. Config-INDEPENDENT (a backend's host tool is present or not,
        irrespective of any per-secret mapping), so it takes no argument:
        the readiness fold (LLD c) calls it once per ``secret-backend`` node
        and stores the verdict (R9.6 gives backends offline readiness).

        This is the per-kind refinement of the uniform capability
        ``not_ready(config)`` hook: secret-backend impls are instances (LLD
        a) whose readiness is config-free, so the no-arg instance form is the
        honest shape, and the fold's per-kind dispatch contains the asymmetry.

        Offline and cheap by contract: a pure presence test (``op`` on PATH),
        never a store probe, biometric, or re-auth (that is
        interactive-optimism's concern at resolution time, kept optimistic).
        REQUIRED, not defaulted (Protocol bodies are not inherited by
        structural implementers): every registered backend implements it.
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
}
"""The core capability registry: the two always-available backends
(``env-var``, ``prompt``). The ``onepassword`` backend now ships as the
``onepassword`` system plugin (``agentworks.plugins.onepassword``), which
seats its instance here at import through the ``secret-backend`` adapter;
future plugins register the same way (and publish their own capability
resources with ``system-plugin`` origins)."""
