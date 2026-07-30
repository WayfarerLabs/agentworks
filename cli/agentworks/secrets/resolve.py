"""The secrets runtime: a loop over the active backends.

Resolution is iterating the needed secrets over each active backend in
chain order -- no resolver object, no cache, no memo (ADR 0016). A
command resolves ONCE at its
composition root and passes the values down; "prompt-once" is true by
construction. Caching across CLI invocations would be a different
feature with different security properties.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks import output
from agentworks.errors import AgentworksError, ConfigError, SecretUnavailableError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources.graph import Readiness
    from agentworks.resources.registry import Registry
    from agentworks.secrets.backends import SecretBackend
    from agentworks.secrets.base import MappingValue, SecretDecl


@dataclass(frozen=True)
class ActiveBackend:
    """One chain entry at runtime: a registered capability, its stored
    readiness verdict, plus the loop-side orchestration (mapping lookup and the
    generic ``False`` opt-out). Not a resource, just a thin wrapper the
    resolution loop and inspection surfaces share so the opt-out is enforced
    structurally in one place (a ``False`` mapping never reaches the capability).

    ``readiness`` is the verdict the fold stored on the backend's graph node
    (read by :func:`active_backends`, never recomputed, R11). Resolution gates
    on it (R9.6): a not-ready backend is skipped with a warning.
    """

    capability: SecretBackend
    readiness: Readiness

    @property
    def name(self) -> str:
        return self.capability.name

    @property
    def interactive(self) -> bool:
        """Whether resolution interacts with the operator (prompt).
        Inspection previews must not call ``resolve`` on interactive
        backends -- probing would BE the interaction."""
        return self.capability.interactive

    def mapping_for(self, secret: SecretDecl) -> MappingValue | None:
        """This backend's entry in the secret's ``backend_mappings``.
        ``None`` when absent (the backend's default convention applies,
        if it has one)."""
        return secret.backend_mappings.get(self.name)

    def would_attempt(self, secret: SecretDecl) -> bool:
        mapping = self.mapping_for(secret)
        if mapping is False:
            return False
        return self.capability.would_attempt(secret, mapping)

    def describe_lookup(self, secret: SecretDecl) -> str | None:
        mapping = self.mapping_for(secret)
        if mapping is False:
            return None
        return self.capability.describe_lookup(secret, mapping)

    def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
        wants: list[tuple[SecretDecl, MappingValue | None]] = [
            (s, mapping) for s in secrets if (mapping := self.mapping_for(s)) is not False
        ]
        if not wants:
            return {}
        return self.capability.batch_get(wants)


def active_backends(config: Config, registry: Registry) -> list[ActiveBackend]:
    """The active chain as runtime backends, in precedence order.

    Each layer in its natural role: the chain comes from CONFIG
    (``[secret_config].backends``, a setting), and each opted-in name is
    resolved to its ``secret-backend`` graph node, off which this reads the
    backend IMPL and its stored readiness verdict (LLD d; no
    ``SECRET_BACKEND_REGISTRY`` probe, R11). An unknown chain name gets the
    operator's vocabulary (the chain is config, so the error is a config
    error), and the hint enumerates the registered backends.

    The chain is filtered to ``present`` (a node exists) and ``enabled`` (its
    opt-in axis, LLD d): a present-but-DISABLED opted-in backend is dormant,
    excluded here exactly as an absent-from-chain backend is, so resolution
    never attempts it. This is inert today (no disabled-backend producer ships,
    R7) but is the enablement seam the plugin rebuild fills; a disabled node
    folds to a ready placeholder, so this reads ``enablement_of``, not
    ``readiness_of``. Readiness gates later, at resolution (R9.6).
    """
    from agentworks.resources.graph import Enablement

    graph = registry.graph
    backends: list[ActiveBackend] = []
    for name in config.secret_config_data.backends:
        try:
            impl = graph.impl_of("secret-backend", name)
        except KeyError:
            registered = sorted(entry.name for entry in registry.iter_kind("secret-backend"))
            raise ConfigError(
                f"[secret_config].backends names unknown backend {name!r}",
                hint=f"registered backends: {registered}",
            ) from None
        if graph.enablement_of("secret-backend", name) is Enablement.disabled:
            # A disabled opted-in backend is dormant (never consulted), the same
            # as a backend absent from the chain. Not a readiness skip, so no
            # warning: it is an opt-out, not a can't-run-here.
            continue
        # ``impl`` is never ``None`` here: a published capability row with no
        # registered impl already fails fast at ``build_graph`` (``_impl_for``
        # raises ``StateError``), so post-finalize every present backend node
        # carries its instance. The cast reflects that invariant.
        backends.append(
            ActiveBackend(
                capability=cast("SecretBackend", impl),
                readiness=graph.readiness_of("secret-backend", name),
            )
        )
    return backends


def validate_chain(config: Config, registry: Registry) -> None:
    """Secret-system reachability, run by ``build_registry`` right after
    finalize: the chain's names must be ``secret-backend`` capability
    resources, and every operator-declared secret must be reachable via the
    chain (some opted-in backend would attempt it).

    This is the reachability HALF of the old ``validate_chain``. Per-mapping
    spec validation moved into the finalize ``validate`` pass (each secret's
    own ``validate`` checks every present backend's mapping, R9.9); what stays
    here is the eager post-finalize boundary check, now GRAPH-reading (LLD d):
    a secret is reachable iff ``edges_of(secret) ∩ opted-in`` is non-empty.

    Three preservation invariants (LLD d acceptance): the scope is
    OPERATOR-DECLARED secrets only (an auto-declared row like the ever-present
    tailscale-auth-key must not invalidate a deliberate ``backends = []``
    opt-out; it surfaces at use time as ``SecretUnavailableError``), the keying
    is WOULD-ATTEMPT and READINESS-BLIND (``edges_of`` is the frozen
    would-attempt candidate set; a secret whose only opted-in backend is
    not-ready is still reachable and fails only at resolution, exactly as
    today), and the soft/hard miss halt semantics stay in ``resolve_secrets``.
    """
    from agentworks.resources.access import secret_decls

    # Validate the chain names are known backends (config vocabulary) by
    # building the active chain; the returned backends are not otherwise used
    # here (reachability reads the frozen edges, not a live would_attempt).
    active_backends(config, registry)
    opted_in = set(config.secret_config_data.backends)

    graph = registry.graph
    all_decls = secret_decls(registry)
    operator_decls = [
        decl
        for decl in all_decls.values()
        if getattr(getattr(decl, "origin", None), "variant", None) == "operator-declared"
    ]
    unreachable = [
        decl for decl in operator_decls if not ({ref.name for ref in graph.edges_of("secret", decl.name)} & opted_in)
    ]
    if unreachable:
        names = ", ".join(sorted(d.name for d in unreachable))
        chain_str = ", ".join(config.secret_config_data.backends) or "(empty)"
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


def _fail_unavailable(
    missing: list[SecretDecl],
    backends: list[ActiveBackend],
    errors: dict[str, str] | None,
) -> None:
    """Attribute every unresolved secret to the backends that DID attempt
    it, then fail per the active policy: raise the all-or-nothing
    ``SecretUnavailableError`` (command path, ``errors is None``) or record
    a per-secret failure line into ``errors`` (inspection path).

    Shared by both callers in :func:`resolve_secrets`: the end-of-loop
    fall-through and the before-interactive doom check (issue #202), so
    both build the SAME per-secret attribution and message.
    """
    sorted_missing = sorted(missing, key=lambda d: d.name)
    # Per-secret backend list: only backends that actually attempted
    # (would_attempt == True) appear, so a secret with a backend
    # opted out via backend_mappings doesn't get told it was tried.
    per_secret: dict[str, str] = {}
    for d in sorted_missing:
        attempted = [b.name for b in backends if b.would_attempt(d)]
        tried = ", ".join(attempted) if attempted else "(none; secret unreachable)"
        per_secret[d.name] = f"{d.name}: tried {tried}"
    if errors is None:
        names = [d.name for d in sorted_missing]
        # Always name a REAL secret in the example command: an
        # `<name>` placeholder isn't paste-safe (angle brackets are
        # shell redirection) and the first missing name is exactly
        # what the operator wants to inspect.
        raise SecretUnavailableError(
            f"no active backend could resolve secret(s): {', '.join(names)}",
            hint=(
                "; ".join(per_secret.values()) + f". `agw secret describe {names[0]}` shows how each "
                "backend looks a secret up (e.g. which environment "
                "variable it reads)."
            ),
        )
    for name, line in per_secret.items():
        errors[name] = f"no active backend could resolve the secret ({line})"


def resolve_secrets(
    secrets: list[SecretDecl],
    backends: list[ActiveBackend],
    *,
    errors: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve every secret through the active backends, in chain order.

    Each backend's ``resolve`` is called once with the still-missing,
    would-attempt subset; the next backend sees only what remains. Soft
    misses (a backend has no value) fall through naturally; hard
    misses (``SecretMappingError`` from a persistent-store backend)
    halt the chain so a misconfigured store doesn't quietly fall
    through to a prompt.

    ``errors`` selects the failure policy (one loop, both policies;
    same shape as the config loaders' ``issues`` out-param):

    - ``None`` (commands): all-or-nothing. Hard misses and
      control-character values raise immediately; anything still
      unresolved after every backend raises ``SecretUnavailableError``
      with a per-secret list of the backends that attempted. To spare a
      wasted prompt, that same failure is raised EARLY (issue #202),
      before an interactive backend that would actually prompt this run
      (``output.is_interactive()``), for any still-missing secret no
      remaining backend would attempt; in ``--non-interactive`` mode the
      prompt no-ops, so there is nothing to get ahead of and the
      end-of-loop raise stands.
    - a dict (inspection surfaces, e.g. ``env show --resolve``):
      partial success. Per-secret failures land in ``errors`` keyed by
      secret name, successfully-resolved values are RETURNED rather
      than discarded (prompt answers are never re-asked), and a
      backend-level exception is recorded against every secret that
      backend was attempting (batch-level attribution) without
      forwarding them to later backends, preserving the hard-miss
      "don't mask a store misconfiguration with a prompt" semantics.
    """
    resolved: dict[str, str] = {}
    deduped: list[SecretDecl] = []
    seen: set[str] = set()
    for s in secrets:
        if s.name not in seen:
            seen.add(s.name)
            deduped.append(s)

    missing = deduped
    for index, backend in enumerate(backends):
        if not missing:
            break
        # R9.6: a not-ready opted-in backend is SKIPPED WITH A WARNING (never
        # silent) and the chain falls through to the next candidate. Delta vs
        # today: a mapped-but-unavailable store (e.g. onepassword with no `op`)
        # used to raise ConnectivityError and halt; now it warns and falls
        # through. One warning per still-missing secret this backend would
        # attempt.
        if not backend.readiness.is_ready:
            for s in missing:
                if backend.would_attempt(s):
                    output.warn(f"secret {s.name}: skipping {backend.name}, not ready: {backend.readiness.reason}")
            continue
        # Fail before an interactive backend prompts (issue #202). Once
        # the non-interactive backends ahead of it have run, their soft
        # misses are known, so any still-missing secret that NO remaining
        # backend (this one and every later one) would even attempt is
        # already doomed. Raising it HERE, before the prompt fires,
        # spares the operator a prompt for one secret that a different,
        # already-unresolvable secret would abort the command over
        # anyway. The attribution and raise reuse the end-of-loop
        # implementation, so the two failure sites are identical.
        #
        # Readiness-aware (R9.6, in lockstep with the skip above): a NOT-READY
        # remaining backend will be skipped, so it does not count as attempting;
        # ``remaining`` is filtered to ready backends before the would_attempt
        # test, so the predictor never spares a prompt for a secret a skipped
        # backend "would" have taken.
        #
        # Command path only: the inspection path (errors is not None)
        # never runs interactive backends, and in --non-interactive mode
        # the prompt backend no-ops, so there is no prompt to get ahead
        # of and the end-of-loop raise stands.
        if errors is None and backend.interactive and output.is_interactive():
            remaining = [b for b in backends[index:] if b.readiness.is_ready]
            doomed = [s for s in missing if not any(b.would_attempt(s) for b in remaining)]
            if doomed:
                _fail_unavailable(doomed, backends, errors)
        attemptable = [s for s in missing if backend.would_attempt(s)]
        if not attemptable:
            continue
        try:
            got = backend.resolve(attemptable)
        except AgentworksError as exc:
            if errors is None:
                raise
            # Batch-level attribution: the backend's exception doesn't
            # say which mapping tripped it, so every secret this backend
            # was attempting is marked failed and withheld from later
            # backends (mirrors the hard-miss halt for these secrets).
            for s in attemptable:
                errors[s.name] = str(exc)
            missing = [s for s in missing if s.name not in errors]
            continue
        # Surface which backend + identifier won so operators can tell
        # env-var-from-shell apart from a fall-through to prompt. For
        # backends without a static identifier (prompt) the
        # parenthetical is omitted. Never includes the resolved value.
        decl_by_name = {s.name: s for s in attemptable}
        for name in sorted(got):
            ident = backend.describe_lookup(decl_by_name[name])
            suffix = f" ({ident})" if ident else ""
            output.info(f"Resolved {name} via {backend.name}{suffix}")
        for name, value in got.items():
            # ADR 0014: embedded newlines would corrupt SSH
            # `-o SetEnv=KEY=VALUE` arguments. The env-var backend
            # already strips trailing newlines (the common copy-paste
            # artifact); anything still containing one is a malformed
            # secret value and a hard error worth surfacing now rather
            # than as an opaque SSH-side rejection. NULs are rejected
            # for the same reason: OpenSSH's argv handling would
            # silently truncate the SetEnv arg at the first NUL.
            if "\n" in value or "\r" in value or "\0" in value:
                message = (
                    f"secret {name!r}: resolved value contains a "
                    f"control character (newline, carriage return, "
                    f"or NUL); cannot transport via SSH SetEnv. Fix "
                    f"the value at the source (e.g. strip trailing "
                    f"newlines from the env var or vault entry)."
                )
                if errors is None:
                    raise ConfigError(message)
                errors[name] = message
                continue
            resolved[name] = value
        missing = [s for s in missing if s.name not in got]

    if missing:
        _fail_unavailable(missing, backends, errors)
    return resolved


def preview_resolution(
    secret: SecretDecl,
    backends: list[ActiveBackend],
    *,
    interactive_available: bool,
) -> str | None:
    """The name of the first backend that would resolve ``secret``, or
    ``None`` if nothing in the chain would.

    Walks the chain in precedence order. ``would_attempt`` gates each
    backend; a NOT-READY backend is skipped (in lockstep with the
    resolution loop's readiness skip, R9.6/R9.7: it never resolves here, so
    the predictor never names a backend resolution will skip); an interactive
    backend (prompt) is reported without probing (probing would BE the operator
    interaction); every other ready backend must actually produce a value to be
    reported. Readiness is the offline layer UNDER interactive-optimism: a ready
    ``prompt`` is still previewed optimistically on ``would_attempt`` alone.

    ``interactive_available`` is the caller's policy for whether an
    interactive backend counts as resolving (issue #202): the preflight
    prediction passes ``output.is_interactive()`` so a prompt-only secret
    reads as unresolvable under ``--non-interactive`` / no TTY (matching
    resolve-time reality, where the prompt backend no-ops); the pure
    inspection surfaces (``describe``, ``doctor``) pass ``True`` to keep
    their optimistic, config-shape preview. When it is ``False`` the walk
    CONTINUES past the interactive backend to any later non-interactive
    one rather than stopping.

    Used by ``agw doctor`` and the describe view's resolution preview.
    """
    for backend in backends:
        if not backend.would_attempt(secret):
            continue
        if not backend.readiness.is_ready:
            # Not-ready: resolution will skip this backend with a warning
            # (R9.6), so it never resolves the secret here either. Skipping it
            # keeps the predictor honest (no "would resolve via onepassword" for
            # a backend a real run would skip) without probing an unusable tool.
            continue
        if backend.interactive:
            if interactive_available:
                return backend.name
            continue
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
