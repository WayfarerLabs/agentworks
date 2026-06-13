"""SecretResolver: walks the configured backend chain, batches lookups.

See FRD R4 and HLA "Secret model" / "Eager prompting flow" for the design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import SecretUnavailableError

if TYPE_CHECKING:
    from agentworks.secrets.base import SecretDecl, SecretSource


class SecretResolver:
    """Resolves declared secrets through an ordered chain of SecretSources.

    Cache lifetime is the resolver instance (typically one per CLI command).
    A future controller-process caller will need to revisit cache lifetime
    (TTL, revocation hooks) since its process lifetime is much longer than
    a single command.
    """

    def __init__(
        self,
        sources: list[SecretSource],
        decls: dict[str, SecretDecl],
    ) -> None:
        self._sources = sources
        self._decls = decls
        self._cache: dict[str, str] = {}

    def unreachable_secrets(self) -> list[SecretDecl]:
        """Secrets that no active source would attempt to resolve.

        Loader calls this at config-load time to surface unreachable
        secrets as a config-time error: if any are present, no command
        will ever be able to resolve them, so config is broken.
        """
        return [
            d for d in self._decls.values()
            if not any(s.would_attempt(d) for s in self._sources)
        ]

    def skipping_sources(self, secret: SecretDecl) -> list[SecretSource]:
        """Active sources that will not attempt to resolve this secret.

        Used by ``agw doctor`` to report soft-skip findings ("backend X has
        no mapping for secret Y; will skip").
        """
        return [s for s in self._sources if not s.would_attempt(secret)]

    def first_attempting_source(self, secret: SecretDecl) -> SecretSource | None:
        """The first source in precedence order that would attempt this
        secret. Used by ``agw doctor`` for the "would I get prompted?"
        preview.
        """
        for s in self._sources:
            if s.would_attempt(secret):
                return s
        return None

    def resolve_all(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Batch-resolve every secret through the chain.

        Each source's ``batch_get`` is called once with the still-missing
        set; values it returns are cached and removed from the missing set;
        the next source sees only what is still unresolved. If a secret is
        still unresolved after every source, raises ``SecretUnavailableError``.

        Cache hits (secrets resolved in a previous call within the same
        resolver instance) are returned without re-consulting any source.
        """
        out: dict[str, str] = {}
        missing: list[SecretDecl] = []
        for s in secrets:
            cached = self._cache.get(s.name)
            if cached is not None:
                out[s.name] = cached
            else:
                missing.append(s)

        for source in self._sources:
            if not missing:
                break
            still_attemptable = [s for s in missing if source.would_attempt(s)]
            if not still_attemptable:
                continue
            resolved = source.batch_get(still_attemptable)
            for name, value in resolved.items():
                self._cache[name] = value
                out[name] = value
            missing = [s for s in missing if s.name not in resolved]

        if missing:
            names = sorted(s.name for s in missing)
            tried = ", ".join(s.kind for s in self._sources) or "(none)"
            raise SecretUnavailableError(
                f"no active backend could resolve secret(s): {', '.join(names)}",
                hint=f"backends tried: {tried}",
            )
        return out

    def render(
        self,
        env: dict[str, object],
    ) -> dict[str, str]:
        """Resolve an effective-env dict into concrete ``{KEY: value}``.

        ``env`` values are either plaintext strings or ``EnvEntry``-shaped
        objects with ``.value`` (plaintext) or ``.secret`` (secret name
        referencing ``self._decls``). To keep this module independent of
        the env package, we duck-type on attributes rather than importing
        ``EnvEntry``: any object with a ``.value`` or ``.secret`` attribute
        works.
        """
        needed: list[SecretDecl] = []
        for entry in env.values():
            secret_name = getattr(entry, "secret", None)
            if secret_name and secret_name in self._decls:
                decl = self._decls[secret_name]
                if decl not in needed:
                    needed.append(decl)
        resolved = self.resolve_all(needed) if needed else {}

        out: dict[str, str] = {}
        for key, entry in env.items():
            secret_name = getattr(entry, "secret", None)
            value = getattr(entry, "value", None)
            if secret_name:
                out[key] = resolved[secret_name]
            elif value is not None:
                out[key] = value
            elif isinstance(entry, str):
                out[key] = entry
        return out

    def required_for(self, env: dict[str, object]) -> list[SecretDecl]:
        """Return deduplicated SecretDecls referenced by ``env``.

        Used by eager-prompting orchestration to compute the union of
        needed secrets across a candidate target set before resolving.
        """
        seen: set[str] = set()
        out: list[SecretDecl] = []
        for entry in env.values():
            secret_name = getattr(entry, "secret", None)
            if secret_name and secret_name in self._decls and secret_name not in seen:
                seen.add(secret_name)
                out.append(self._decls[secret_name])
        return out
