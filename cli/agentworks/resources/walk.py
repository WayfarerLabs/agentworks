"""Subgraph walks across the Resource Registry.

``collect_secrets_for(registry, root)`` walks a Resource's
``required_resources()`` DFS from ``root``, dedupes targets by
``(kind, name)``, and returns the SecretDecls reachable from the walk.
Used by manager-entry code (``vm create``, ``agent create``, etc.) to
build the ``extra_decls`` list passed to
``agentworks.secrets.orchestration.resolve_for_command`` for
eager-resolve. The orchestrator does its own env-block walk via
``SecretTarget``; ``collect_secrets_for`` covers system-level secrets
(Tailscale, git-credential tokens) the env-block walk doesn't reach.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.resources.registry import Registry
    from agentworks.resources.requirement import ResourceRequirement
    from agentworks.secrets.base import SecretDecl


def collect_secrets_for(
    registry: Registry,
    root: tuple[str, str],
) -> list[SecretDecl]:
    """Walk ``required_resources()`` depth-first from ``root``; collect
    ``SecretDecl`` Resources reachable from the walk.

    Order: first-encounter via DFS, deduplicated by secret name. The
    root itself isn't included (it's the publisher, not a target);
    only secrets the root and its transitive requirements point at are
    returned.

    The Registry must be finalized (so synthesized auto-declares exist
    and ``Origin.auto_declared.source`` references are accurate). Calls
    against a non-finalized Registry are not guaranteed correct; the
    finalize pass is what walks every Resource's requirements and
    auto-declares missing names.

    Raises ``KeyError`` if ``root`` doesn't resolve to a Resource in the
    Registry; the manager-entry caller is expected to have looked up
    the resource (``vm.template`` etc.) before calling.
    """
    seen: set[tuple[str, str]] = set()
    secret_decls: list[SecretDecl] = []
    stack: list[tuple[str, str]] = [root]

    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        try:
            resource = registry.lookup(*node)
        except KeyError:
            # Defensive: the finalize pass should have auto-declared any
            # missing references the root reaches, so an unreached
            # (kind, name) here means a typo in producer code; raise so
            # the caller hears about it. Use the same KeyError shape
            # registry.lookup raises.
            if node == root:
                raise
            continue

        if node[0] == "secret":
            secret_decls.append(resource)

        # Walk this resource's requirements next.
        for req in _required_resources(resource):
            target = (req.kind, req.name)
            if target not in seen:
                stack.append(target)

    return secret_decls


def _required_resources(resource: object) -> list[ResourceRequirement]:
    """Mirror of ``Registry._required_resources``: duck-type via
    ``getattr`` so resources without the method return no requirements.
    """
    method = getattr(resource, "required_resources", None)
    if method is None:
        return []
    return list(method())
