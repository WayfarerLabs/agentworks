"""The instance-scoped ``Capability`` base: the lifecycle contract every
capability implementation extends.

A capability instance moves through stages with sharply different
contracts (the full capability model is documented in
``capabilities/README.md``):

1. ``validate_config``: pure classmethod; validates a config blob's
   shape and returns the resource references it implies.
2. construct: cheap, config-valid by construction (re-runs
   ``validate_config``); binds ``(name, config, resolver)``, never
   resolved secret values. No network, no resolution, no prompt.
3. ``preflight``: pre-resolve, read-only, best-effort readiness;
   predicts secret resolvability without prompting, checks unauthenticated
   reachability / tools. Doctor reuses it.
4. ``verify``: post-resolve, read-only, authenticated readiness; with
   resolved secrets in hand, does the authenticated dry-run (a git
   provider's ``GET /user``, a platform's API check). Default no-op.
5. ops: the mutation phase, subclass-owned. Values come from the
   resolver's cache, populated by the operation's single resolve pass at
   the preflight boundary; minting lives here (verify never mutates).

Readiness is two methods split by the secret-resolve boundary: preflight
before the prompt, verify after it. That split is what keeps an
authenticated check from depending on where a secret came from.

Capability implementations extend this base; consuming resources (decls,
sessions) do not: a rich consuming resource composes the preflights of
the instances it holds through its own API.
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING, Any, ClassVar

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.resources.reference import ConfigReference
    from agentworks.secrets.resolver import Resolver


def idempotent_op[F: "Callable[..., Any]"](fn: F) -> F:
    """Mark an op as required-idempotent on the kind ABC: run twice, it
    lands in the same place as run once (``reinit`` re-applies
    everything, and failed commands are retried).

    Most provisioning ops satisfy this for free (wholesale writes); the
    marker earns its keep where idempotency stops being free: a
    minting op must check-then-mint. Implementations of a flagged op
    must conform; :func:`is_idempotent_op` reads the flag through
    overrides.
    """
    fn.__idempotent_op__ = True  # type: ignore[attr-defined]
    return fn


def is_idempotent_op(cls: type, op_name: str) -> bool:
    """Whether ``op_name`` is flagged idempotent anywhere in ``cls``'s
    MRO (the flag sits on the kind ABC's declaration; subclass overrides
    inherit the contract without restating the marker)."""
    return any(
        getattr(base.__dict__.get(op_name), "__idempotent_op__", False)
        for base in cls.__mro__
        if op_name in base.__dict__
    )


class Capability(ABC):
    """A capability implementation bound to one consuming resource's
    config and the operation's resolver.

    Class-level identity (``name``, ``description``) is what the
    registry's read-only capability row carries. ``owner_kind`` names
    the consuming resource kind hosting the config (``"vm-site"`` for
    VM platforms) and frames construct-time validation errors.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    owner_kind: ClassVar[str]

    def __init__(
        self,
        owner_name: str,
        config: Mapping[str, object],
        resolver: Resolver | None = None,
    ) -> None:
        """Bind to ``(owner_name, config, resolver)``.

        Config validity is a construct-time invariant: ``validate_config``
        re-runs here, so a shape error dies at construction, never later
        in preflight. Construction is otherwise cheap: no network, no
        secret resolution, no prompt. The declared secret references
        register on the resolver, so the operation's boundary resolve
        covers the union across every instance constructed against it.

        ``resolver`` is optional only for direct construction in tests
        and inspection surfaces; the composition roots always pass one,
        and ops that need a value fail with a typed error without it.
        """
        self.owner_name = owner_name
        self.config = config
        self.resolver = resolver
        self._secret_refs: tuple[ConfigReference, ...] = tuple(
            ref
            for ref in type(self).validate_config(self._owner_display, config)
            if ref.kind == "secret"
        )
        if resolver is not None:
            for ref in self._secret_refs:
                resolver.register_name(ref.name)

    @property
    def _owner_display(self) -> str:
        return f"{self.owner_kind}/{self.owner_name}"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        """Validate ``config`` (the blob owned by ``owner``) and return
        the resource references it implies.

        Invoked at each source's blob boundary (manifest decode with
        ``file:line`` framing; legacy TOML loaders), by the consuming
        resource's ``referenced_resources()`` at finalize, and again at
        construct; MUST be pure. ``owner`` is display context for error
        messages: host-agnostic, never dispatched on.

        Base behavior: accepts no configuration. Subclasses with config
        override wholesale.

        NOTE: this invoked-validation API may be deprecated in favor of
        capabilities pushing a declarative config schema definition at
        registration time (fields typed as resource references to
        specific kinds, with usage information), letting the core
        engine validate and derive references without invoking the
        capability.
        """
        if config:
            display = getattr(cls, "name", cls.__name__)
            raise ConfigError(
                f"{owner}: the {display} capability accepts no "
                f"configuration; got {sorted(config)}"
            )
        return ()

    def disabled_reason(self) -> str | None:
        """Why this bound instance cannot run on this host, or ``None``
        when it can. The generic "do you have what you need to run"
        surface: the resource layer exposes it as a binary disabled
        flag plus reason, so a disabled resource still registers (it
        lists, describes, and holds references) but any attempt to use
        it is a typed error and existing references degrade to
        warnings.

        Contract: cheap, offline, host-introspection only (OS, tool
        presence, the shape of the bound config); never network,
        secrets, or prompting. Readiness that needs a resolver or a
        remote read is :meth:`preflight`'s job at the op boundary; this
        runs on inspection surfaces (doctor, ``resource list``,
        selection) where preflight would be too heavy. Default: never
        disabled.
        """
        return None

    def preflight(self) -> None:
        """Verify readiness: "will the real work probably succeed?"

        Read-only and side-effect-free; that property is load-bearing:
        it is what lets doctor reuse this for per-resource health rows
        and what makes it safely re-runnable. Best-effort, not an
        oracle: anything only confirmable by mutating is the op's job.

        Base behavior: every secret reference the bound config declares
        must be predicted resolvable by some active backend, without
        prompting (an unresolvable secret is fatal and knowable here; a
        prompt-only secret's value check defers past preflight).
        Subclasses extend (``super().preflight()``) with their world
        checks: required tools present, an API reachable (a read).
        """
        if not self._secret_refs:
            return
        if self.resolver is None:
            raise ConfigError(
                f"{self._owner_display}: cannot preflight declared "
                f"secrets without a resolver (constructed for inspection?)"
            )
        for ref in self._secret_refs:
            decl = self.resolver.register_name(ref.name)
            if self.resolver.predict(decl) is None:
                raise ConfigError(
                    f"{self._owner_display}: secret '{ref.name}' "
                    f"({ref.usage}) is not resolvable by any active "
                    f"backend",
                    hint=(
                        f"`agw secret describe {ref.name}` shows how each "
                        "backend looks the secret up; add a backend mapping "
                        "or extend [secret_config].backends."
                    ),
                )

    def verify(self) -> None:  # noqa: B027  # intentional concrete no-op default
        """Authenticated readiness: with secrets in hand, does the real
        work look like it will succeed?

        Preflight's post-resolve twin. It runs AFTER the operation's
        single resolve pass, so it MAY read resolved secret values from
        the resolver's cache (``self.resolver.get(name)``) and do the
        authenticated reads preflight cannot: a git provider's
        ``GET /user``, a platform's API connection check. Read-only and
        side-effect-free exactly like :meth:`preflight` (it never mints,
        creates, or mutates), which is what lets it be re-run and, via a
        future ``doctor --verify``, called outside an operation.

        The split across the resolve boundary is what dissolves
        source-asymmetry: by the time verify runs, EVERY declared secret
        is resolved (env-var, prompted, 1Password alike), so an
        authenticated check treats them all identically. Preflight
        predicts before the prompt (may I even bother resolving?); verify
        confirms after it (may I start mutating?).

        The point is to catch errors cleanly before any op mutates: to
        avoid unnecessary mutations, and to spare the operator
        hard-to-diagnose failures partway through the real work. What you
        check toward that is your call, same as preflight.

        Best-effort, not an oracle: it catches what an authenticated read
        can catch and raises a typed error on definitive rejection;
        anything only a mutation can confirm is the op's job, and network
        indeterminacy warns (never raises), since a transient outage must
        not block work an unverified-but-valid token would have done.

        Base behavior: no-op. Many capabilities have nothing to
        authenticate, and a no-op verify is a legitimate answer, not an
        unfinished one. Subclasses with a credential or reachable API
        override wholesale (no ``super().verify()`` to call).
        """
