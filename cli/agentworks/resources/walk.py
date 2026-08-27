"""Subgraph walks across the Resource Registry.

``collect_secrets_for(registry, root)`` returns the ``SecretDecl`` Resources
``root`` needs at run time, following the retained dependency graph's
runtime-need edges. It answers "which secrets does this subgraph call for",
which is what runtime composition needs. Database-backed resource publication
uses each domain's typed effective-reference extractor instead; the finalized
graph then owns direct live-use facts for inspection. This walk still covers
the system-level secrets (Tailscale, git-credential tokens) an env-block walk
does not reach.

The orchestrator's own union is a different computation and deliberately so:
it comes off a plan's nodes (``orchestration.secrets.secret_union``), which
hold already-resolved templates, so it never reads the graph.
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
    ``root``, a thin filter over the graph's ``runtime_reachable_from`` query
    (R11: the graph is the single access path; this no longer hand-rolls a DFS
    over ``dependencies()``).

    The RUNTIME-need closure, not the full one (FR17). An inheritance edge is
    source composition, and an inheriting row already publishes its merged
    declaration's secrets as edges of its own, so crossing it here would add
    the parent's standalone secrets on top: a child that overrode the parent's
    auth-key name would come back needing both names, and the operator would
    be asked for a secret nothing uses.

    Order: first-encounter, as the closure yields it, deduplicated. The
    root itself isn't included (it's the publisher, not a target); only secrets
    the root and its transitive references point at are returned. The
    ``secret -> secret-source`` edges the graph now carries do not add secrets
    (sources are a distinct kind), so the filtered result matches the old
    walk's secret set.

    The Registry must be finalized (the closure reads the retained
    graph). Raises ``KeyError`` if ``root`` doesn't resolve to a Resource in the
    Registry; the manager-entry caller is expected to have looked up the
    resource (``vm.template`` etc.) before calling.
    """
    registry.lookup(*root)  # preserve the "unknown root raises KeyError" contract
    secret_decls: list[SecretDecl] = []
    # The closure excludes the start node, so a secret-typed ``root`` is
    # not in its own result. The old hand-rolled DFS included such a root; the
    # divergence is deliberate and unobservable (no caller passes a secret root)
    # and matches this function's contract that "the root itself isn't included".
    for kind, name in registry.graph.runtime_reachable_from(*root):
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
