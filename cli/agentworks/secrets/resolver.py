"""The per-operation secret resolver: ORCHESTRATOR-OWNED boundary
machinery (no capability instance ever holds one).

One ``Resolver`` per service-layer operation, living at the
composition root. The orchestrator registers the plan's secret union
on it (the walk's declared ``secret_refs``, plus any env-chain
targets), runs ONE :meth:`resolve` pass at the preflight boundary (as
soon as every participating node's preflight passes), one prompt
session, and then delivers values downstream through scoped readers
(``orchestration.secrets.ScopedSecrets`` over :meth:`values`); the
activation gate's just-in-time values enter through :meth:`seed`.

This does not change the no-cross-invocation-cache stance (ADR 0016):
the cache lives and dies with the operation, exactly like the resolved
mapping the composition roots used to thread down as a dict; the
resolver just reifies "resolve once per command and hand the values
down" into one object per operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from agentworks.config import Config
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl
    from agentworks.secrets.orchestration import SecretTarget
    from agentworks.secrets.policy import InteractionPolicy


class Resolver:
    """Accumulate an operation's secret declarations; resolve once,
    then serve cached values.

    The two verbs map onto the operation's lifecycle:

    - :meth:`resolve` (the preflight boundary): one batched pass over
      the active sources for every registered declaration: one
      prompt session. Idempotent when nothing new was registered.
      (Resolvability PREDICTION is not this object's job: it is
      central, over declarations, via
      ``orchestration.secrets.predict_resolution``.)
    - :meth:`get` (ops): a cached value. Raises a typed error if the
      boundary resolve has not run; an op must never trigger
      resolution (a prompt mid-op is exactly what the boundary
      ordering exists to prevent).
    """

    def __init__(
        self,
        config: Config,
        registry: Registry,
        *,
        interaction: InteractionPolicy,
    ) -> None:
        from agentworks.secrets.policy import require_exact_interaction_policy

        require_exact_interaction_policy(interaction)
        self._config = config
        self._registry = registry
        self._interaction = interaction
        self._decls: dict[str, SecretDecl] = {}
        self._targets: list[SecretTarget] = []
        self._seeded: dict[str, str] = {}
        self._values: dict[str, str] | None = None

    # -- registration --------------------------------------------------

    def register(self, decls: Iterable[SecretDecl]) -> None:
        """Add declarations to the operation's resolve set (first
        registration of a name wins; re-registration is a no-op)."""
        for decl in decls:
            self._decls.setdefault(decl.name, decl)

    def register_targets(self, targets: Sequence[SecretTarget]) -> None:
        """Register every secret referenced by the targets' env chains.

        This is how the runtime env system joins the operation's one
        resolve pass: each target's merged per-scope env (the scope
        precedence ladder) is walked for secret references via
        ``compute_needed_secrets``, and the resulting declarations join
        the set, so a command's site secrets, provisioning secrets,
        and workload env secrets all land in the same prompt session.
        """
        from agentworks.secrets.orchestration import compute_needed_secrets

        retained = list(targets)
        self._targets.extend(retained)
        self.register(compute_needed_secrets(retained, self._registry))

    def register_name(self, name: str) -> SecretDecl:
        """Register a secret by name and return its declaration.

        Looks the name up against the registry's ``secret`` rows and
        falls back to synthesizing a bare declaration when absent: an
        operator who has no manifest referencing a well-known secret name
        leaves the registry without that ``secret`` row, and the source
        chain must stay callable for well-known names (the same fallback
        the pre-resolver ``_collect_secrets`` used).
        """
        existing = self._decls.get(name)
        if existing is not None:
            return existing
        decl = self._decl_for(name)
        self._decls[name] = decl
        return decl

    def _decl_for(self, name: str) -> SecretDecl:
        from agentworks.secrets.base import SecretDecl
        from agentworks.secrets.kinds import SECRET_KIND_NAME

        try:
            found: SecretDecl = self._registry.lookup(SECRET_KIND_NAME, name)
        except KeyError:
            return SecretDecl(name=name, description="")
        return found

    def seed(self, values: Mapping[str, str]) -> None:
        """Pre-seed the boundary pass with values the ACTIVATION GATE
        already resolved (the one sanctioned resolution outside the
        boundary pass; see ``orchestration/activation.py``).

        The point is the NO-DOUBLE-RESOLVE property: seeded names
        register on the operation's resolve set and are excluded from
        the boundary pass's source loop, so a gate-resolved secret
        never resolves or prompts twice in one command. (Ops read the
        gate's scoped reader while the gate runs, and scoped delivery
        over the boundary cache after it; seeded values also stay
        readable via :meth:`get` before the pass, part of the same
        contract.)

        Seeding after the boundary pass is the same contract violation
        as registering after it (a value the pass never covered), so
        it raises instead of quietly widening the cache.
        """
        if self._values is not None:
            raise StateError(
                "secret values were seeded after the operation's resolve "
                f"pass: {', '.join(sorted(values))}. The activation gate "
                "resolves and seeds before the boundary; reaching here "
                "means a caller seeded too late."
            )
        for name, value in values.items():
            self.register_name(name)
            self._seeded[name] = value

    # -- the lifecycle verbs -------------------------------------------

    def resolve(self) -> None:
        """THE operation's one resolve pass, run at the preflight
        boundary (after every participating resource's preflight
        passes, before any op). One prompt session for everything
        registered; values cached for :meth:`get`.

        Idempotent while the registered set is unchanged. Registering
        more declarations after the pass and resolving again is a
        contract violation (it would mean a second prompt session), so
        it raises instead of quietly re-prompting.
        """
        if self._values is not None:
            unresolved = [n for n in self._decls if n not in self._values]
            if unresolved:
                raise StateError(
                    "secret declarations were registered after the "
                    f"operation's resolve pass: {', '.join(sorted(unresolved))}. "
                    "Register every participating resource's secrets before "
                    "the preflight-boundary resolve."
                )
            from agentworks.secrets.orchestration import validate_target_values

            validate_target_values(self._targets, self._values)
            return
        from agentworks.secrets.policy import InteractionPolicy
        from agentworks.secrets.resolve import (
            CompletionPolicy,
            OutputInteractionBroker,
            ResolutionPolicy,
            active_sources,
            resolve_batch,
        )

        # Gate-seeded values are already resolved (by the gate's own
        # source-chain pass); the boundary loop covers only the rest.
        missing = [decl for name, decl in self._decls.items() if name not in self._seeded]
        if not missing:
            resolved = dict(self._seeded)
        else:
            broker = OutputInteractionBroker(missing) if self._interaction is InteractionPolicy.ALLOW else None
            batch = resolve_batch(
                missing,
                active_sources(self._config, self._registry),
                policy=ResolutionPolicy(interaction=self._interaction, completion=CompletionPolicy.COMPLETE),
                interaction_broker=broker,
            )
            resolved = {**self._seeded, **batch.complete_or_raise()}
        from agentworks.secrets.orchestration import validate_target_values

        validate_target_values(self._targets, resolved)
        self._values = resolved

    def resolve_gate(self, name: str) -> str:
        """Resolve and seed one declaration before the operation boundary."""
        if self._values is not None:
            raise StateError("a gate secret cannot resolve after the operation boundary")
        decl = self.register_name(name)
        existing = self._seeded.get(name)
        if existing is not None:
            return existing
        from agentworks.secrets.policy import InteractionPolicy
        from agentworks.secrets.resolve import (
            CompletionPolicy,
            OutputInteractionBroker,
            ResolutionPolicy,
            active_sources,
            resolve_batch,
        )

        broker = OutputInteractionBroker([decl]) if self._interaction is InteractionPolicy.ALLOW else None
        batch = resolve_batch(
            [decl],
            active_sources(self._config, self._registry),
            policy=ResolutionPolicy(interaction=self._interaction, completion=CompletionPolicy.COMPLETE),
            interaction_broker=broker,
        )
        projected = batch.complete_or_raise()
        if tuple(projected) != (name,):
            raise StateError("gate secret resolution returned an invalid singleton result")
        self._seeded[name] = projected[name]
        return self._seeded[name]

    def resolve_late_repair(self, decl: SecretDecl) -> str:
        """Resolve one authorized repair secret without widening the cache."""
        if self._values is None:
            raise StateError("a late repair secret requires the operation boundary")
        from agentworks.secrets.policy import InteractionPolicy
        from agentworks.secrets.resolve import (
            CompletionPolicy,
            OutputInteractionBroker,
            ResolutionPolicy,
            active_sources,
            resolve_batch,
        )

        broker = OutputInteractionBroker([decl]) if self._interaction is InteractionPolicy.ALLOW else None
        batch = resolve_batch(
            [decl],
            active_sources(self._config, self._registry),
            policy=ResolutionPolicy(interaction=self._interaction, completion=CompletionPolicy.COMPLETE),
            interaction_broker=broker,
        )
        projected = batch.complete_or_raise()
        if tuple(projected) != (decl.name,):
            raise StateError("late repair resolution returned an invalid singleton result")
        return projected[decl.name]

    def get(self, name: str) -> str:
        """A resolved value, from the boundary pass's cache (or, before
        the pass, from the activation gate's seed; see :meth:`seed`)."""
        if self._values is None:
            seeded = self._seeded.get(name)
            if seeded is not None:
                return seeded
            raise StateError(
                f"secret '{name}' requested before the operation's resolve "
                "pass ran. The composition root resolves once at the "
                "preflight boundary; reaching here means a caller skipped "
                "that step."
            )
        try:
            return self._values[name]
        except KeyError:
            raise StateError(
                f"secret '{name}' was not part of the operation's resolve "
                "pass. Register it (and re-run preflight) before the "
                "boundary resolve."
            ) from None

    @property
    def resolved(self) -> bool:
        """Whether the boundary pass has run."""
        return self._values is not None

    @property
    def values(self) -> dict[str, str]:
        """The boundary pass's full resolved mapping, for consumers that
        take the whole set (``compose_env``'s ``values``). Same
        must-have-resolved contract as :meth:`get`."""
        if self._values is None:
            raise StateError(
                "resolved secret values requested before the operation's "
                "resolve pass ran. The composition root resolves once at "
                "the preflight boundary; reaching here means a caller "
                "skipped that step."
            )
        return dict(self._values)
