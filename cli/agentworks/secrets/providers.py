"""Secret providers: the code capabilities behind secret backends.

A provider is code (``env-var``, ``prompt``; later ``onepassword``, ...);
a backend is a named, configured instantiation of one (a Resource,
declarable in manifests). Providers register here code-side and are
mirrored into the Registry as read-only ``secret-provider`` descriptor
rows so backend references validate through the framework's uniform
miss policy and providers show up in ``agw resource list``.

This module also owns ``resolver_for``: the registry-derived resolver
assembly that replaced ``Config.secret_resolver`` (resource-manifests
SDD, Phase 3). The resolver instance carries the per-command
resolved-value cache (prompt-once), so ``resolver_for`` memoizes per
Config object: every call with the same Config returns the same
resolver regardless of which ``build_registry`` result accompanies it
(all builds of one config produce equal backend rows).
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

    from agentworks.config import Config
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


# id(config) -> (weakref to the config, its resolver). The weakref guards
# against id reuse after garbage collection handing a stale resolver to
# an unrelated Config (matters in long test sessions).
_RESOLVERS: dict[int, tuple[weakref.ref[Any], SecretResolver]] = {}


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


def resolver_for(config: Config, registry: Registry | None = None) -> SecretResolver:
    """The per-config ``SecretResolver``, assembled from the registry.

    Walks ``[secret_config].backends`` in precedence order; each name
    must be a ``secret-backend`` row (bundled built-in, manifest, or
    legacy TOML). Unknown chain names and unreachable secrets raise
    ``ConfigError`` here -- the relocation of the old load-time checks,
    which could not survive manifest-declared backends.

    ``registry`` is optional: deep call paths (orchestration, render)
    pass only the config and the standard registry is built on the
    first miss. The per-config memo makes that a one-time cost and, more
    importantly, guarantees every caller in a command shares ONE
    resolver instance, preserving the prompt-once cache semantics.
    """
    cached = _RESOLVERS.get(id(config))
    if cached is not None and cached[0]() is config:
        return cached[1]

    if registry is None:
        from agentworks.bootstrap import build_registry

        registry = build_registry(config)

    from agentworks.resources.access import secret_decls

    chain = config.secret_config_data.backends
    secrets = secret_decls(registry)

    sources: list[SecretSource] = []
    for name in chain:
        try:
            row = registry.lookup("secret-backend", name)
        except KeyError:
            raise ConfigError(
                f"[secret_config].backends names unknown backend {name!r}; "
                "declare it as a secret-backend manifest (or use a built-in: "
                f"{sorted(PROVIDER_REGISTRY)})"
            ) from None
        sources.append(_source_for(row, name))

    resolver = SecretResolver(sources, secrets)

    # The reachability check covers operator-declared secrets only,
    # matching the pre-swap behavior (it ran over Config.secrets).
    # Auto-declared rows (e.g. the ever-present tailscale-auth-key)
    # must not invalidate a deliberate `backends = []` opt-out; they
    # surface at use time as SecretUnavailableError instead.
    operator_names = {
        name
        for name, decl in secrets.items()
        if getattr(getattr(decl, "origin", None), "variant", None)
        == "operator-declared"
    }
    unreachable = [
        decl
        for decl in resolver.unreachable_secrets()
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

    _RESOLVERS[id(config)] = (weakref.ref(config), resolver)
    return resolver
