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
4. ``runup``: post-resolve, read-only, authenticated readiness; with
   resolved secrets in hand, does the authenticated dry-run (a git
   provider's ``GET /user``, a platform's API check), the engine
   run-up before takeoff. Default no-op.
5. ops: the mutation phase, subclass-owned. Values come from the
   resolver's cache, populated by the operation's single resolve pass at
   the preflight boundary; minting lives here (runup never mutates).

Readiness is two methods split by the secret-resolve boundary: preflight
before the prompt, runup after it. That split is what keeps an
authenticated check from depending on where a secret came from.

Capability implementations extend this base; consuming resources (decls,
sessions) do not: a rich consuming resource composes the preflights of
the instances it holds through its own API.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from agentworks.errors import ConfigError, StateError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.config import Config
    from agentworks.resources.reference import ConfigReference
    from agentworks.secrets.resolver import Resolver
    from agentworks.transports import Transport


class SecretReader(Protocol):
    """Read-only access to resolved secret values, as runup/ops see them
    at the op boundary. The operation's resolver satisfies it (post
    resolve pass), as does the orchestration layer's scoped delivery
    view (``orchestration.secrets.ScopedSecrets``). Raises if a name
    was not resolved."""

    def get(self, name: str) -> str: ...


class ScopeLevel(Enum):
    """How deep an operation's identity chain reaches (the operation
    scope's key). One level per COMMAND, never per node: a batch
    command over N VMs is SYSTEM level, with each VM's identity coming
    from the nodes themselves."""

    SYSTEM = "system"  # the whole installation, no VM
    VM = "vm"  # a VM
    WORKSPACE = "workspace"  # a workspace on a VM
    AGENT = "agent"  # an agent user in a workspace on a VM
    SESSION = "session"  # a harness as agent-or-admin, in a workspace, on a VM


# The level-to-fields invariant, per constructible level: (required
# name fields, forbidden name fields). ``system_slug`` is the anchor,
# allowed at every level; ``admin`` is SESSION vocabulary and is
# enforced separately. WORKSPACE / AGENT / SESSION rules land with the
# commands that operate at those levels (orchestration-layer plan,
# phases 3-4); until then those levels are loudly non-constructible,
# so no scope with an unenforced invariant can exist.
_SCOPE_LEVEL_RULES: dict[ScopeLevel, tuple[tuple[str, ...], tuple[str, ...]]] = {
    ScopeLevel.SYSTEM: ((), ("vm", "workspace", "agent", "session")),
    ScopeLevel.VM: (("vm",), ("workspace", "agent", "session")),
}


@dataclass(frozen=True)
class OperationScope:
    """WHY an operation is running: its static identity chain, keyed by
    :class:`ScopeLevel`. Built once per command, at the orchestrator's
    entry; identical on every node's context; names only (strings).

    ``__post_init__`` ENFORCES that exactly the level's fields are set
    and the rest are absent, so a scope inconsistent with its level
    cannot be constructed. This is a promised invariant, not a
    convention.

    It is DESCRIPTIVE, not power-granting, which is why it is a plain
    ungated field on the context: a node reads the LEVEL off it (the
    skip/defer/probe/error fork) and treats the name fields as framing
    for errors and logs. A node never ADDRESSES through these names;
    acting identity is the node's own (layer 1, its ``kind/name`` and
    row-carried ancestors).
    """

    level: ScopeLevel
    system_slug: str | None = None  # the anchor; may be unset on a first-ever create
    vm: str | None = None
    workspace: str | None = None
    agent: str | None = None
    session: str | None = None
    admin: bool = False

    def __post_init__(self) -> None:
        rules = _SCOPE_LEVEL_RULES.get(self.level)
        if rules is None:
            raise StateError(
                f"OperationScope cannot be constructed at the "
                f"{self.level.value} level yet: that level's field rules "
                f"land with the commands that operate at it "
                f"(orchestration-layer SDD)."
            )
        required, forbidden = rules
        problems = [
            f"requires {field!r}"
            for field in required
            if getattr(self, field) is None
        ]
        problems += [
            f"forbids {field!r} (got {getattr(self, field)!r})"
            for field in forbidden
            if getattr(self, field) is not None
        ]
        if self.admin:
            problems.append(
                "forbids 'admin' (SESSION vocabulary: exactly one of "
                "agent/admin, at SESSION level only)"
            )
        if problems:
            raise StateError(
                f"OperationScope level-to-fields invariant violated: a "
                f"{self.level.value}-level scope "
                f"{'; '.join(problems)}."
            )


@dataclass(frozen=True, init=False)
class RunContext:
    """The resolved runtime world handed to a capability at a stage
    boundary: to ``runup`` and, as op shapes converge, to ops (and to
    ``preflight``, which gets the command-start slice of it).

    The service-layer operation assembles it for its timing, and the
    timing is the whole difference between the two readiness stages:
    ``preflight`` gets it as of command start (targets that ALREADY
    exist, and no resolved secrets yet); ``runup`` gets it as of op start
    (current targets, resolved secrets). Everything is optional and is
    present only when it exists at that timing and, in a future
    permission model, when the capability is granted it: a
    provisioning-phase runup has no on-VM targets; a `vm create`
    preflight has none either (the VM is created later, which is
    exactly what keeps preflight dependency-blind); a `session create`
    preflight against an existing VM does have an admin target.

    Two kinds of content, shaped differently on purpose:

    - The DESCRIPTIVE world is plain fields: ``config`` and
      ``operation_scope`` (why the command is running; reading it
      grants no capability, so it is ungated).
    - The POWER-GRANTING world (execution targets, resolved secrets)
      is reached through plain accessor METHODS,
      :meth:`admin_target` / :meth:`agent_target` / :meth:`secret`.
      In v1 they are pure pass-through (no requester binding, no
      grant check); the method shape exists so the node-facing
      signature is stable when a later permission model gates by the
      requesting node.

    The rule that goes with it: readiness's pre-resolve concerns read
    ``self`` (config bound at construct, ``self.resolver`` for
    prediction); ``runup`` and ops read the context.
    """

    config: Config | None
    operation_scope: OperationScope | None
    _admin_target: Transport | None
    _agent_target: Transport | None
    _secrets: SecretReader | None

    def __init__(
        self,
        *,
        config: Config | None = None,
        operation_scope: OperationScope | None = None,
        admin_target: Transport | None = None,
        agent_target: Transport | None = None,
        secrets: SecretReader | None = None,
    ) -> None:
        # Hand-written only to store the power-granting inputs under
        # private names while their public surface is the accessor
        # methods below (a generated __init__ would force callers to
        # spell the private names). Frozen dataclass, so assignment
        # goes through object.__setattr__. One consequence: never use
        # dataclasses.replace() on a RunContext (it would hand the
        # PRIVATE field names back to this __init__ and fail
        # confusingly); construct a fresh context instead, which is the
        # per-stage re-assembly rule anyway.
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "operation_scope", operation_scope)
        object.__setattr__(self, "_admin_target", admin_target)
        object.__setattr__(self, "_agent_target", agent_target)
        object.__setattr__(self, "_secrets", secrets)

    def admin_target(self) -> Transport | None:
        """The admin execution target, when one exists at this stage's
        timing. Plain pass-through in v1."""
        return self._admin_target

    def agent_target(self) -> Transport | None:
        """The agent execution target, when one exists at this stage's
        timing. Plain pass-through in v1."""
        return self._agent_target

    def secret(self, name: str) -> str:
        """A resolved secret value, from the operation's boundary
        resolve pass (delivery may be scoped to the reader's declared
        names). Raises :class:`~agentworks.errors.ConfigError` when the
        context carries no resolved secrets at all: post-resolve code
        reached with a pre-boundary (or inspection-only) context. That
        error names the secret, not the requesting capability (the old
        per-capability guards carried owner framing); requester framing
        returns when a later permission model binds the requester to
        the context."""
        if self._secrets is None:
            raise ConfigError(
                f"secret {name!r} requested from a run context with no "
                f"resolved secrets (assembled before the resolve "
                f"boundary, or for inspection only?)"
            )
        return self._secrets.get(name)


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

    def preflight(self, ctx: RunContext) -> None:
        """Verify readiness: "will the real work probably succeed?"

        Read-only and side-effect-free; that property is load-bearing:
        it is what lets doctor reuse this for per-resource health rows
        and what makes it safely re-runnable. Best-effort, not an
        oracle: anything only confirmable by mutating is the op's job.

        Preflight is forced early: it precedes the single resolve pass,
        which runs once at command start, so it runs for every resource
        before anything is touched. That makes it DEPENDENCY-BLIND: assume
        only what is true at command entry; never check state a later step
        in the same command creates. (Antipattern: a git-credential
        preflight failing ``vm create`` because git is not installed, the
        admin user is absent, or the VM does not exist yet, all created
        later in that command. Those checks belong in runup, which is
        deferred to the op boundary.)

        ``ctx`` is the command-start world (:class:`RunContext`): it holds
        targets that ALREADY exist (a `session create` sees the existing
        VM's ``admin_target``; a `vm create` sees none, which is what
        structurally enforces the blindness above) but NO resolved secrets
        yet. Pre-resolve concerns still read ``self``: ``self.config``
        and ``self.resolver`` in its prediction role.

        Base behavior: every secret reference the bound config declares
        must be predicted resolvable by some active backend, without
        prompting (an unresolvable secret is fatal and knowable here; a
        prompt-only secret's value check defers past preflight).
        Subclasses extend (``super().preflight()``) with their world
        checks: required tools present, an unauthenticated endpoint
        reachable, anything knowable without secrets or mid-command
        state.
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

    def runup(self, ctx: RunContext) -> None:  # noqa: B027  # intentional concrete no-op default
        """Authenticated readiness: with secrets in hand, does the real
        work look like it will succeed? The engine run-up before takeoff.

        Preflight's post-resolve twin (preflight is the walk-around; this
        is the run-up at the hold-short line, right before the op). It
        reads resolved secret values from the context (``ctx.secret(name)``,
        the op-start :class:`RunContext`) and does the authenticated reads
        preflight cannot: a git provider's ``GET /user``, a platform's
        API connection check. It may also use the context's execution
        targets (``ctx.admin_target()`` / ``ctx.agent_target()``) that an
        earlier phase created. Read-only and side-effect-free exactly like
        :meth:`preflight` (it never mints, creates, or mutates), which is
        what lets it be re-run and, via a future ``doctor --runup``,
        called outside an operation.

        Unlike preflight, runup is NOT forced to the front of the command:
        it is deferred to right before the ops it gates. The secrets it
        needs were resolved once up front (cached), but it fires at the op
        boundary, so it may test anything, including dependencies an
        earlier phase of the same command has since put in place (the VM
        exists, git is installed). Hoisting it forward would only cripple
        it to preflight's dependency-blindness for no gain.

        And what a runup failure MEANS is the caller's call, not runup's:
        this method just raises on definitive rejection. The service-layer
        operation decides, by whether the failed resource is idempotently
        retryable: retryable -> skip it with clear messaging and continue
        (degrade to partial; a retry recovers it; vm/agent provisioning
        skips a rejected credential and reinit fixes it); ultimately fatal
        -> stop and best-effort roll back any mutations already made,
        rather than leave a stranded half-state.

        The split across the resolve boundary is what dissolves
        source-asymmetry: by the time runup runs, EVERY declared secret
        is resolved (env-var, prompted, 1Password alike), so an
        authenticated check treats them all identically. Preflight
        predicts before the prompt (may I even bother resolving?); runup
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
        authenticate, and a no-op runup is a legitimate answer, not an
        unfinished one. Subclasses with a credential or reachable API
        override wholesale (no ``super().runup()`` to call).
        """
