"""The activation gate: power-state convergence plus the held-active
span.

Commands that touch an EXISTING VM converge its power state first, so
every readiness probe that reaches the target queries a live
environment. The gate OPENS after build and BEFORE the preflight
sweep, and it is a SPAN, not a point: it stays open through the whole
command (WSL2 must be HELD active, not merely started) and closes at
the end, on success or failure, after any unwind.

It is not a protocol stage and not a preflight side effect (preflight
is read-only): the gate is the orchestrator driving the live VM node's
own power-state ops. Power state is VM-node vocabulary, so nothing
about it touches the thin ``Node`` surface; :class:`GateTarget` is the
narrow structural slice of that vocabulary this helper drives, exactly
the power-state ops the now-retired imperative
``vms.manager.ensure_active`` / ``keep_active`` pair once carried (the
migration's parity oracle for this gate).

Three properties are load-bearing:

- **Maintenance, not plan mutation.** Convergence is idempotent
  declared-state maintenance: never rollback-tracked (a VM
  auto-started for a command that later fails stays up; the span just
  closes), and it does not bend "preflight-all before any mutation",
  which governs the command's PLAN.
- **The node is the authority on auto-start.** Auto-start applies only
  to an auto-stopped VM; a manually stopped one (``operator_stopped``)
  refuses with a typed error raised within the node's own scope
  (:meth:`GateTarget.auto_start`), including the same re-read-the-flag
  race guard the retired imperative ``ensure_active`` carried.
- **Gate secrets resolve JUST-IN-TIME, outside the boundary pass.**
  Observing and starting a stopped VM may need the platform's API
  credential (the common case) or the Tailscale auth key (the rejoin
  repair case). This is the one sanctioned resolution outside the
  boundary pass: narrow, known names, resolved through the normal
  backend chain, entirely pre-walk-away, and skipped altogether on the
  fast path (a confirmed-active VM costs no resolution and no
  interaction). The two cases resolve at DIFFERENT moments, matching
  HEAD (``vms.manager._ensure_tailscale``'s documented
  conditional-need exception): the observe/start credentials
  (:meth:`GateTarget.gate_secret_refs`) resolve eagerly once the fast
  path fails, while the repair secrets
  (:meth:`GateTarget.repair_secret_refs`) resolve LAZILY, on first
  read by the repair path, because whether a rejoin (and therefore a
  new key) is needed is only knowable after starting the VM and
  watching it fail to reconnect; resolving eagerly would prompt every
  start for a key that is almost never used. Either way the node only
  reads from a reader the gate handed it (declare/receive holds) and
  resolution stays orchestrator-owned. Everything resolved, eager or
  lazy, lands in the values the gate returns, so the orchestrator can
  SEED the boundary pass and no secret resolves or prompts twice in
  one command (``Resolver.seed`` is that path: the orchestrator's
  resolve callback seeds the boundary resolver as it resolves).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

from agentworks.errors import StateError
from agentworks.orchestration.secrets import ScopedSecrets

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.capabilities.base import SecretReader
    from agentworks.config import Config
    from agentworks.resources.registry import Registry
    from agentworks.secrets.resolver import Resolver


class GateTarget(Protocol):
    """The power-state surface the gate drives: the live VM node's own
    vocabulary, sliced structurally so the helper stays domain-blind.

    The live VM node is the implementation (it composes its held
    platform instance's ops); test doubles satisfy it directly.
    """

    def gate_secret_refs(self) -> tuple[str, ...]:
        """The secret names this target's observe/start ops need (the
        platform API credential). Resolved EAGERLY, once, when the
        fast path cannot confirm the target active."""
        ...

    def repair_secret_refs(self) -> tuple[str, ...]:
        """The secret names only the post-start repair path needs (the
        Tailscale rejoin auth key). Everything about them is LAZY: the
        gate consults this method, and resolves a name, only when the
        repair path actually reads one inside ``auto_start``, never up
        front. Whether repair is needed is only knowable after the
        start, and today's oracle (``vms.manager._ensure_tailscale``)
        deliberately does not prompt every start for a key that is
        almost never used; the same laziness lets an implementation
        derive the names from state (the VM's template) that the
        healthy path never has to touch."""
        ...

    def confirmed_active(self) -> bool:
        """Cheap, secret-free fast path: ``True`` only when the target
        is positively known active (today's Tailscale reachability
        probe, skipped when the row already says manually stopped).
        ``False`` means unknown: ask the backend."""
        ...

    def observed_stopped(self, gate_secrets: SecretReader) -> bool:
        """Authenticated backend observation: ``True`` only on a
        definitive stopped/deallocated observation. Running or
        indeterminate is ``False``: a transient status failure must
        not trigger a spurious start (the real op surfaces the real
        error)."""
        ...

    def auto_start(self, gate_secrets: SecretReader) -> None:
        """Start an auto-stopped target, including any post-start
        reachability repair (the Tailscale rejoin path, with its
        reusable-key messaging; its secrets arrive through
        ``gate_secrets`` and resolve on first read). The node re-reads
        its operator-stopped intent here and REFUSES a manually
        stopped target with a typed error and the explicit-start hint:
        the node, not the helper, is the authority."""
        ...

    def hold_active(self) -> contextlib.AbstractContextManager[None]:
        """Hold the target against the backend's idle-shutdown
        mechanism (the ``vm_active`` span); a no-op context for
        platforms with nothing to hold."""
        ...


class _GateSecrets:
    """The reader ``auto_start`` receives: eager gate values served
    from the gate's seed mapping, repair names resolved lazily on
    first read. Lazily-resolved values are recorded into the SAME
    mapping :func:`ensure_active` returns, so they reach the boundary
    seed and nothing resolves or prompts twice. Even the repair-name
    DECLARATION is consulted lazily (on a read the eager set does not
    cover), so an untaken repair path costs neither a resolve nor the
    state lookup the declaration may need.

    Satisfies ``SecretReader``. Anything outside the declared gate and
    repair names is refused, same contract as
    :class:`~agentworks.orchestration.secrets.ScopedSecrets`.
    """

    def __init__(
        self,
        values: dict[str, str],
        repair_names: Callable[[], tuple[str, ...]],
        resolve_secret: Callable[[str], str],
    ) -> None:
        self._values = values
        self._repair_names = repair_names
        self._resolve_secret = resolve_secret

    def get(self, name: str) -> str:
        existing = self._values.get(name)
        if existing is not None:
            return existing
        if name in self._repair_names():
            value = self._resolve_secret(name)
            self._values[name] = value
            return value
        raise StateError(
            f"secret {name!r} was not declared as a gate or repair "
            f"secret by this activation target, so it is not delivered "
            f"to it (the declare/receive contract); declare it in "
            f"gate_secret_refs or repair_secret_refs."
        )


def ensure_active(
    target: GateTarget, resolve_secret: Callable[[str], str]
) -> dict[str, str]:
    """Converge ``target``'s power state: the gate's point half.

    Fast path first (no secret touched); otherwise resolve the
    target's gate secrets just-in-time and drive observe-then-start,
    with the operator-stopped refusal raised from the node's own
    ``auto_start`` and the repair secrets resolving lazily only if the
    repair path reads them. Returns every gate-resolved value, eager
    and lazy alike (empty on the fast path), for the orchestrator to
    seed the boundary pass with.
    """
    if target.confirmed_active():
        return {}
    values = {name: resolve_secret(name) for name in target.gate_secret_refs()}
    if target.observed_stopped(ScopedSecrets(values, values.keys())):
        target.auto_start(
            _GateSecrets(values, target.repair_secret_refs, resolve_secret)
        )
    return values


def gate_secret_resolver(
    config: Config, registry: Registry, resolver: Resolver
) -> Callable[[str], str]:
    """The gate's just-in-time resolve callback, shared by every
    command whose gate opens BEFORE its boundary resolve: resolve
    through the normal backend chain and SEED the boundary resolver as
    each value lands (``Resolver.seed``), so the boundary pass skips
    the gate-resolved names and no secret resolves or prompts twice in
    one command. (The ops themselves read the gate's scoped reader,
    not the resolver: seeding is purely the no-double-resolve
    property.)"""

    def resolve_gate_secret(secret_name: str) -> str:
        from agentworks.orchestration.secrets import secret_declarations
        from agentworks.secrets.resolve import active_backends, resolve_secrets

        (decl,) = secret_declarations([secret_name], registry)
        value = resolve_secrets([decl], active_backends(config, registry))[
            secret_name
        ]
        resolver.seed({secret_name: value})
        return value

    return resolve_gate_secret


@contextlib.contextmanager
def activation_gate(
    target: GateTarget, resolve_secret: Callable[[str], str]
) -> Iterator[dict[str, str]]:
    """The gate as the orchestrator opens it: :func:`ensure_active`,
    then the held-active span for the body's duration.

    Yields the gate-resolved secret values (see :func:`ensure_active`).
    The span closes on both success and failure; the orchestrator runs
    any unwind INSIDE the gate, so teardown ops still reach a held
    target.
    """
    values = ensure_active(target, resolve_secret)
    with target.hold_active():
        yield values
