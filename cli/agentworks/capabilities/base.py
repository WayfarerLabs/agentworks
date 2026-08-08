"""The instance-scoped ``Capability`` base: the lifecycle contract every
capability implementation extends.

A capability instance moves through stages with sharply different
contracts (the full capability model is documented in
``capabilities/README.md``):

1. declaration: a capability DECLARES its config as a model
   (``config_model``) and the core does the rest, invoking no capability
   code to validate a blob or to derive the references it implies.
2. construct: cheap, config-valid by construction (the blob validates
   into the declared model, and the validated INSTANCE is what binds),
   never resolved secret values. No network, no resolution, no prompt.
3. ``preflight``: pre-resolve, read-only, best-effort readiness;
   checks unauthenticated reachability / tools (the declared secrets'
   resolvability is predicted centrally by the holding node, not by
   the instance). Doctor reuses it.
4. ``runup``: post-resolve, read-only, authenticated readiness; with
   resolved secrets in hand, does the authenticated dry-run (a git
   provider's ``GET /user``, a platform's API check), the engine
   run-up before takeoff. Default no-op.
5. ops: the mutation phase, subclass-owned. Values come from the
   context (``ctx.secret``), populated by the operation's single
   resolve pass at the preflight boundary; minting lives here (runup
   never mutates).

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
from agentworks.schema import RefOwner

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from agentworks.capabilities.retired_shapes import RetiredPresenceShape
    from agentworks.config import Config
    from agentworks.resources.graph import Readiness
    from agentworks.resources.reference import ConfigReference
    from agentworks.topics import TopicProse
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
    AGENT = "agent"  # an agent user on a VM (workspace access is a grant)
    SESSION = "session"  # a harness integration as agent-or-admin, in a workspace, on a VM


# The level-to-fields invariant, per level: (required name fields,
# forbidden name fields). ``system_slug`` is the anchor, allowed at
# every level; ``admin`` is SESSION vocabulary and is enforced
# separately (a SESSION scope requires exactly one of agent/admin;
# every other level forbids both). Every level carries rules now: a
# level's rules land with the commands that operate at it, and a level
# absent from this table (possible only if the enum grows) refuses
# construction loudly, so no scope with an unenforced invariant can
# exist. AGENT forbids ``workspace`` because agents are VM-scoped in
# the current model (a workspace relationship is a grant, never
# identity); a future workspace-rooted agent operation re-rules that
# field when it migrates.
_SCOPE_LEVEL_RULES: dict[ScopeLevel, tuple[tuple[str, ...], tuple[str, ...]]] = {
    ScopeLevel.SYSTEM: ((), ("vm", "workspace", "agent", "session")),
    ScopeLevel.VM: (("vm",), ("workspace", "agent", "session")),
    ScopeLevel.WORKSPACE: (("vm", "workspace"), ("agent", "session")),
    ScopeLevel.AGENT: (("vm", "agent"), ("workspace", "session")),
    ScopeLevel.SESSION: (("vm", "workspace", "session"), ()),
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
                f"land with the commands that operate at it."
            )
        required, forbidden = rules
        problems = [f"requires {field!r}" for field in required if getattr(self, field) is None]
        problems += [
            f"forbids {field!r} (got {getattr(self, field)!r})"
            for field in forbidden
            if getattr(self, field) is not None
        ]
        if self.level is ScopeLevel.SESSION:
            # A session runs as its agent OR as the admin, never both,
            # never neither; the flag and the name are one choice.
            if (self.agent is not None) == self.admin:
                problems.append("requires exactly one of 'agent' or 'admin'")
        elif self.admin:
            problems.append("forbids 'admin' (SESSION vocabulary: exactly one of agent/admin, at SESSION level only)")
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
    ``self`` (config bound at construct); ``runup`` and ops read the
    context.
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
    config.

    Class-level identity (``name``, ``description``) is what the
    registry's read-only capability row carries. ``owner_kind`` names
    the consuming resource kind hosting the config (``"vm-site"`` for
    VM platforms) and frames construct-time validation errors.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    """One operator-facing line: what this implementation IS.

    Also its SUMMARY on every schema-derived surface (the implementation
    list under ``agw resource describe-kind <kind>``, its own field
    reference, the guide's topic pages), which is why nothing authors a
    second one-liner beside ``prose``."""

    owner_kind: ClassVar[str]

    prose: ClassVar[TopicProse | None] = None
    """The authored paragraphs about this implementation: what it needs,
    what it assumes, what an operator should know before choosing it.

    Optional, unlike a resource kind's, and defaulted rather than declared:
    the topic contract's rule is that a participant with no useful content
    contributes nothing, and a plugin author must be able to register a
    capability without writing an essay. Field facts never go here; those
    come from ``config_model``."""

    contract_version: ClassVar[int]
    """The capability contract version this implementation is written
    against. Registration requires it to equal the version its kind's
    descriptor declares supported, so an impl on an older contract is refused
    with a version message instead of failing somewhere downstream.

    Declared, never defaulted, for the same reason ``name`` and
    ``description`` are: a default would make the version claim inherited
    rather than made, and bumping this base alongside a descriptor would then
    silently re-certify every impl that had not actually been migrated. Each
    implementation states its own, exactly as the ``SecretBackend`` Protocol
    kind's impls must."""

    config_model: ClassVar[type[BaseModel]]
    """The config this capability OFFERS, as a model.

    Declared, never defaulted, for the same reason ``contract_version`` is:
    defaulting it to an empty model would make "I accept no configuration"
    the thing an author gets by FORGETTING, which is exactly how the retired
    invoked ``validate`` behaved and is what would make an unmigrated
    implementation look migrated.

    The core reads it and does the rest: shape validation, reference
    extraction, defaulting, schema emission, and rendering all derive from
    this one declaration, and capability code is invoked for none of them.
    Registration-time conformance checks it against the kind's model
    contract (``CapabilityKindDescriptor.config_schema``)."""

    retired_shape: ClassVar[RetiredPresenceShape | None] = None
    """A config spelling this implementation used to accept, so a
    pre-migration document is refused with its exact rewrite rather than
    with a generic unknown key.

    Defaulted to ``None``, unlike the declarations above: an
    implementation with no retired spelling is the normal case, and having
    to say so would be noise on every capability that has never broken its
    config. Declared only for the length of one migration; see
    :mod:`agentworks.capabilities.retired_shapes`, which is release-scoped
    and meant to be deleted whole."""

    @classmethod
    def config_for(cls) -> type[BaseModel]:
        """The config model this capability offers.

        A capability DECLARES the config it offers the way it declares its
        API methods, and the core reads the declaration rather than asking
        the capability to do anything with it. This hook is the override
        point for a capability whose answer is not simply ``config_model``,
        and reading the config THROUGH it is what makes such a capability
        an ordinary registration rather than a framework change.

        Config is offered per FACET by contract, a facet being the LEVEL a
        capability is driven at (vm, user, workspace, session): the pairing
        of that level's API methods with that level's config. CONSUMERS
        choose the facet they drive, so a producer never has to know who is
        asking. Facets are deliberately NOT scopes and core owns the
        mapping between them: admin and agent both drive the user level,
        and session start and resume share the session level, so two
        surfaces that mean the same level get the same answer by
        construction rather than by each capability encoding the
        equivalence. Nothing under ``capabilities/`` spells a scope.

        **The parameter selecting a facet is not on this signature, because
        nothing yet offers more than one config.** Every capability shipped
        today shares a single config across all of its operations, so a
        facet parameter would be a signature every reader has to decode and
        no caller can use. It arrives, additively, with the first
        capability whose methods run at several levels (a harness
        integration is the expected one), which is the same change that
        brings the consumers that would pass it.

        Note what offering a config does NOT say: it is not a claim to
        support a level, and offering none is not a claim to lack one.
        Support is carried by the implementation. Reading a config offering
        as a support signal would rebuild the declaration-contract
        mechanism that was rescinded on 2026-08-05, under a new name.
        """
        return cls.config_model

    def __init__(self, owner_name: str, config: Mapping[str, object]) -> None:
        """Bind to ``(owner_name, config)``, validated.

        Config validity is a construct-time invariant: the blob is
        validated here, so a shape error dies at construction rather than
        later in preflight, and what is BOUND is the validated,
        fully-defaulted model instance, which is what lets an operation
        read a typed field instead of a dict key with a fallback beside
        it. Construction is otherwise cheap: no network, no secret
        resolution, no prompt, and no secret machinery at all. The
        operation's boundary union comes from the plan's declared
        ``secret_refs`` (the walk), and resolved values arrive per call
        through the context (``ctx.secret``, scoped delivery); the
        instance never holds a value source.

        What is validated is whatever :meth:`config_for` answers with, so a
        capability that overrides the hook is bound to the model it
        actually offers rather than to its ``config_model`` declaration.
        """
        from agentworks.capabilities.config import validate_own_config
        from agentworks.schema import extract_references

        self.owner_name = owner_name
        owner = RefOwner(kind=self.owner_kind, name=owner_name)
        model = type(self).config_for()
        self._config = validate_own_config(type(self), config, owner=owner)
        # Extracted from the RAW blob, exactly as the finalize pass does,
        # so an instance's declared secrets are the same set the graph
        # carries for it.
        self._secret_refs: tuple[ConfigReference, ...] = tuple(
            ref for ref in extract_references(model, config, owner) if ref.kind == "secret"
        )

    @property
    def config(self) -> BaseModel:
        """This instance's bound config: the validated, fully-defaulted
        model instance.

        Every capability narrows this to its own model type through
        :meth:`_config_as`, so an operation reads a typed field and mypy
        checks it.
        """
        return self._config

    def _config_as[M: BaseModel](self, model: type[M]) -> M:
        """This instance's bound config, as ``model``.

        The narrowing every capability's own typed ``config`` property
        goes through. It is an ``isinstance`` check rather than a
        ``cast`` because the invariant is worth ENFORCING: construction
        validated the blob against exactly the model the class declares,
        so a mismatch here is a framework bug, and a cast would let it
        surface as an ``AttributeError`` somewhere in an operation
        instead.
        """
        if not isinstance(self._config, model):
            raise StateError(
                f"{type(self).__name__} bound a {type(self._config).__name__} config "
                f"where its own declared model is {model.__name__}"
            )
        return self._config

    @property
    def _owner_display(self) -> str:
        return f"{self.owner_kind}/{self.owner_name}"

    @classmethod
    def not_ready(cls, config: Mapping[str, object]) -> Readiness:
        """Why a resource bound to ``config`` cannot run on this host, or
        ready when it can. The config-dependent half of readiness: the
        readiness fold (LLD c) calls this off the capability's graph-carried
        impl to decide a consuming resource's verdict (a local-Lima site
        without ``limactl``, keyed on the site's platform config).

        NON-CONSTRUCTING and total by contract: a classmethod that reads
        ``config`` fields best-effort, tolerates malformed ones, and NEVER
        constructs an instance or validates (construction re-runs the throwing
        ``validate``, which would make the fold non-total and turn a malformed
        block into a permanent readiness reason: the B1 loop the reshape from
        the old bound-instance ``disabled_reason`` exists to avoid). The
        config-INDEPENDENT half (a whole platform unsupported on this host)
        is :meth:`VMPlatform.unsupported_reason`, owned by the capability node,
        not this method.

        Contract: cheap, offline, host-introspection only (OS, tool presence,
        the shape of the passed config); never network, secrets, or prompting.
        Readiness that needs a resolver or a remote read is :meth:`preflight`'s
        job at the op boundary. Default: ready.
        """
        from agentworks.resources.graph import Readiness

        return Readiness.ready()

    def preflight(self, ctx: RunContext) -> None:  # noqa: B027  # intentional concrete no-op default
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
        yet. Pre-resolve concerns still read ``self`` (``self.config``).

        Resolvability prediction for the declared secret references is
        NOT the instance's job, and not the holding node's either: it is
        the OPERATION's, run centrally over the declarations by the
        preflight sweep (:func:`~agentworks.orchestration.readiness
        .preflight_all`). Whether a declared secret can be resolved is a
        property of the runtime world the operation is running in (the
        active backend chain, this run's interactivity), not of the
        resource that named it, and a resource must not assume a concern
        that is not its own. An unresolvable secret still fails the sweep
        with the same owner/usage framing, without this instance or its
        node touching the secret machinery. The visible consequence is
        doctor, which invokes ``preflight`` per row without a sweep: it
        reports resolvability once, on the secret's own row, rather than
        on every resource that names it.

        Base behavior: no-op. Subclasses extend
        (``super().preflight()``) with their world checks: required
        tools present, an unauthenticated endpoint reachable, anything
        knowable without secrets or mid-command state.
        """

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
