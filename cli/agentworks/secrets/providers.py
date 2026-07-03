"""Secret providers: the code capabilities behind secret backends.

A provider is code (``env-var``, ``prompt``; later ``onepassword``, ...);
a backend is a named, configured instantiation of one (a Resource,
declarable in manifests). Providers register here code-side and are
mirrored into the Registry as read-only ``secret-provider`` descriptor
rows so backend references validate through the framework's uniform
miss policy and providers show up in ``agw resource list``.

This module also owns the chain plumbing over a finalized registry:
``validate_chain`` (the semantic reachability check the
``secret-config`` kind's ``validate`` hook delegates to -- runs at
``Registry.finalize``) and ``resolver_for`` (registry-pure resolver
assembly, replacing ``Config.secret_resolver`` -- resource-manifests
SDD, Phase 3). By finalize the graph already guarantees every chain
name resolves to a ``secret-backend`` row (the ``secret-config`` row's
references), so assembly is a plain projection. The resolver instance
carries the per-command resolved-value cache (prompt-once); it is
memoized per Registry, and the standard registry is itself a per-Config
singleton (see ``bootstrap.build_registry``), so every caller in a
command shares one resolver.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any, Protocol

from agentworks.errors import ConfigError
from agentworks.secrets.base import SecretBackendConfig, SecretBackendDecl
from agentworks.secrets.env_var import EnvVarSource
from agentworks.secrets.prompt import PromptSource
from agentworks.secrets.resolver import SecretResolver

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretSource


class SecretProvider(Protocol):
    """The code capability a ``secret-backend`` resource instantiates.

    ``validate_config`` runs at manifest decode (errors are wrapped with
    the document's ``file:line``); ``instantiate`` runs at resolver
    assembly. Built-in providers accept no configuration; the
    config-bearing contract is exercised by a test-only provider until a
    real one (onepassword, ...) ships.
    """

    @property
    def name(self) -> str: ...

    def validate_config(
        self, backend_name: str, config: Mapping[str, object]
    ) -> Mapping[str, object]: ...

    def instantiate(
        self, backend_name: str, config: Mapping[str, object]
    ) -> SecretSource: ...


class _NoConfigProvider:
    """Provider base for capabilities that take no per-backend config."""

    def __init__(self, name: str, factory: Callable[[], SecretSource]) -> None:
        self.name = name
        self._factory = factory

    def validate_config(
        self, backend_name: str, config: Mapping[str, object]
    ) -> Mapping[str, object]:
        if config:
            raise ConfigError(
                f'secret-backend "{backend_name}": the {self.name} provider '
                f"accepts no configuration; got {sorted(config)}"
            )
        return {}

    def instantiate(
        self, backend_name: str, config: Mapping[str, object]
    ) -> SecretSource:
        self.validate_config(backend_name, config)
        return self._factory()


PROVIDER_REGISTRY: dict[str, SecretProvider] = {
    "env-var": _NoConfigProvider("env-var", EnvVarSource),
    "prompt": _NoConfigProvider("prompt", PromptSource),
}
"""Code-side provider registry. Future plugins add entries here (and
publish their own descriptor rows with plugin origins)."""


def publish_to(registry: Registry) -> None:
    """Publish one ``secret-provider`` descriptor row per registered
    provider, ``built-in`` origin. Read-only rows: they exist so backend
    ``provider`` references validate uniformly and providers are visible
    in ``agw resource list``.
    """
    from agentworks.resources import Origin
    from agentworks.resources.kinds.secret_provider import SecretProviderEntry

    origin = Origin.built_in(source="agentworks.secrets")
    for name in PROVIDER_REGISTRY:
        registry.add(
            "secret-provider", name, SecretProviderEntry(name=name), origin
        )


# One resolver per Registry instance. The standard registry is itself a
# per-Config singleton (see bootstrap.build_registry), so all default
# call paths in a command share one resolver -- the prompt-once cache
# identity -- while explicitly-built registries (tests, custom
# orchestration) get their own resolver matching their own rows.
_RESOLVERS: weakref.WeakKeyDictionary[Any, SecretResolver] = (
    weakref.WeakKeyDictionary()
)


def _chain_sources(registry: Registry) -> tuple[tuple[str, ...], list[SecretSource]]:
    """The active chain and its instantiated sources, read entirely from
    the registry: ``secret-config:default`` (always present after
    finalize -- published by ``Config.publish_to`` or seeded by
    always-materialize) names ``secret-backend`` rows whose existence
    the reference graph already guaranteed.
    """
    chain: tuple[str, ...] = registry.lookup("secret-config", "default").backends
    sources: list[SecretSource] = []
    for name in chain:
        try:
            row = registry.lookup("secret-backend", name)
        except KeyError:
            # Unreachable through finalize (the secret-config row's
            # references force the miss policy first); backstop for
            # hand-built registries that skipped publishing the row.
            raise ConfigError(
                f"secret-config references unknown secret-backend {name!r}"
            ) from None
        sources.append(_source_for(row, name))
    return chain, sources


def validate_chain(registry: Registry) -> None:
    """Finalize-time semantic validation for the secret system, called
    by the ``secret-config`` kind's ``validate`` hook: chain sources
    must instantiate (unknown/legacy provider errors surface here) and
    every operator-declared secret must be reachable via the chain.

    The reachability check covers operator-declared secrets only,
    preserving the env-and-secrets SDD's load-time behavior (it ran over
    ``Config.secrets``). Auto-declared rows (e.g. the ever-present
    tailscale-auth-key) must not invalidate a deliberate
    ``backends = []`` opt-out; they surface at use time as
    ``SecretUnavailableError`` instead.
    """
    from agentworks.resources.access import secret_decls

    chain, sources = _chain_sources(registry)
    secrets = secret_decls(registry)
    probe = SecretResolver(sources, secrets)

    operator_names = {
        name
        for name, decl in secrets.items()
        if getattr(getattr(decl, "origin", None), "variant", None)
        == "operator-declared"
    }
    unreachable = [
        decl
        for decl in probe.unreachable_secrets()
        if decl.name in operator_names
    ]
    if unreachable:
        names = ", ".join(sorted(d.name for d in unreachable))
        chain_str = ", ".join(chain) or "(empty)"
        # Tight by construction: with the default chain (env-var,
        # prompt), prompt attempts every secret, so nothing is
        # unreachable. Reaching this error means the operator stripped
        # prompt AND the remaining backends opt out (or backends = []).
        raise ConfigError(
            f"unreachable secret(s): {names}",
            hint=(
                f"active backend chain: [{chain_str}]. Each declared secret "
                "needs at least one backend in the chain that would attempt "
                "it. To fix: add 'prompt' (or another always-attempting backend) "
                "to [secret_config].backends; drop a "
                "`backend_mappings.<kind> = false` opt-out on the affected "
                "secret(s); add `backend_mappings.<kind>` for a backend that "
                "has no default convention (e.g. 1password); or remove the "
                "unused secret declaration."
            ),
        )


def _source_for(row: Any, name: str) -> SecretSource:
    if isinstance(row, SecretBackendDecl):
        provider = PROVIDER_REGISTRY.get(row.provider)
        if provider is None:
            # The framework's miss policy reports unknown providers at
            # finalize; reaching here means a descriptor row exists
            # without code, which is a registration bug.
            raise ConfigError(
                f'secret-backend "{name}" names provider {row.provider!r}, '
                "which has no registered implementation"
            )
        return provider.instantiate(name, row.config)
    if isinstance(row, SecretBackendConfig):
        # Legacy TOML [secret_backends.<kind>] row: its kind IS the
        # provider name. Survives until the Phase 5 cutover.
        provider = PROVIDER_REGISTRY.get(row.kind)
        if provider is None:
            raise ConfigError(
                f'[secret_backends.{row.kind}] declares an unknown backend '
                f"kind; supported: {sorted(PROVIDER_REGISTRY)}"
            )
        return provider.instantiate(name, {})
    raise ConfigError(
        f'secret-backend "{name}" has an unexpected row type '
        f"{type(row).__name__}"
    )


def resolver_for(registry: Registry) -> SecretResolver:
    """The ``SecretResolver`` for a finalized registry: a plain
    projection of the ``secret-config`` chain onto instantiated backend
    sources plus the registry's secret rows. All validation (chain names
    via the reference graph, reachability and provider instantiation via
    the ``secret-config`` kind's ``validate`` hook) already ran at
    ``Registry.finalize`` -- the runtime reads, the registry validated.

    Memoized per Registry instance: the resolver carries the per-command
    resolved-value cache (prompt-once), and the standard registry is a
    per-Config singleton (``bootstrap.build_registry``), so every caller
    in a command shares ONE resolver, while an explicitly-built registry
    gets a resolver matching its own rows.
    """
    cached = _RESOLVERS.get(registry)
    if cached is not None:
        return cached

    from agentworks.resources.access import secret_decls

    _, sources = _chain_sources(registry)
    resolver = SecretResolver(sources, secret_decls(registry))
    _RESOLVERS[registry] = resolver
    return resolver
