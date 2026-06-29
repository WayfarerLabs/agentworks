"""``Registry``: the framework's typed, queryable Resource store.

The Registry is a publish destination, not a parser. Publishers
(``agentworks.config``, ``agentworks.catalog``, future plugin / YAML
manifest publishers) push composed Resources in via
``Registry.add(kind, name, resource, origin)``. After all publishers have
contributed, ``Registry.finalize()`` runs the framework pass: walks the
requirement graph, dispatches per-kind miss policies (auto-declare may
synthesize new Resources; error raises ``ConfigError``), attaches
``usage`` lists, detects cycles, and freezes the Registry. After
``finalize`` returns, the Registry is read-only and queryable via
``lookup`` / ``iter_kind``.

The convenience that orchestrates the standard set of publishers lives in
``agentworks.bootstrap.build_registry``. The Registry itself doesn't know
which publishers exist; that's application-level knowledge.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from agentworks.errors import ConfigError
from agentworks.resources.kind import KIND_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from agentworks.resources.origin import Origin
    from agentworks.resources.requirement import ResourceRequirement


class Registry:
    """The framework's Resource store. Construct via ``Registry.empty()``;
    publish via ``add``; finalize via ``finalize``; query via ``lookup`` /
    ``iter_kind``.

    Lifecycle:

    1. ``Registry.empty()`` -> mutable Registry, no Resources.
    2. Each publisher calls ``registry.add(kind, name, resource, origin)``
       one or more times.
    3. ``registry.finalize()`` runs the framework pass and locks the
       Registry. ``add`` raises after this; lookup is available.
    """

    def __init__(self) -> None:
        # Intentionally constructed via ``empty()``; the bare constructor
        # is fine for tests / framework code that wants a stub.
        self._resources: dict[str, dict[str, Any]] = {}
        self._frozen: bool = False

    # -- Construction --------------------------------------------------

    @classmethod
    def empty(cls) -> Registry:
        """Return a fresh empty Registry. The canonical entry point;
        ``__init__`` is not part of the public surface (call sites
        spelling it out aren't wrong, just less explicit about the
        empty-state semantics)."""
        return cls()

    # -- Publish phase -------------------------------------------------

    def add(
        self,
        kind: str,
        name: str,
        resource: Any,
        origin: Origin,
    ) -> None:
        """Add a Resource from any publisher. The publisher constructs
        the appropriate ``Origin`` variant (``operator_declared`` /
        ``code_declared`` / future variants) and passes it in; the
        Registry attaches it to the Resource via ``dataclasses.replace``
        and stores the result keyed by ``(kind, name)``.

        Raises ``RuntimeError`` if the Registry has been finalized.
        Adding the same ``(kind, name)`` twice replaces the prior entry
        (publishers are expected not to publish duplicates; the test
        suite covers this; production publishers don't do it).
        """
        if self._frozen:
            raise RuntimeError("registry is frozen; add must precede finalize")
        stamped = dataclasses.replace(resource, origin=origin)
        self._resources.setdefault(kind, {})[name] = stamped

    # -- Finalize phase ------------------------------------------------

    def finalize(self) -> None:
        """Run the framework pass over published Resources, then freeze.

        Steps:

        1. **Worklist loop**: walk ``required_resources()`` on every
           Resource not yet visited, accumulating requirements by
           ``(kind, name)`` target. For any target that resolves to no
           Resource currently in the Registry, dispatch the kind's miss
           policy: ``"auto-declare"`` (subject to ``auto_declare_names``)
           calls ``synthesize`` and inserts the result; ``"error"`` raises
           ``ConfigError``. Loop until a pass adds no new Resources --
           synthesized Resources may themselves produce requirements, so
           a single pass would silently drop their unresolved edges. The
           accumulated requirement map is preserved across iterations so
           the post-loop usage-attachment pass sees the complete graph.
        2. **Usage attachment**: every Resource (operator-declared,
           code-declared, auto-declared) that has incoming requirements
           gets a ``usage`` tuple attached via ``dataclasses.replace``.
           Both publish-time Origin and synthesize-time Origin already
           landed; usage is centralized here so the kind's ``synthesize``
           doesn't have to know the final requirement map.
        3. **Cycle detection** in the now-complete requirement graph via
           iterative DFS three-coloring; raises ``ConfigError`` on the
           first cycle with the offending path.
        4. **Freeze**.

        First-encountered requirement order (for the
        ``Origin.auto_declared(source=...)`` rule) is preserved by
        ``dict``'s guaranteed insertion order in CPython 3.7+. The
        worklist loop appends to the accumulated requirement map; the
        order in which requirements first appear is the order the
        framework records.

        Raises ``RuntimeError`` if already finalized. Raises
        ``ConfigError`` for unresolved references under an error policy,
        reserved-name violations, and cycles.
        """
        if self._frozen:
            raise RuntimeError("registry has already been finalized")

        # 1: worklist loop. ``walked`` tracks which Resources have had
        # their required_resources() walked so we don't double-count.
        all_reqs: dict[tuple[str, str], list[ResourceRequirement]] = {}
        walked: set[tuple[str, str]] = set()

        while True:
            new_walks = self._collect_new_requirements(all_reqs, walked)
            if not new_walks:
                # No new Resources to walk. We're stable.
                break
            # Dispatch miss policies for any targets not yet in the
            # Registry. A miss-handler may add a Resource whose own
            # required_resources() the next iteration will walk.
            for target, reqs in list(all_reqs.items()):
                target_kind, target_name = target
                if target_name in self._resources.get(target_kind, {}):
                    continue
                kind_handler = _lookup_kind(target_kind, reqs[0])
                self._handle_miss(target_kind, target_name, kind_handler, reqs)

        # 2: usage attachment. Every Resource with incoming requirements
        # gets its usage tuple set via dataclasses.replace. Auto-declared
        # secrets additionally get a synthesized description so the list
        # view's Description column has meaningful text (the polish needs
        # the final usage tuple, so it lands here rather than at the
        # kind's synthesize-time call site).
        for (kind, name), reqs in all_reqs.items():
            existing = self._resources[kind][name]
            polished = dataclasses.replace(existing, usage=_usage_tuple(reqs))
            polished = _polish_auto_declared_description(polished)
            self._resources[kind][name] = polished

        # 3: cycle detection across the now-complete graph.
        _detect_cycles(self._resources)

        # 4: freeze.
        self._frozen = True

    def _collect_new_requirements(
        self,
        all_reqs: dict[tuple[str, str], list[ResourceRequirement]],
        walked: set[tuple[str, str]],
    ) -> bool:
        """Walk ``required_resources()`` on every Resource not yet in
        ``walked``, appending discovered requirements into ``all_reqs``
        (in first-encountered order). Returns True if any Resource was
        walked this pass; False means the worklist has stabilized.
        """
        any_walked = False
        # Snapshot the current per-kind dicts so iteration is safe
        # against concurrent additions by miss-policy dispatch in this
        # outer pass.
        snapshot: list[tuple[tuple[str, str], Any]] = []
        for kind, kind_dict in self._resources.items():
            for name, resource in kind_dict.items():
                key = (kind, name)
                if key not in walked:
                    snapshot.append((key, resource))
        for key, resource in snapshot:
            walked.add(key)
            any_walked = True
            for req in _required_resources(resource):
                all_reqs.setdefault((req.kind, req.name), []).append(req)
        return any_walked

    def _handle_miss(
        self,
        kind: str,
        name: str,
        kind_handler: Any,
        reqs: list[ResourceRequirement],
    ) -> None:
        """Dispatch the kind's miss policy. Mutates ``self._resources``
        for the auto-declare branch; raises ``ConfigError`` otherwise.
        """
        first = reqs[0]
        if kind_handler.miss_policy == "auto-declare":
            allowed = kind_handler.auto_declare_names
            if allowed is not None and name not in allowed:
                raise ConfigError(
                    f"{kind} kind only auto-declares the reserved name(s) "
                    f"{sorted(allowed)!r}; got {name!r} "
                    f"(required by {first.source[0]}:{first.source[1]})"
                )
            synthesized = kind_handler.synthesize(reqs)
            self._resources.setdefault(kind, {})[name] = synthesized
            return
        if kind_handler.miss_policy == "error":
            raise ConfigError(
                f"{first.source[0]} {first.source[1]!r} references "
                f"unknown {kind} {name!r}"
            )
        raise RuntimeError(
            f"unexpected miss_policy {kind_handler.miss_policy!r} on "
            f"KIND_REGISTRY[{kind!r}]"
        )

    # -- Query phase ---------------------------------------------------

    def lookup(self, kind: str, name: str) -> Any:
        """Return the Resource at ``(kind, name)``. Raises ``KeyError``
        if not present. Available before and after ``finalize`` (test
        scaffolding uses this during publish); operator-facing code
        should only lookup after finalize.
        """
        return self._resources[kind][name]

    def iter_kind(self, kind: str) -> Iterator[Any]:
        """Iterate Resources under one ``kind``. Empty iterator if the
        kind has no Resources (or no Resources have been published under
        it yet).
        """
        return iter(self._resources.get(kind, {}).values())

    @property
    def is_finalized(self) -> bool:
        """True after ``finalize`` has run."""
        return self._frozen


# -- Internal helpers --------------------------------------------------


def _required_resources(resource: Any) -> Sequence[ResourceRequirement]:
    """Return the Resource's ``required_resources()`` or an empty
    sequence if it doesn't define one. Phase 1a has no producers wired
    beyond what tests synthesize; Phase 1b adds the method to env-bearing
    Resources, Phase 1c/d to VMTemplate / GitCredentialConfig.
    """
    method = getattr(resource, "required_resources", None)
    if method is None:
        return ()
    return tuple(method())


def _lookup_kind(kind: str, req: ResourceRequirement) -> Any:
    """Look up the kind in ``KIND_REGISTRY``, raising a clear error if
    the requirement references a kind no one has registered. Includes
    the requirement's source in the error for traceability.
    """
    try:
        return KIND_REGISTRY[kind]
    except KeyError:
        raise ConfigError(
            f"{req.source[0]} {req.source[1]!r} references "
            f"unregistered kind {kind!r}"
        ) from None


def _polish_auto_declared_description(resource: Any) -> Any:
    """Synthesize a description for an auto-declared SecretDecl when its
    description is empty. Operators rely on a non-empty Description in
    ``agw secret list``; the framework derives one from the first
    requirement's usage text + source so the row reads as "what this
    secret is for and who's asking".

    Format: ``"(auto) <usage> for <kind>:<name>"`` plus, when more than
    one distinct source requires this secret, ``" (and N more)"`` (N
    counts distinct sources other than the first one already named).
    No-op for non-secrets, operator-declared resources, code-declared
    resources, secrets with no recorded usage, or any SecretDecl whose
    description is already set.
    """
    from agentworks.secrets.base import SecretDecl

    if not isinstance(resource, SecretDecl):
        return resource
    if resource.description:
        return resource
    origin = resource.origin
    if origin is None or origin.variant != "auto-declared":
        return resource
    if not resource.usage:
        return resource
    first = resource.usage[0]
    if not (isinstance(first.source, tuple) and len(first.source) == 2):
        return resource
    distinct_other = {u.source for u in resource.usage} - {first.source}
    suffix = f" (and {len(distinct_other)} more)" if distinct_other else ""
    description = (
        f"(auto) {first.text} for {first.source[0]}:{first.source[1]}{suffix}"
    )
    return dataclasses.replace(resource, description=description)


def _usage_tuple(
    reqs: Sequence[ResourceRequirement],
) -> tuple[Any, ...]:
    """Build the ``usage`` tuple a Resource carries on it after
    ``finalize``. Lives here (rather than imported from requirement.py
    at runtime) to keep the import graph minimal; the return tuple
    holds ``UsageEntry`` instances but is typed loosely so this module
    doesn't need a TYPE_CHECKING import for the static typing.
    """
    from agentworks.resources.requirement import UsageEntry

    return tuple(UsageEntry(source=r.source, text=r.usage) for r in reqs)


def _detect_cycles(resources: dict[str, dict[str, Any]]) -> None:
    """Detect cycles across the requirement graph via iterative DFS
    three-coloring.

    Phase 1 has no producers that introduce cycles (secrets don't
    reference secrets); the check runs for completeness and is the
    backbone of Phase 2's template-inheritance validation. Implemented
    iteratively so deep inheritance chains in Phase 2 don't risk
    CPython's default recursion limit.

    Raises ``ConfigError`` with the cycle path on the first cycle.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[tuple[str, str], int] = {}

    for start_kind, kind_dict in resources.items():
        for start_name in kind_dict:
            start_node = (start_kind, start_name)
            if color.get(start_node, WHITE) != WHITE:
                continue

            # Iterative DFS via a work stack. Each frame is a tuple of
            # (node, edge_iterator). When we descend, push the parent
            # frame back with its iterator; when we exhaust the iterator,
            # color the node BLACK and pop.
            color[start_node] = GRAY
            path: list[tuple[str, str]] = [start_node]
            edge_stack: list[Any] = [
                iter(_edges_from(resources, start_node))
            ]
            while edge_stack:
                edges = edge_stack[-1]
                try:
                    target = next(edges)
                except StopIteration:
                    color[path[-1]] = BLACK
                    path.pop()
                    edge_stack.pop()
                    continue
                target_color = color.get(target, WHITE)
                if target_color == GRAY:
                    cycle = path[path.index(target):] + [target]
                    cycle_path = " -> ".join(f"{k}:{n}" for k, n in cycle)
                    raise ConfigError(
                        f"resource reference cycle detected: {cycle_path}"
                    )
                if target_color == BLACK:
                    continue
                color[target] = GRAY
                path.append(target)
                edge_stack.append(iter(_edges_from(resources, target)))


def _edges_from(
    resources: dict[str, dict[str, Any]],
    node: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Yield outgoing edges from ``node`` (``(kind, name)`` -> target
    ``(kind, name)``) via the Resource's ``required_resources()``.
    Empty iterator if the node isn't in the Registry (defensive; the
    finalize worklist ensures every reachable node has a Resource).
    """
    kind, name = node
    resource = resources.get(kind, {}).get(name)
    if resource is None:
        return
    for req in _required_resources(resource):
        yield (req.kind, req.name)
