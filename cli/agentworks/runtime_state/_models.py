"""Invocation-local selections, domain observations, and value-free results."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from agentworks import output
from agentworks.errors import AgentworksError, StateError
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic

if TYPE_CHECKING:
    from contextlib import ExitStack

    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.config import Config
    from agentworks.db import ConsoleRow, Database, DesiredRuntimeState, SessionRow, SessionStatus, VMRow, VMStatus
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.resources import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.manager import PreparedSessionLaunch, PreparedSessionStop, SessionLaunchDefinition
    from agentworks.sessions.multi_console._status import ConsoleStatus
    from agentworks.sessions.multi_console.runtime import PreparedConsoleReconcile, PreparedConsoleStop
    from agentworks.vms.manager.runtime import VMRuntimeContext
    from agentworks.vms.nodes import LiveVMNode

    type Observation = VMStatus | SessionStatus | ConsoleStatus | None

type Kind = Literal["vm", "session", "console"]
type ResourceKey = tuple[Kind, str]
type SessionOperation = Literal["start", "restart", "stop", "stop-all"]


class Outcome(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, kw_only=True)
class RuntimeResult:
    """One outcome with the source of its current observation, independent of earlier work."""

    kind: Kind
    name: str
    desired_runtime_state: DesiredRuntimeState | None
    observation: Observation = None
    observation_from_parent: bool = False
    action: str = "inspect"
    outcome: Outcome = Outcome.UNCHANGED
    reason: str | None = None
    target_known: bool = True


@dataclass(frozen=True)
class RuntimeSelection:
    vms: tuple[VMRow, ...]
    sessions: tuple[SessionRow, ...] = ()
    console_routes: tuple[tuple[str, str], ...] = ()
    explicit_vms: frozenset[str] = frozenset()
    expand_consoles: bool = False
    session_routes: tuple[tuple[str, str], ...] = ()


@dataclass(kw_only=True)
class RuntimeInvocation:
    db: Database
    config: Config
    registry: Registry
    resolver: Resolver
    interaction: TtyInteractionPolicy
    selection: RuntimeSelection
    stack: ExitStack
    operation: SessionOperation | None = None
    force: bool = False
    intent: HarnessLaunchIntent
    authorized_running: frozenset[str] = frozenset()
    guards: dict[str, NativeMutationGuard] = field(default_factory=dict)
    vms: dict[str, VMRow] = field(default_factory=dict)
    sessions: dict[str, SessionRow] = field(default_factory=dict)
    consoles: dict[str, ConsoleRow] = field(default_factory=dict)
    nodes: dict[str, LiveVMNode] = field(default_factory=dict)
    contexts: dict[str, VMRuntimeContext] = field(default_factory=dict)
    definitions: dict[str, SessionLaunchDefinition] = field(default_factory=dict)
    launches: dict[str, PreparedSessionLaunch] = field(default_factory=dict)
    stops: dict[str, PreparedSessionStop] = field(default_factory=dict)
    console_starts: dict[str, PreparedConsoleReconcile] = field(default_factory=dict)
    console_stops: dict[str, PreparedConsoleStop] = field(default_factory=dict)
    results: dict[ResourceKey, RuntimeResult] = field(default_factory=dict)
    accepted_changes: set[str] = field(default_factory=set)
    completed: set[ResourceKey] = field(default_factory=set)
    failed_families: set[str] = field(default_factory=set)
    values: dict[str, str] = field(default_factory=dict)
    redactions: list[str] = field(default_factory=list, repr=False)

    def gate_value(self, name: str) -> str:
        """Always use the invocation Resolver's success and failure memo."""
        value = self.resolver.resolve_gate(name)
        self.redactions.append(value)
        return value

    def record(
        self,
        kind: Kind,
        name: str,
        *,
        outcome: Outcome | None = None,
        action: str | None = None,
        observation: Observation = None,
        observation_from_parent: bool = False,
        reason: str | None = None,
        desired_runtime_state: DesiredRuntimeState | None = None,
    ) -> None:
        current = self.results[(kind, name)]
        self.results[(kind, name)] = replace(
            current,
            outcome=outcome if outcome is not None else current.outcome,
            action=action if action is not None else current.action,
            observation=observation if observation is not None else current.observation,
            observation_from_parent=observation_from_parent
            if observation is not None
            else current.observation_from_parent,
            reason=reason if reason is not None else current.reason,
            desired_runtime_state=desired_runtime_state
            if desired_runtime_state is not None
            else current.desired_runtime_state,
        )

    def fail(self, kind: Kind, name: str, error: Exception | str, *, blocked: bool = False) -> None:
        detail = f"{error} {error.hint}" if isinstance(error, AgentworksError) and error.hint else str(error)
        reason = sanitize_setup_diagnostic(detail, redactions=self.redactions)[:500]
        self.record(kind, name, outcome=Outcome.BLOCKED if blocked else Outcome.FAILED, reason=reason)

    def unavailable(self, kind: Kind, name: str) -> bool:
        return self.results[(kind, name)].outcome in {Outcome.BLOCKED, Outcome.FAILED}

    def ensure_vm_result(self, name: str) -> None:
        vm = self.vms[name]
        self.results.setdefault(
            ("vm", name),
            RuntimeResult(kind="vm", name=name, desired_runtime_state=vm.desired_runtime_state),
        )

    def close(self) -> None:
        self.values.clear()
        for context in self.contexts.values():
            context.clear_values()
        self.resolver.close()
        self.redactions.clear()


def render_results(results: tuple[RuntimeResult, ...], *, dry_run: bool = False) -> None:
    if dry_run:
        output.info("Runtime reconciliation preview")
    for result in results:
        target = result.desired_runtime_state or ("adoption required" if result.target_known else "unavailable")
        observed = result.observation.value if result.observation is not None else "unavailable"
        outcome = "would change" if dry_run and result.outcome is Outcome.CHANGED else result.outcome
        reason = f": {result.reason}" if result.reason else ""
        qualification = (
            "; parent VM inactive, guest uninspected for this observation; "
            "inactivity alone does not clear recorded runtime evidence"
            if result.observation_from_parent
            else ""
        )
        output.result(
            f"{result.kind} '{result.name}': {outcome} "
            f"({result.action}; desired {target}; observed {observed}{qualification}){reason}"
        )


def require_success(results: tuple[RuntimeResult, ...]) -> None:
    failures = [result for result in results if result.outcome in {Outcome.FAILED, Outcome.BLOCKED}]
    if len(failures) == 1:
        result = failures[0]
        raise StateError(
            result.reason or "requested runtime state is not satisfied",
            entity_kind=result.kind,
            entity_name=result.name,
        )
    if failures:
        raise StateError(f"{len(failures)} resource(s) could not reach their requested runtime state")
