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
the ops today's ``vms.manager.ensure_active`` / ``keep_active`` call
(the parity oracle; the imperative pair keeps serving un-migrated
commands and retires with them).

Three properties are load-bearing:

- **Maintenance, not plan mutation.** Convergence is idempotent
  declared-state maintenance: never rollback-tracked (a VM
  auto-started for a command that later fails stays up; the span just
  closes), and it does not bend "preflight-all before any mutation",
  which governs the command's PLAN.
- **The node is the authority on auto-start.** Auto-start applies only
  to an auto-stopped VM; a manually stopped one (``operator_stopped``)
  refuses with a typed error raised within the node's own scope
  (:meth:`GateTarget.auto_start`), including the re-read-the-flag race
  guard today's ``ensure_active`` does.
- **Gate secrets resolve JUST-IN-TIME, outside the boundary pass.**
  Observing and starting a stopped VM may need the platform's API
  credential (the common case) or the Tailscale auth key (the rejoin
  repair case). This is the one sanctioned resolution outside the
  boundary pass: narrow, known names, resolved through the normal
  backend chain, entirely pre-walk-away, and skipped altogether on the
  fast path (a confirmed-active VM costs no resolution and no
  interaction). The resolved values are returned to the orchestrator
  so they can SEED the boundary pass and no secret resolves or prompts
  twice in one command (the seeding path itself is designed with the
  first migrated caller; see the plan, Phase 1).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

from agentworks.orchestration.secrets import ScopedSecrets

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.capabilities.base import SecretReader


class GateTarget(Protocol):
    """The power-state surface the gate drives: the live VM node's own
    vocabulary, sliced structurally so the helper stays domain-blind.

    The live VM node is the implementation (it composes its held
    platform instance's ops); test doubles satisfy it directly.
    """

    def gate_secret_refs(self) -> tuple[str, ...]:
        """The narrow secret names this target's power-state ops need:
        the platform API credential to observe/start, plus the rejoin
        repair secret where applicable."""
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
        reusable-key messaging). The node re-reads its
        operator-stopped intent here and REFUSES a manually stopped
        target with a typed error and the explicit-start hint: the
        node, not the helper, is the authority."""
        ...

    def hold_active(self) -> contextlib.AbstractContextManager[None]:
        """Hold the target against the backend's idle-shutdown
        mechanism (the ``vm_active`` span); a no-op context for
        platforms with nothing to hold."""
        ...


def ensure_active(
    target: GateTarget, resolve_secret: Callable[[str], str]
) -> dict[str, str]:
    """Converge ``target``'s power state: the gate's point half.

    Fast path first (no secret touched); otherwise resolve the
    target's gate secrets just-in-time and drive observe-then-start,
    with the operator-stopped refusal raised from the node's own
    ``auto_start``. Returns the gate-resolved values (empty on the
    fast path) for the orchestrator to seed the boundary pass with.
    """
    if target.confirmed_active():
        return {}
    values = {name: resolve_secret(name) for name in target.gate_secret_refs()}
    reader = ScopedSecrets(values, values.keys())
    if target.observed_stopped(reader):
        target.auto_start(reader)
    return values


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
