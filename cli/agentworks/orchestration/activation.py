"""The activation gate: power-state convergence plus the held-active
span.

Ordinary singular commands that touch an EXISTING VM converge its power
state before their preflight and boundary resolve, so every readiness
probe that reaches the target queries a live environment. Their gate
OPENS after build and BEFORE the preflight sweep. Batch session status is
the deliberate inverse: it completes one shared preflight and boundary
resolve before opening each VM gate, then serves ordinary gate credentials
from that cache and resolves only a conditionally-needed repair key late.
In either ordering the gate is a SPAN, not a point: it stays open through
the command (WSL2 must be HELD active, not merely started) and closes at
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
- **Gate secrets are delivered just-in-time, with caller ordering explicit.**
  Observing and starting a stopped VM may need the platform's API credential
  (the common case) or the Tailscale auth key (the rejoin repair case). For an
  ordinary singular command, the gate opens before preflight and boundary
  resolution. Its narrow, known names resolve through the normal source
  chain only after the secret-free fast path fails. Resolving there is
  INHERENT rather than a resolve/preflight-ordering gap (issue #202):
  on-target preflight needs a live target, and bringing the target up can need
  these very secrets. Each name is its own single-declaration pass, so the
  multi-secret "prompt for A, then fail on an already-doomed B" class does not
  arise. :func:`gate_secret_resolver` SEEDS each value into the later boundary
  resolver as it lands, so no secret resolves or prompts twice.

  Batch session status is deliberately different: its shared preflight and
  boundary resolve complete first, then each VM gate reads ordinary gate
  credentials from that boundary cache. Only a repair key whose need becomes
  known after a start resolves late through the batch callback; it never seeds
  after resolution. The post-boundary cache/late-repair path lives in
  ``sessions.manager._scope._batch_vm_boundary`` and is not an expansion of
  :func:`gate_secret_resolver`.

  In both orderings, repair names (:meth:`GateTarget.repair_secret_refs`) are
  consulted and resolved LAZILY on first read by the repair path, because
  whether a rejoin is needed is knowable only after starting and watching the
  VM fail to reconnect. Resolving eagerly would prompt every start for a key
  almost never used. The node reads only the reader the gate hands it
  (declare/receive holds), and resolution stays orchestrator-owned.
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
    from the gate's values mapping, repair names resolved lazily on
    first read. Lazily-resolved values are recorded into the same mapping
    :func:`ensure_active` returns. What the resolve callback does with them is
    caller-specific: an ordinary pre-boundary gate uses
    :func:`gate_secret_resolver`, which seeds its later boundary resolver;
    batch session status has already completed its boundary and instead
    serves cache hits or performs the one permitted late repair resolve,
    without seeding. Even the repair-name DECLARATION is consulted lazily (on
    a read the eager set does not cover), so an untaken repair path costs
    neither a resolve nor the state lookup the declaration may need.

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


def ensure_active(target: GateTarget, resolve_secret: Callable[[str], str]) -> dict[str, str]:
    """Converge ``target``'s power state: the gate's point half.

    Fast path first (no secret touched); otherwise resolve the
    target's gate secrets just-in-time and drive observe-then-start,
    with the operator-stopped refusal raised from the node's own
    ``auto_start`` and the repair secrets resolving lazily only if the
    repair path reads them. Returns every gate-resolved value, eager and lazy
    alike (empty on the fast path). A singular pre-boundary caller's resolve
    callback seeds its later boundary as each value lands; a batch
    post-boundary caller uses its completed cache and never seeds after
    resolution.
    """
    if target.confirmed_active():
        return {}
    values = {name: resolve_secret(name) for name in target.gate_secret_refs()}
    if target.observed_stopped(ScopedSecrets(values, values.keys())):
        target.auto_start(_GateSecrets(values, target.repair_secret_refs, resolve_secret))
    return values


def gate_secret_resolver(
    config: Config,
    registry: Registry,
    resolver: Resolver,
) -> Callable[[str], str]:
    """The gate's just-in-time resolve callback, shared by every
    command whose gate opens BEFORE its boundary resolve: resolve
    through the normal source chain and SEED the boundary resolver as
    each value lands (``Resolver.seed``), so the boundary pass skips
    the gate-resolved names and no secret resolves or prompts twice in
    one command. (The ops themselves read the gate's scoped reader,
    not the resolver: seeding is purely the no-double-resolve
    property.)"""

    def resolve_gate_secret(secret_name: str) -> str:
        return resolver.resolve_gate(secret_name)

    return resolve_gate_secret


@contextlib.contextmanager
def activation_gate(target: GateTarget, resolve_secret: Callable[[str], str]) -> Iterator[dict[str, str]]:
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
