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
  code behind those rows (``env-var``, ``prompt``; later
  ``onepassword``, plugin-registered backends). Capability kinds have
  no declarable form; ``SecretBackend`` is an ordinary well-defined API
  abstracting where secrets actually come from, consumed by the
  resolution loop (``agentworks.secrets.resolve``).

There is no instantiation layer between the chain and the capability
(ADR 0016): resources and config reference backends directly,
many-to-one. Per-secret behavior lives in
``backend_mappings`` (keyed by backend name); if a backend someday
genuinely needs multiple configured instances, a declarable instance
kind for that backend is an additive graduation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentworks.secrets.env_var import EnvVarBackend
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

    ``interactive`` marks backends whose ``batch_get`` IS an operator
    interaction (prompt); inspection previews never probe those.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def interactive(self) -> bool: ...

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
"""The capability registry. Future plugins register here (and publish
their own capability resources with plugin origins)."""


def publish_to(registry: Registry) -> None:
    """Publish one ``secret-backend`` capability resource per registered
    backend, ``built-in`` origin. Read-only rows: the chain and
    per-secret mappings validate against them uniformly, and the
    backends list/describe like every other resource.
    """
    from agentworks.resources import Origin
    from agentworks.resources.kinds.secret_backend import SecretBackendEntry

    origin = Origin.built_in(source="agentworks.secrets")
    for name, backend in SECRET_BACKEND_REGISTRY.items():
        registry.add(
            "secret-backend",
            name,
            SecretBackendEntry(name=name, description=backend.description),
            origin,
        )
