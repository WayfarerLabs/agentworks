"""The secrets runtime: a loop over the active backends.

Resolution is iterating the needed secrets over each active backend in
chain order -- no resolver object, no cache, no memo (ADR 0016). A
command resolves ONCE at its
composition root and passes the values down; "prompt-once" is true by
construction. Caching across CLI invocations would be a different
feature with different security properties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from agentworks import output
from agentworks.errors import AgentworksError, ConfigError, ExternalError, SecretUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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

    ``registered_name`` is the config and registry-owned key used to select the
    implementation. Resolution never asks provider code to restate its own
    identity, so diagnostics and mapping lookup cannot be influenced by a
    stateful or secret-bearing provider property.

    ``readiness`` is the verdict the fold stored on the backend's graph node
    (read by :func:`active_backends`, never recomputed, R11). Resolution gates
    on it (R9.6): a not-ready backend is skipped with a warning.
    """

    capability: SecretBackend
    readiness: Readiness
    registered_name: str

    @property
    def name(self) -> str:
        return self.registered_name

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


class ResolutionReporter(Protocol):
    """Receive value-free events from the ordered resolution loop."""

    def skipped(self, secret: str, backend: str, reason: str | None) -> None: ...

    def resolved(self, secret: str, backend: str, identifier: str | None) -> None: ...


class OutputResolutionReporter:
    def skipped(self, secret: str, backend: str, reason: str | None) -> None:
        output.warn(f"secret {secret}: skipping {backend}, not ready: {reason}")

    def resolved(self, secret: str, backend: str, identifier: str | None) -> None:
        suffix = f" ({identifier})" if identifier else ""
        output.info(f"Resolved {secret} via {backend}{suffix}")


class QuietResolutionReporter:
    def skipped(self, secret: str, backend: str, reason: str | None) -> None:
        del secret, backend, reason

    def resolved(self, secret: str, backend: str, identifier: str | None) -> None:
        del secret, backend, identifier


def active_backends(config: Config, registry: Registry) -> list[ActiveBackend]:
    """The active chain as runtime backends, in precedence order.

    Each layer in its natural role: the chain comes from CONFIG
    (``[secret_config].backends``, a setting), and each opted-in name is
    resolved to its ``secret-backend`` graph node, off which this reads the
    backend IMPL and its stored readiness verdict (LLD d; no
    ``SECRET_BACKEND_REGISTRY`` probe, R11).

    The unknown-name ``ConfigError`` is a BACKSTOP, not the primary check.
    On every registry built by ``bootstrap.build_registry`` the generic
    settings-reference pass has already refused an unknown chain name with a
    better-framed message, so this branch is unreachable there. It is kept
    because this function is public and takes any registry: a caller that
    assembles one by hand (``Registry.empty()`` + ``publish_to`` +
    ``finalize``, which the tests and multi-source orchestration do) skips
    that pass, and without this branch a typo would surface as a bare
    ``KeyError`` out of ``impl_of``. A precise config error at the layer that
    can still tell what went wrong beats a traceback, so it stays.

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
                registered_name=name,
            )
        )
    return backends


def disabled_plugin_backends(registry: Registry) -> dict[str, str]:
    """Map each disabled system-plugin ``secret-backend`` name to its plugin
    name (backend name -> plugin name).

    Read for the resolve-failure hint (LLD b): a disabled plugin backend is
    excluded from the active chain by :func:`active_backends`, so a secret whose
    only ``backend_mappings`` target is one fails resolve. This map lets
    :func:`_fail_unavailable` name the plugin to enable ("enable plugin
    `<name>`") instead of emitting the generic unreachable message, keeping
    R11.1's promise that every migration failure names the plugin.

    Reads the SAME axis and origin the doctor roster reads (``enablement_of``
    plus the ``system-plugin`` origin); it probes no impl and constructs
    nothing. Empty when no secret-backend producer is disabled, so the failure
    message is verbatim today's until a plugin backend (onepassword) is present
    but not enabled.
    """
    from agentworks.resources.graph import Enablement

    graph = registry.graph
    disabled: dict[str, str] = {}
    for name, row in registry.iter_kind_items("secret-backend"):
        origin = getattr(row, "origin", None)
        if origin is None or origin.variant != "system-plugin":
            continue
        if graph.enablement_of("secret-backend", name) is Enablement.disabled:
            disabled[name] = cast("str", origin.plugin)
    return disabled


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

    # The chain's NAMES are already settled: ``build_registry`` runs the
    # generic settings-reference check immediately before this, so every name
    # in ``backends`` resolves to a secret-backend row by the time we get
    # here. This used to call ``active_backends`` purely for that side effect
    # and throw the result away.
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
    backends: Sequence[ActiveBackend | _VerificationBackend],
    errors: dict[str, str] | None,
    registry: Registry | None = None,
) -> None:
    """Attribute every unresolved secret to the backends that DID attempt
    it, then fail per the active policy: raise the all-or-nothing
    ``SecretUnavailableError`` (command path, ``errors is None``) or record
    a per-secret failure line into ``errors`` (inspection path).

    Shared by both callers in :func:`resolve_secrets`: the end-of-loop
    fall-through and the before-interactive doom check (issue #202), so
    both build the SAME per-secret attribution and message.

    ``registry`` powers the additive, failure-path-only plugin hint (LLD b),
    centralized here so EVERY resolution path (the ``Resolver``, the env-chain
    ``resolve_for_command``, the activation gate, the batch rejoin, and the
    inspection surfaces) gets it uniformly rather than each caller threading a
    map. The disabled-plugin map is computed lazily (only on failure) via
    :func:`disabled_plugin_backends`. For a still-missing secret any of whose
    ``backend_mappings`` target a disabled plugin backend, the per-secret line
    gains an "enable plugin `<name>`" clause per distinct disabled plugin it
    maps to. A ``False`` opt-out entry is skipped (mirroring
    ``SecretDecl.dependencies`` / ``validate``): a secret that explicitly opted
    OUT of a disabled plugin backend is not told to enable it, since enabling it
    would not help. A secret mapping no disabled plugin backend is unchanged, so
    the generic message still covers the ordinary no-mapping / wrong-env-var
    cases. ``registry`` defaulted to ``None`` (no hint), so callers that pass
    only backends (tests) are unaffected.
    """
    disabled_backend_plugins = disabled_plugin_backends(registry) if registry is not None else {}
    sorted_missing = sorted(missing, key=lambda d: d.name)
    # Per-secret backend list: only backends that actually attempted
    # (would_attempt == True) appear, so a secret with a backend
    # opted out via backend_mappings doesn't get told it was tried.
    per_secret: dict[str, str] = {}
    for d in sorted_missing:
        attempted = [b.name for b in backends if b.would_attempt(d)]
        tried = ", ".join(attempted) if attempted else "(none; secret unreachable)"
        line = f"{d.name}: tried {tried}"
        # One clause per DISTINCT disabled plugin this secret's mappings target,
        # so a secret whose only mapping is a disabled plugin backend names the
        # plugin to enable rather than reading as generically unreachable. A
        # ``False`` opt-out never counts: enabling a plugin the secret opted out
        # of would not resolve it.
        named: list[str] = []
        for backend_name, mapping in d.backend_mappings.items():
            if mapping is False:
                continue
            plugin = disabled_backend_plugins.get(backend_name)
            if plugin is not None and plugin not in named:
                named.append(plugin)
        for plugin in named:
            line += f"; enable plugin `{plugin}`"
        per_secret[d.name] = line
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
    registry: Registry | None = None,
    reporter: ResolutionReporter | None = None,
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

    ``registry`` is forwarded to :func:`_fail_unavailable`, which uses it (only
    on failure) to compute the plugin-aware "enable plugin `<name>`" hint for a
    secret whose mapping targets a disabled plugin backend (LLD b). Every
    resolution path passes the registry it already holds; defaulted to ``None``
    (no hint), so callers that pass only backends (tests) are unaffected.
    """
    return _resolve_secrets_ordered(
        secrets,
        backends,
        errors=errors,
        registry=registry,
        reporter=reporter or OutputResolutionReporter(),
        interactive_available=output.is_interactive(),
    )


def resolve_secrets_quiet(
    secrets: list[SecretDecl],
    backends: list[ActiveBackend],
    *,
    registry: Registry | None = None,
    interactive_available: bool,
) -> dict[str, str]:
    """Resolve through the canonical ordered loop without progress output."""
    allowed_secret_names = frozenset(secret.name for secret in secrets)
    safe_backends = tuple(
        _VerificationBackend.wrap(
            backend,
            allowed_secret_names=allowed_secret_names,
            allow_interactive=interactive_available,
        )
        for backend in backends
    )
    return _resolve_secrets_ordered(
        secrets,
        safe_backends,
        errors=None,
        registry=registry,
        reporter=QuietResolutionReporter(),
        interactive_available=interactive_available,
        exclude_interactive=not interactive_available,
    )


def _safe_exception_attribute(exc: Exception, name: str) -> object | None:
    """Read an optional exception field without trusting subclass setup."""
    try:
        return getattr(exc, name, None)
    except Exception:
        return None


def _sanitize_verification_exception(
    exc: Exception,
    *,
    allowed_secret_names: frozenset[str] = frozenset(),
) -> AgentworksError:
    """Replace provider-controlled exception state with safe typed framing."""
    from agentworks.errors import (
        AlreadyExistsError,
        AuthorizationError,
        BackupError,
        BrokenStateError,
        ConfigError,
        ConnectivityError,
        NotFoundError,
        ProvisioningError,
        SecretMappingError,
        SecretUnavailableError,
        StateError,
        TokenRejectedError,
        ValidationError,
    )

    safe_categories: dict[type[Exception], type[AgentworksError]] = {
        TokenRejectedError: TokenRejectedError,
        NotFoundError: NotFoundError,
        AlreadyExistsError: AlreadyExistsError,
        ValidationError: ValidationError,
        StateError: StateError,
        BrokenStateError: BrokenStateError,
        AuthorizationError: AuthorizationError,
        ConnectivityError: ConnectivityError,
        SecretUnavailableError: SecretUnavailableError,
        SecretMappingError: SecretMappingError,
        ExternalError: ExternalError,
        ProvisioningError: ProvisioningError,
        BackupError: BackupError,
        ConfigError: ConfigError,
    }
    category = safe_categories.get(type(exc), ExternalError)
    if isinstance(exc, AgentworksError):
        entity_kind = _safe_exception_attribute(exc, "entity_kind")
        entity_name = _safe_exception_attribute(exc, "entity_name")
        # Exception fields are backend-authored too. Preserve only the fixed
        # kind and an exact name from the caller's already-known declaration
        # set; malformed, surprising, or secret-bearing fields are discarded.
        if entity_kind == "secret" and type(entity_name) is str and entity_name in allowed_secret_names:
            return category("secret verification failed", entity_kind="secret", entity_name=entity_name)
        if entity_kind == "secret":
            return category("secret verification failed", entity_kind="secret")
        return category("secret verification failed")
    return ExternalError("secret verification failed")


def _verification_backend_call[T](
    operation: Callable[[], T],
    *,
    allowed_secret_names: frozenset[str],
) -> T:
    """Call provider code, sanitizing only the verification boundary."""
    sanitized: AgentworksError | None = None
    try:
        return operation()
    except Exception as exc:
        sanitized = _sanitize_verification_exception(exc, allowed_secret_names=allowed_secret_names)

    # Raising outside the provider exception handler prevents the fetched
    # value and provider traceback from remaining as implicit context.
    assert sanitized is not None
    raise sanitized from None


@dataclass(frozen=True)
class _VerificationBackend:
    """Sanitized, lazy backend snapshot for named-secret verification.

    Provider-authored policy is read only when the ordered resolver reaches
    this backend. Each property is then cached so a stateful provider cannot
    change the decision between filtering and resolution.
    """

    backend: ActiveBackend
    allowed_secret_names: frozenset[str]
    name: str
    allow_interactive: bool
    _readiness: Readiness | None = field(default=None, init=False, repr=False)
    _interactive: bool | None = field(default=None, init=False, repr=False)

    @classmethod
    def wrap(
        cls,
        backend: ActiveBackend,
        *,
        allowed_secret_names: frozenset[str],
        allow_interactive: bool,
    ) -> _VerificationBackend:
        """Wrap a backend without evaluating provider-authored policy."""
        return cls(
            backend=backend,
            allowed_secret_names=allowed_secret_names,
            name=backend.registered_name,
            allow_interactive=allow_interactive,
        )

    @property
    def readiness(self) -> Readiness:
        """Return one exact-type-validated, value-safe readiness snapshot."""
        from agentworks.resources.graph import Readiness

        cached = self._readiness
        if cached is None:
            readiness = _verification_backend_call(
                lambda: self.backend.readiness,
                allowed_secret_names=self.allowed_secret_names,
            )
            if (
                type(readiness) is not Readiness
                or (readiness.reason is not None and type(readiness.reason) is not str)
                or type(readiness.is_available) is not bool
            ):
                raise ExternalError("secret verification failed")
            # Quiet verification never reports provider-authored readiness
            # prose. Copy only its decision axes so later property access is
            # guaranteed first-party and cannot expose provider state.
            safe_readiness = Readiness(
                reason=None if readiness.reason is None else "backend unavailable",
                is_available=readiness.is_available,
            )
            object.__setattr__(self, "_readiness", safe_readiness)
            cached = safe_readiness
        return cached

    @property
    def interactive(self) -> bool:
        """Return the provider policy after one sanitized, cached read."""
        cached = self._interactive
        if cached is None:
            interactive = _verification_backend_call(
                lambda: self.backend.interactive,
                allowed_secret_names=self.allowed_secret_names,
            )
            if type(interactive) is not bool:
                raise ExternalError("secret verification failed")
            object.__setattr__(self, "_interactive", interactive)
            cached = interactive
        return cached

    def would_attempt(self, secret: SecretDecl) -> bool:
        # Failure attribution revisits every backend after the ordered loop.
        # Keep a backend excluded by verification policy fully inert there too.
        if not self.allow_interactive and self.interactive:
            return False
        attempted = _verification_backend_call(
            lambda: self.backend.would_attempt(secret),
            allowed_secret_names=self.allowed_secret_names,
        )
        if type(attempted) is not bool:
            raise ExternalError("secret verification failed")
        return attempted

    def describe_lookup(self, secret: SecretDecl) -> None:
        # Quiet verification has no progress surface, so it does not invoke
        # this provider-authored diagnostic hook at all.
        del secret

    def resolve(self, secrets: list[SecretDecl]) -> dict[str, str]:
        resolved = _verification_backend_call(
            lambda: self.backend.resolve(secrets),
            allowed_secret_names=self.allowed_secret_names,
        )
        expected = {secret.name for secret in secrets}
        if (
            type(resolved) is not dict
            or any(type(name) is not str or type(value) is not str for name, value in resolved.items())
            or not set(resolved).issubset(expected)
        ):
            raise ExternalError("secret verification failed")
        return resolved


def _resolve_secrets_ordered(
    secrets: list[SecretDecl],
    backends: Sequence[ActiveBackend | _VerificationBackend],
    *,
    errors: dict[str, str] | None,
    registry: Registry | None,
    reporter: ResolutionReporter,
    interactive_available: bool,
    exclude_interactive: bool = False,
) -> dict[str, str]:
    """Shared value-resolution algorithm with caller-selected reporting."""
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
        # Named-secret verification defaults to a strict non-interactive
        # policy. Evaluate that policy only when ordered resolution reaches
        # this backend, then skip before readiness or provider execution.
        if exclude_interactive and backend.interactive:
            continue
        # R9.6: a not-ready opted-in backend is SKIPPED WITH A WARNING (never
        # silent) and the chain falls through to the next candidate. Delta vs
        # today: a mapped-but-unavailable store (e.g. onepassword with no `op`)
        # used to raise ConnectivityError and halt; now it warns and falls
        # through. One warning per still-missing secret this backend would
        # attempt.
        if not backend.readiness.is_ready:
            for s in missing:
                if backend.would_attempt(s):
                    reporter.skipped(s.name, backend.name, backend.readiness.reason)
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
        if errors is None and backend.interactive and interactive_available:
            remaining = [b for b in backends[index:] if b.readiness.is_ready]
            doomed = [s for s in missing if not any(b.would_attempt(s) for b in remaining)]
            if doomed:
                _fail_unavailable(doomed, backends, errors, registry)
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
            reporter.resolved(name, backend.name, ident)
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
        _fail_unavailable(missing, backends, errors, registry)
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
