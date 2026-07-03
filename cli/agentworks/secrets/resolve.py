"""The secrets runtime: a loop over the active backends.

Resolution is iterating the needed secrets over each active backend in
chain order -- no resolver object, no cache, no memo (runtime-model LLD
of the resource-manifests SDD). A command resolves ONCE at its
composition root and passes the values down; "prompt-once" is true by
construction. Caching across CLI invocations would be a different
feature with different security properties.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import AgentworksError, ConfigError, SecretUnavailableError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretBackendDecl, SecretDecl


def active_backends(config: Config, registry: Registry) -> list[SecretBackendDecl]:
    """The active chain as backend resources, in precedence order.

    Each layer in its natural role: the chain comes from CONFIG
    (``[secret_config].backends`` -- a setting), the rows from the
    resource Registry. An unknown chain name gets the operator's
    vocabulary -- the chain is config, so the error is a config error,
    not a registry-graph error.
    """
    from agentworks.secrets.providers import PROVIDER_REGISTRY

    backends: list[SecretBackendDecl] = []
    for name in config.secret_config_data.backends:
        try:
            row: SecretBackendDecl = registry.lookup("secret-backend", name)
        except KeyError:
            raise ConfigError(
                f"[secret_config].backends names unknown backend {name!r}",
                hint=(
                    f"declare {name!r} as a secret-backend manifest, or "
                    f"use a built-in backend: {sorted(PROVIDER_REGISTRY)}"
                ),
            ) from None
        backends.append(row)
    return backends


def validate_chain(config: Config, registry: Registry) -> None:
    """Secret-system config consistency, run by ``build_registry`` right
    after finalize: the chain's names must be ``secret-backend`` rows,
    each backend's config must satisfy its provider, and every
    operator-declared secret must be reachable via the chain.

    The chain is pure config consumed here the way any subsystem
    consumes its settings; this is simply the secrets subsystem
    validating its config against the finalized registry, so every
    resource-touching command fails fast with config vocabulary.

    The reachability check covers operator-declared secrets only,
    preserving the env-and-secrets SDD's load-time behavior (it ran over
    ``Config.secrets``). Auto-declared rows (e.g. the ever-present
    tailscale-auth-key) must not invalidate a deliberate
    ``backends = []`` opt-out; they surface at use time as
    ``SecretUnavailableError`` instead.
    """
    from agentworks.resources.access import secret_decls

    backends = active_backends(config, registry)
    for backend in backends:
        # Defensive re-validation: manifest decode already ran this, but
        # hand-built registries and future non-manifest publishers get
        # the same guarantee here.
        backend.validate_config()

    operator_decls = [
        decl
        for decl in secret_decls(registry).values()
        if getattr(getattr(decl, "origin", None), "variant", None)
        == "operator-declared"
    ]
    unreachable = [
        decl
        for decl in operator_decls
        if not any(b.would_attempt(decl) for b in backends)
    ]
    if unreachable:
        names = ", ".join(sorted(d.name for d in unreachable))
        chain_str = ", ".join(b.name for b in backends) or "(empty)"
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
                "`backend_mappings.<backend> = false` opt-out on the affected "
                "secret(s); add `backend_mappings.<backend>` for a backend that "
                "has no default convention (e.g. 1password); or remove the "
                "unused secret declaration."
            ),
        )


def resolve_secrets(
    secrets: list[SecretDecl], backends: list[SecretBackendDecl]
) -> dict[str, str]:
    """Resolve every secret through the active backends, in chain order.

    Each backend's ``resolve`` is called once with the still-missing,
    would-attempt subset; the next backend sees only what remains. Soft
    misses (a backend's provider has no value) fall through naturally;
    hard misses (``SecretMappingError`` from a persistent-store
    provider) propagate, halting the chain. Anything still unresolved
    after every backend raises ``SecretUnavailableError`` with a
    per-secret list of the backends that attempted.
    """
    resolved: dict[str, str] = {}
    deduped: list[SecretDecl] = []
    seen: set[str] = set()
    for s in secrets:
        if s.name not in seen:
            seen.add(s.name)
            deduped.append(s)

    missing = deduped
    for backend in backends:
        if not missing:
            break
        attemptable = [s for s in missing if backend.would_attempt(s)]
        if not attemptable:
            continue
        got = backend.resolve(attemptable)
        # Surface which backend + identifier won so operators can tell
        # env-var-from-shell apart from a fall-through to prompt. For
        # backends without a static identifier (prompt) the
        # parenthetical is omitted. Never includes the resolved value.
        decl_by_name = {s.name: s for s in attemptable}
        for name in sorted(got):
            ident = backend.describe_lookup(decl_by_name[name])
            suffix = f" ({ident})" if ident else ""
            output.detail(f"Resolved {name} via {backend.name}{suffix}")
        for name, value in got.items():
            # ADR 0014: embedded newlines would corrupt SSH
            # `-o SetEnv=KEY=VALUE` arguments. The env-var provider
            # already strips trailing newlines (the common copy-paste
            # artifact); anything still containing one is a malformed
            # secret value and a hard error worth surfacing now rather
            # than as an opaque SSH-side rejection. NULs are rejected
            # for the same reason: OpenSSH's argv handling would
            # silently truncate the SetEnv arg at the first NUL.
            if "\n" in value or "\r" in value or "\0" in value:
                raise ConfigError(
                    f"secret {name!r}: resolved value contains a "
                    f"control character (newline, carriage return, "
                    f"or NUL); cannot transport via SSH SetEnv. Fix "
                    f"the value at the source (e.g. strip trailing "
                    f"newlines from the env var or vault entry).",
                )
            resolved[name] = value
        missing = [s for s in missing if s.name not in got]

    if missing:
        sorted_missing = sorted(missing, key=lambda d: d.name)
        names = [d.name for d in sorted_missing]
        # Per-secret backend list: only backends that actually attempted
        # (would_attempt == True) appear, so a secret with a backend
        # opted out via backend_mappings doesn't get told it was tried.
        per_secret = []
        for d in sorted_missing:
            attempted = [b.name for b in backends if b.would_attempt(d)]
            tried = ", ".join(attempted) if attempted else "(none; secret unreachable)"
            per_secret.append(f"{d.name}: tried {tried}")
        raise SecretUnavailableError(
            f"no active backend could resolve secret(s): {', '.join(names)}",
            hint="; ".join(per_secret),
        )
    return resolved


def preview_resolution(
    secret: SecretDecl, backends: list[SecretBackendDecl]
) -> str | None:
    """The name of the first backend that would resolve ``secret``, or
    ``None`` if nothing in the chain would.

    Walks the chain in precedence order. ``would_attempt`` gates each
    backend; an interactive backend (prompt) is reported without probing
    (probing would BE the operator interaction); every other backend
    must actually produce a value to be reported.

    Used by ``agw doctor`` and the describe view's resolution preview.
    """
    for backend in backends:
        if not backend.would_attempt(secret):
            continue
        if backend.interactive:
            return backend.name
        try:
            resolved = backend.resolve([secret])
        except AgentworksError:
            # A probe failure (store hard-miss, connectivity) must not
            # abort an inspection surface (doctor, describe); the
            # backend simply doesn't preview as resolving. The real
            # resolve path keeps its hard-miss halt semantics.
            continue
        if secret.name in resolved:
            return backend.name
    return None
