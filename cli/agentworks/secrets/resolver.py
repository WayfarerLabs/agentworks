"""SecretResolver: walks the configured backend chain, batches lookups.

See FRD R4 and HLA "Secret model" / "Eager prompting flow" for the design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, SecretUnavailableError
from agentworks.secrets.base import SecretDecl

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.env.entry import EnvEntry
    from agentworks.secrets.base import SecretSource


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

    @property
    def sources(self) -> tuple[SecretSource, ...]:
        """Active source chain in precedence order. Read-only view for
        introspection (``agw secret list``, etc.); resolution paths
        continue to use ``self._sources`` directly."""
        return tuple(self._sources)

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

    def first_attempting_source(self, secret: SecretDecl) -> SecretSource | None:
        """The first source in precedence order that would attempt this
        secret. Used by ``agw doctor`` for the "would I get prompted?"
        preview.
        """
        for s in self._sources:
            if s.would_attempt(secret):
                return s
        return None

    def preview_resolution(self, secret: SecretDecl) -> str | None:
        """Return the kind of the first source that would resolve ``secret``,
        or ``None`` if no source in the active chain would.

        Walks the chain in precedence order. ``would_attempt`` gates each
        source; a source opted out via ``backend_mappings`` is skipped.
        Prompt has no probe-safe ``get`` (calling it would prompt the
        operator), so if prompt's ``would_attempt`` is True it's reported
        as the resolver without calling ``get``. Every other source must
        return non-None from ``get`` to be reported.

        Used by ``agw doctor`` to tell the operator which backend will
        satisfy each declared secret at command time.
        """
        for source in self._sources:
            if not source.would_attempt(secret):
                continue
            if source.kind == "prompt":
                return source.kind
            if source.get(secret) is not None:
                return source.kind
        return None

    def resolve_all(self, secrets: list[SecretDecl]) -> dict[str, str]:
        """Batch-resolve every secret through the chain.

        Each source's ``batch_get`` is called once with the still-missing
        set; values it returns are cached and removed from the missing set;
        the next source sees only what is still unresolved. If a secret is
        still unresolved after every source, raises ``SecretUnavailableError``.

        Cache hits (secrets resolved in a previous call within the same
        resolver instance) are returned without re-consulting any source.

        Soft misses (a source returning ``None`` from ``get``) fall through
        to the next source naturally. Hard misses (a persistent-store backend
        raising ``SecretMappingError``) halt the chain: the exception
        propagates out so a misconfigured store doesn't fall through to a
        prompt and mask the real config error. See ``SecretSource.get`` for
        the soft-vs-hard contract.
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
                # ADR 0014: embedded newlines would corrupt SSH
                # `-o SetEnv=KEY=VALUE` arguments. The env-var source
                # already strips trailing newlines (the common copy-paste
                # artifact); anything still containing one is a malformed
                # secret value and a hard error worth surfacing now
                # rather than as an opaque SSH-side rejection. NULs are
                # rejected for the same reason: OpenSSH's argv handling
                # would silently truncate the SetEnv arg at the first NUL.
                if "\n" in value or "\r" in value or "\0" in value:
                    raise ConfigError(
                        f"secret {name!r}: resolved value contains a "
                        f"control character (newline, carriage return, "
                        f"or NUL); cannot transport via SSH SetEnv. Fix "
                        f"the value at the source (e.g. strip trailing "
                        f"newlines from the env var or vault entry).",
                    )
                self._cache[name] = value
                out[name] = value
            missing = [s for s in missing if s.name not in resolved]

        if missing:
            sorted_missing = sorted(missing, key=lambda d: d.name)
            names = [d.name for d in sorted_missing]
            # Per-secret backend list: only sources that actually attempted
            # (would_attempt == True) appear, so a secret with env-var opted out
            # via backend_mappings doesn't get told "env-var was tried".
            per_secret = []
            for d in sorted_missing:
                attempted = [s.kind for s in self._sources if s.would_attempt(d)]
                kinds = ", ".join(attempted) if attempted else "(none; secret unreachable)"
                per_secret.append(f"{d.name}: tried {kinds}")
            raise SecretUnavailableError(
                f"no active backend could resolve secret(s): {', '.join(names)}",
                hint="; ".join(per_secret),
            )
        return out

    def render(
        self,
        env: Mapping[str, EnvEntry],
    ) -> dict[str, str]:
        """Resolve an effective-env dict into concrete ``{KEY: value}``.

        Phase 2 of the env-and-secrets effort introduced the EnvEntry type;
        this signature was widened to ``dict[str, object]`` during Phase 1
        to delay the env-package dependency. Now narrowed to the natural
        shape: a mapping of env-var name to EnvEntry (which carries either
        ``.value`` plaintext or ``.secret`` reference).

        Phase 1b of the Resource Registry SDD relaxes the strict
        "must declare" rule: a secret reference whose name has no
        ``[secrets.<name>]`` block is treated as auto-declared
        (synthesized ``SecretDecl`` with empty ``backend_mappings``) and
        runs through the same backend chain. If no backend resolves it,
        the existing ``SecretUnavailableError`` path fires at command
        time rather than ``ConfigError`` at config load. Operators see
        the auto-declared secret in ``agw secret list`` with origin =
        auto-declared so typo'd names surface visibly.

        The exhaustive-or-else case (entry has neither value nor secret)
        cannot happen by construction because EnvEntry's
        ``__post_init__`` enforces the exactly-one invariant.
        """
        seen: set[str] = set()
        needed: list[SecretDecl] = []
        for entry in env.values():
            if entry.secret is not None and entry.secret not in seen:
                seen.add(entry.secret)
                # Use the operator-declared SecretDecl if present (carries
                # backend_mappings overrides); otherwise synthesize a
                # bare one (auto-declare semantics: backends apply their
                # default conventions). The synthesized shape matches
                # ``_SecretKind.synthesize`` (in
                # ``agentworks.resources.kinds.secret``) modulo ``origin``,
                # which the resolver doesn't read -- so render-side
                # synthesize and registry-side synthesize converge on the
                # same resolution outcome by construction.
                needed.append(
                    self._decls.get(entry.secret)
                    or SecretDecl(name=entry.secret, description="")
                )
        resolved = self.resolve_all(needed) if needed else {}

        out: dict[str, str] = {}
        for key, entry in env.items():
            if entry.secret is not None:
                out[key] = resolved[entry.secret]
            else:
                # EnvEntry invariant: exactly one of value/secret set, so
                # value is non-None when secret is None.
                assert entry.value is not None
                out[key] = entry.value
        return out

    def required_for(self, env: Mapping[str, EnvEntry]) -> list[SecretDecl]:
        """Return deduplicated SecretDecls referenced by ``env``, filtered
        to those operator-declared in ``self._decls``.

        Used by eager-prompting orchestration to compute the union of
        needed secrets across a candidate target set before resolving.
        Phase 1b note: secret refs whose names are NOT in ``self._decls``
        (auto-declarable via the Registry's miss policy) are intentionally
        excluded -- eager prompting walks operator-declared secrets only,
        since auto-declared ones carry no operator-set description / hint
        worth pre-flighting. ``render`` handles the auto-declarable case
        on demand at command time.
        """
        seen: set[str] = set()
        out: list[SecretDecl] = []
        for entry in env.values():
            if entry.secret is not None and entry.secret in self._decls and entry.secret not in seen:
                seen.add(entry.secret)
                out.append(self._decls[entry.secret])
        return out
