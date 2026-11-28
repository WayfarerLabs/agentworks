"""One invocation owns its families' lock sets, credentials, and result delivery.

The order, per family in VM name order: (a) read the selection, (b) probe
each family's set, (d) record intent under a brief hold of that set, (e)
prepare infrastructure unlocked (gate secrets, provider observation, a boot
under a brief vm exclusive), then, once every family has finished (e), (f)
acquire each family's set, held to the end, and (g) re-check it, before (h)
the guest work, the workload boundary and execution.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING

from agentworks.db import DatabaseDriverError
from agentworks.errors import StateConflictError, UserAbort

from ._execute import execute_deferred_work, final_observations, resolve_workload_boundary
from ._families import acquire_final_sets, probe_families
from ._models import RuntimeInvocation, render_results, require_success
from ._prepare import prepare_guest_workloads, prepare_infrastructure, prepare_local_workloads
from ._selection import read_families, seed_results

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy

    from ._models import RuntimeSelection, SessionDeclaration, SessionOperation


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
    declarations: Mapping[str, SessionDeclaration] | None = None,
) -> None:
    from agentworks.bootstrap import load_request_registry
    from agentworks.secrets.resolver import Resolver

    declarations = declarations or {}
    registry = load_request_registry(
        config,
        live_database=db,
        pending_publishers=tuple(_publisher(declaration) for declaration in declarations.values()),
    )
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
            declarations=declarations,
        )
        seed_results(invocation)
        try:
            read_families(invocation)
            probe_families(invocation)
            prepare_local_workloads(invocation)
            prepare_infrastructure(invocation)
            acquire_final_sets(invocation)
            prepare_guest_workloads(invocation)
            if resolve_workload_boundary(invocation):
                execute_deferred_work(invocation)
                final_observations(invocation)
            results = tuple(invocation.results.values())
            render_results(results)
            require_success(results)
        except (StateConflictError, DatabaseDriverError) as error:
            for kind, name in tuple(invocation.results):
                if (kind, name) not in invocation.completed and not invocation.unavailable(kind, name):
                    invocation.fail(kind, name, error, blocked=True)
            render_results(tuple(invocation.results.values()))
            raise
        except (UserAbort, KeyboardInterrupt):
            for kind, name in tuple(invocation.results):
                if (kind, name) not in invocation.completed and not invocation.unavailable(kind, name):
                    invocation.fail(kind, name, "operation cancelled before runtime work completed", blocked=True)
            render_results(tuple(invocation.results.values()))
            raise
        finally:
            invocation.close()


def _publisher(declaration: SessionDeclaration) -> Callable[[Registry], None]:
    return lambda registry: registry.add_live(declaration.pending)
