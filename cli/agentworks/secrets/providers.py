"""The secret-provider capability registry.

Three distinct layers meet in the secret system, and keeping them
distinct is the design (ADR 0016):

- CONFIG: ``[secret_config].backends``, the active chain. A setting,
  not a resource.
- RESOURCES: ``secret-backend`` rows in the resource Registry (bundled
  built-ins or operator manifests) -- the exposed resources, and THE
  DOOR: all runtime access to a capability goes through a backend's
  methods (``agentworks.secrets.base.SecretBackendDecl``).
- RAW CAPABILITIES: this module. ``SECRET_PROVIDER_REGISTRY`` holds the code
  implementations (``env-var``, ``prompt``; later ``onepassword``,
  plugin-registered providers). Providers are not resources and have no
  kind; the provider API below is consumed only by backend door methods
  (plus ``validate_config`` at manifest decode).

Each provider is mirrored into the resource Registry as a read-only
``secret-provider`` descriptor row so backend ``provider`` references
validate through the framework's uniform miss policy and providers show
up in ``agw resource list`` -- this registry remains the source of truth
for the logic itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agentworks.secrets.env_var import EnvVarProvider
from agentworks.secrets.prompt import PromptProvider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import MappingValue, SecretDecl


class SecretProvider(Protocol):
    """The raw capability a ``secret-backend`` resource exposes.

    Providers are STATELESS: every call receives the backend's
    ``config``. All methods MUST be cheap and side-effect-free except
    ``batch_get`` (which resolves values and, for the prompt provider,
    interacts with the operator). Expensive setup (store sessions, CLI
    subprocesses) belongs inside ``batch_get``, amortized across the
    batch.

    ``validate_config`` runs at manifest decode (errors wrapped with the
    document's ``file:line``) and again defensively at chain validation.

    Miss contract for ``batch_get``: a secret the provider has no value
    for is simply ABSENT from the result (soft miss -- the resolve loop
    falls through to the next backend). A persistent-store provider
    raises ``SecretMappingError`` when an explicit mapping definitively
    has no value (hard miss -- halts the chain so a misconfigured store
    doesn't quietly fall through to a prompt that masks the real config
    error). Transport / auth failures raise ``ConnectivityError`` /
    ``ExternalError``.

    ``interactive`` marks providers whose ``batch_get`` IS an operator
    interaction (prompt); inspection previews never probe those.
    """

    @property
    def name(self) -> str: ...

    @property
    def interactive(self) -> bool: ...

    def validate_config(
        self, backend_name: str, config: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    def would_attempt(
        self,
        config: Mapping[str, object],
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> bool: ...

    def describe_lookup(
        self,
        config: Mapping[str, object],
        secret: SecretDecl,
        mapping: MappingValue | None,
    ) -> str | None: ...

    def batch_get(
        self,
        config: Mapping[str, object],
        wants: list[tuple[SecretDecl, MappingValue | None]],
    ) -> dict[str, str]: ...


SECRET_PROVIDER_REGISTRY: dict[str, SecretProvider] = {
    "env-var": EnvVarProvider(),
    "prompt": PromptProvider(),
}
"""The capability registry. Future plugins register here (and publish
their own descriptor rows with plugin origins)."""


def publish_to(registry: Registry) -> None:
    """Publish one ``secret-provider`` descriptor row per registered
    provider, ``built-in`` origin. Read-only rows: they exist so backend
    ``provider`` references validate uniformly and providers are visible
    in ``agw resource list``.
    """
    from agentworks.resources import Origin
    from agentworks.resources.kinds.secret_provider import SecretProviderEntry

    origin = Origin.built_in(source="agentworks.secrets")
    for name in SECRET_PROVIDER_REGISTRY:
        registry.add(
            "secret-provider", name, SecretProviderEntry(name=name), origin
        )
