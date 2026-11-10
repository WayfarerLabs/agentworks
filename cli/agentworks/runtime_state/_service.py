"""One invocation owns reconciliation guards, credentials, and result delivery."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING

from agentworks.errors import UserAbort

from ._execute import execute_deferred_work, final_observations, resolve_workload_boundary
from ._models import RuntimeInvocation, render_results, require_success
from ._prepare import prepare_guest_workloads, prepare_infrastructure, prepare_local_workloads
from ._selection import acquire_families, seed_results

if TYPE_CHECKING:
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.secrets.policy import TtyInteractionPolicy

    from ._models import RuntimeSelection, SessionOperation


def run_selected(
    db: Database,
    config: Config,
    selection: RuntimeSelection,
    *,
    interaction: TtyInteractionPolicy,
    intent: HarnessLaunchIntent,
    operation: SessionOperation | None = None,
    force: bool = False,
    authorized_running: frozenset[str] = frozenset(),
) -> None:
    from agentworks.bootstrap import load_request_registry
    from agentworks.secrets.resolver import Resolver

    registry = load_request_registry(config, live_database=db)
    resolver = Resolver(config, registry, interaction=interaction)
    with ExitStack() as stack:
        invocation = RuntimeInvocation(
            db=db,
            config=config,
            registry=registry,
            resolver=resolver,
            interaction=interaction,
            selection=selection,
            stack=stack,
            intent=intent,
            operation=operation,
            force=force,
            authorized_running=authorized_running,
        )
        seed_results(invocation)
        try:
            acquire_families(invocation)
            prepare_local_workloads(invocation)
            prepare_infrastructure(invocation)
            prepare_guest_workloads(invocation)
            if resolve_workload_boundary(invocation):
                execute_deferred_work(invocation)
                final_observations(invocation)
            results = tuple(invocation.results.values())
            render_results(results)
            require_success(results)
        except UserAbort:
            for kind, name in tuple(invocation.results):
                if (kind, name) not in invocation.completed and not invocation.unavailable(kind, name):
                    invocation.fail(kind, name, "operation cancelled before runtime work completed", blocked=True)
            render_results(tuple(invocation.results.values()))
            raise
        finally:
            invocation.close()
