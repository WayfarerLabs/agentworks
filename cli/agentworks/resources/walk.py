"""Subgraph walks across the Resource Registry.

``collect_secrets_for(registry, root)`` returns the ``SecretDecl`` Resources
transitively reachable from ``root`` in the retained dependency graph. Used by
manager-entry code (``vm create``, ``agent create``, etc.) to build the
``extra_decls`` list passed to
``agentworks.secrets.orchestration.resolve_for_command`` for eager-resolve. The
orchestrator does its own env-block walk via ``SecretTarget``;
``collect_secrets_for`` covers system-level secrets (Tailscale, git-credential
tokens) the env-block walk doesn't reach.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl


def collect_secrets_for(
    registry: Registry,
    root: tuple[str, str],
) -> list[SecretDecl]:
    """Collect the ``SecretDecl`` Resources transitively reachable from
    ``root``, a thin filter over the graph's ``reachable_from`` query (R11: the
    graph is the single access path; this no longer hand-rolls a DFS over
    ``dependencies()``).

    Order: first-encounter, as ``reachable_from`` yields it, deduplicated. The
    root itself isn't included (it's the publisher, not a target); only secrets
    the root and its transitive references point at are returned. The
    ``secret -> secret-backend`` edges the graph now carries do not add secrets
    (backends are a distinct kind), so the filtered result matches the old
    walk's secret set.

    The Registry must be finalized (``reachable_from`` reads the retained
    graph). Raises ``KeyError`` if ``root`` doesn't resolve to a Resource in the
    Registry; the manager-entry caller is expected to have looked up the
    resource (``vm.template`` etc.) before calling.
    """
    registry.lookup(*root)  # preserve the "unknown root raises KeyError" contract
    secret_decls: list[SecretDecl] = []
    for kind, name in registry.graph.reachable_from(*root):
        if kind != "secret":
            continue
        try:
            secret_decls.append(registry.lookup(kind, name))
        except KeyError:
            # An edge target left unmaterialized (a not-ready node's gated
            # secret, R12) appears in the reachable key list but has no row;
            # skip it, matching the old walk's "skip an unreached target".
            continue
    return secret_decls
