"""The activation gate: power-state convergence plus the held-active
span.

Ordinary singular commands that touch an EXISTING VM converge its power
state before their preflight and boundary resolve, so every readiness
probe that reaches the target queries a live environment. Their gate
OPENS after build and BEFORE the preflight sweep. Batch lifecycle preparation
also resolves gate names before its shared workload boundary, reusing the
same operation Resolver across VM families.
The gate is a SPAN, not a point: it stays open through
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
- **The node is the authority on auto-start.** Stopped runtime intent
  prohibits automatic power-on. The node checks its current identity and
  intent under the same VM-family guard used by lifecycle mutations.
  Hold admission shares that guard because a backend hold can itself boot.
- **Gate secrets are delivered just-in-time, with caller ordering explicit.**
  Observing and starting a stopped VM may need the platform's API credential.
  For an ordinary singular command, the gate opens before preflight and
  boundary resolution. Its narrow, known names resolve through the normal
  source chain only after the secret-free fast path fails. Resolving there is
  INHERENT rather than a resolve/preflight-ordering gap (issue #202):
  on-target preflight needs a live target, and bringing the target up can need
  these very secrets. Each name is its own single-declaration pass, so the
  multi-secret "prompt for A, then fail on an already-doomed B" class does not
  arise. :func:`gate_secret_resolver` SEEDS each value into the later boundary
  resolver as it lands, so no secret resolves or prompts twice.

  In batch preparation, infrastructure failures stay local to their dependent
  families; Resolver remembers value-free failures and rejects a workload
  union requiring one before any further source call. Nothing resolves after
  a boundary.

  The gate resolves nothing else. A started VM that does not answer over SSH
  fails naming ``agw vm recover``, whose own boundary resolves the Tailscale
  auth key its repair may need, so no gate prompts for a key or carries a
  conditional one.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

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

    def confirmed_active(self) -> bool:
        """Cheap, secret-free fast path: ``True`` only when the target
        is positively known active (today's Tailscale reachability
        probe, skipped when the row already records stopped intent).
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
        """Start an auto-stopped target and wait for it to answer,
        failing with guidance when it does not. ``gate_secrets`` serves
        only the gate's own names. The node re-reads its runtime intent
        here and REFUSES a target with stopped intent with a typed error
        and the explicit-start hint: the node, not the helper, is the
        authority."""
        ...

    def hold_active(self) -> contextlib.AbstractContextManager[None]:
        """Hold the target against the backend's idle-shutdown
        mechanism (the ``vm_active`` span); a no-op context for
        platforms with nothing to hold."""
        ...


def ensure_active(target: GateTarget, resolve_secret: Callable[[str], str]) -> dict[str, str]:
    """Converge ``target``'s power state: the gate's point half.

    Fast path first (no secret touched); otherwise resolve the
    target's gate secrets just-in-time and drive observe-then-start,
    with the stopped-intent refusal raised from the node's own
    ``auto_start``. Returns every gate-resolved value (empty on the fast
    path). A singular pre-boundary caller's resolve callback seeds its
    later boundary as each value lands; a post-boundary caller (rekey)
    serves its completed cache and never seeds after resolution.
    """
    if target.confirmed_active():
        return {}
    values = {name: resolve_secret(name) for name in target.gate_secret_refs()}
    gate_secrets = ScopedSecrets(values, values.keys())
    if target.observed_stopped(gate_secrets):
        target.auto_start(gate_secrets)
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
