"""``Registry``: the framework's typed, queryable Resource store.

The Registry is a publish destination, not a parser. Publishers
(``agentworks.config``, ``agentworks.catalog``, future plugin / YAML
manifest publishers) push composed Resources in via
``Registry.add(kind, name, resource, origin)``. After all publishers have
contributed, ``Registry.finalize()`` runs the framework pass: walks the
reference graph, dispatches per-kind miss policies (auto-declare may
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
    from agentworks.resources.reference import ResourceReference


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
        ``built_in`` / future variants) and passes it in; the
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

        0. **Always-materialize reserved-default names** (Phase 2a):
           Kinds whose ``auto_declare_names`` is a non-None set guarantee
           those names exist in the registry after finalize, regardless
           of whether anything referenced them. Seeded before the
           worklist loop so the first pass walks these Resources
           alongside operator-published ones. Kinds with
           ``auto_declare_names = None`` (secrets) are unaffected --
           they stay reference-driven.
        1. **Worklist loop**: walk ``referenced_resources()`` on every
           Resource not yet visited, accumulating references by
           ``(kind, name)`` target. For any target that resolves to no
           Resource currently in the Registry, dispatch the kind's miss
           policy: ``"auto-declare"`` (subject to ``auto_declare_names``)
           calls ``synthesize`` and inserts the result; ``"error"`` raises
           ``ConfigError``. Loop until a pass adds no new Resources --
           synthesized Resources may themselves produce references, so
           a single pass would silently drop their unresolved edges. The
           accumulated reference map is preserved across iterations so
           the post-loop usage-attachment pass sees the complete graph.
        2. **Usage attachment + description polish**: every Resource
           (operator-declared, built-in, auto-declared) gets a
           ``usage`` tuple attached via ``dataclasses.replace`` -- empty
           if nothing referenced it. The kind-agnostic
           description-polish runs in the same pass: for any Resource
           with a ``description`` field that's empty and an
           auto-declared origin, the framework synthesizes a
           description from the reference graph (or, when ``usage``
           is empty, falls back to ``"(auto) auto-declared default
           <kind>"``).
        3. **Cycle detection** in the now-complete reference graph via
           iterative DFS three-coloring; raises ``ConfigError`` on the
           first cycle with the offending path.
        4. **Freeze**.

        First-encountered reference order (for the
        ``Origin.auto_declared(source=...)`` rule) is preserved by
        ``dict``'s guaranteed insertion order in CPython 3.7+. The
        worklist loop appends to the accumulated reference map; the
        order in which references first appear is the order the
        framework records.

        Raises ``RuntimeError`` if already finalized. Raises
        ``ConfigError`` for unresolved references under an error policy,
        reserved-name violations, and cycles.
        """
        if self._frozen:
            raise RuntimeError("registry has already been finalized")

        # 0: always-materialize reserved-default names. Seeds the worklist
        # so unreferenced defaults still land in the registry (FRD R3).
        self._materialize_reserved_defaults()

        # 1: worklist loop. ``walked`` tracks which Resources have had
        # their referenced_resources() walked so we don't double-count.
        all_refs: dict[tuple[str, str], list[ResourceReference]] = {}
        walked: set[tuple[str, str]] = set()

        while True:
            new_walks = self._collect_new_references(all_refs, walked)
            if not new_walks:
                # No new Resources to walk. We're stable.
                break
            # Dispatch miss policies for any targets not yet in the
            # Registry. A miss-handler may add a Resource whose own
            # referenced_resources() the next iteration will walk.
            for target, refs in list(all_refs.items()):
                target_kind, target_name = target
                if target_name in self._resources.get(target_kind, {}):
                    continue
                kind_handler = _lookup_kind(target_kind, refs[0])
                self._handle_miss(target_kind, target_name, kind_handler, refs)

        # 2: references attachment + description polish for every Resource.
        # Iterate all currently-published Resources (not just those with
        # incoming references) so always-materialized rows get
        # ``references=()`` plus the empty-references description fallback too.
        for kind in list(self._resources.keys()):
            for name in list(self._resources[kind].keys()):
                existing = self._resources[kind][name]
                refs = all_refs.get((kind, name), [])
                polished = dataclasses.replace(
                    existing, references=_references_tuple(refs)
                )
                polished = _polish_auto_declared_description(polished, kind)
                self._resources[kind][name] = polished

        # 3: cycle detection across the now-complete graph.
        _detect_cycles(self._resources)

        # 4: freeze.
        self._frozen = True

    def _materialize_reserved_defaults(self) -> None:
        """Seed the registry with reserved-default rows for every kind
        whose ``auto_declare_names`` is a non-None set.

        For each ``(kind, name)`` pair in a kind's reserved set, if the
        name isn't already in the registry (operator-declared or
        published by another publisher), dispatch
        ``synthesize(references=())`` and add the result. Kinds with
        ``auto_declare_names = None`` are skipped -- their resources
        stay reference-driven.

        Origin convention: the kind owns origin assignment for the
        empty-references path. By contract (FRD R3), kinds with
        ``auto_declare_names`` non-None synthesize with
        ``Origin.auto_declared(source=ALWAYS_MATERIALIZE_SOURCE)``
        themselves. The Registry does NOT stamp origin here, distinct
        from ``add``'s stamp-by-the-registry pattern -- the seeded row
        already carries its origin when it reaches this method.

        Called at the start of ``finalize`` before the worklist loop so
        the seeded Resources participate in the reference walk
        alongside operator-published ones (FRD R3, HLA Publish-and-
        finalize section).
        """
        for kind, kind_handler in KIND_REGISTRY.items():
            if kind_handler.auto_declare_names is None:
                continue
            for name in kind_handler.auto_declare_names:
                if name in self._resources.get(kind, {}):
                    continue
                self._resources.setdefault(kind, {})[name] = (
                    kind_handler.synthesize(())
                )

    def _collect_new_references(
        self,
        all_refs: dict[tuple[str, str], list[ResourceReference]],
        walked: set[tuple[str, str]],
    ) -> bool:
        """Walk ``referenced_resources()`` on every Resource not yet in
        ``walked``, appending discovered references into ``all_refs``
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
            for req in _referenced_resources(resource):
                all_refs.setdefault((req.kind, req.name), []).append(req)
        return any_walked

    def _handle_miss(
        self,
        kind: str,
        name: str,
        kind_handler: Any,
        refs: list[ResourceReference],
    ) -> None:
        """Dispatch the kind's miss policy. Mutates ``self._resources``
        for the auto-declare branch; raises ``ConfigError`` otherwise.
        """
        first = refs[0]
        if kind_handler.miss_policy == "auto-declare":
            allowed = kind_handler.auto_declare_names
            if allowed is not None and name not in allowed:
                raise ConfigError(
                    f"{kind} kind only auto-declares the reserved name(s) "
                    f"{sorted(allowed)!r}; got {name!r} "
                    f"(required by {first.source[0]}:{first.source[1]})"
                )
            synthesized = kind_handler.synthesize(refs)
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

    def iter_kind_items(self, kind: str) -> Iterator[tuple[str, Any]]:
        """Iterate ``(name, Resource)`` pairs under one ``kind``. Used by
        the cross-kind ``agw resource list`` / ``describe`` commands which
        need the framework's canonical name (the Registry's per-kind
        dict key) regardless of whether the Resource type carries it on
        a ``.name`` field (most do) or on a different field
        (``SecretBackendConfig.kind``). Empty iterator if the kind has
        no Resources.
        """
        return iter(self._resources.get(kind, {}).items())

    def iter_kinds(self) -> Iterator[str]:
        """Iterate the kind identifiers that currently have at least one
        published Resource. Used by ``agw resource list`` to enumerate
        all kinds when no ``--kind`` filter is given.
        """
        return iter(self._resources.keys())

    @property
    def is_finalized(self) -> bool:
        """True after ``finalize`` has run."""
        return self._frozen


# -- Internal helpers --------------------------------------------------


def _referenced_resources(resource: Any) -> Sequence[ResourceReference]:
    """Return the Resource's ``referenced_resources()`` or an empty
    sequence if it doesn't define one. Phase 1a has no producers wired
    beyond what tests synthesize; Phase 1b adds the method to env-bearing
    Resources, Phase 1c/d to VMTemplate / GitCredentialConfig.
    """
    method = getattr(resource, "referenced_resources", None)
    if method is None:
        return ()
    return tuple(method())


def _lookup_kind(kind: str, req: ResourceReference) -> Any:
    """Look up the kind in ``KIND_REGISTRY``, raising a clear error if
    the reference references a kind no one has registered. Includes
    the reference's source in the error for traceability.
    """
    try:
        return KIND_REGISTRY[kind]
    except KeyError:
        raise ConfigError(
            f"{req.source[0]} {req.source[1]!r} references "
            f"unregistered kind {kind!r}"
        ) from None


def _polish_auto_declared_description(resource: Any, kind: str) -> Any:
    """Synthesize a description for an auto-declared Resource when its
    description is empty. Operators rely on a non-empty Description in
    ``agw resource list`` / ``agw secret list``; the framework derives
    one so the row reads as "what this resource is for and who's asking".

    Two cases share this polish step:

    - **Usage-driven** (auto-declared via incoming reference): set
      from the first matching reference as
      ``"(auto) <usage> for <kind>:<name>"`` plus ``" (and N more)"``
      when more than one distinct source matches.
    - **Empty-usage** (always-materialized reserved default; no incoming
      references): set as ``"(auto) auto-declared default <kind>"``,
      e.g. ``"(auto) auto-declared default vm_template"``.

    Kind-agnostic by design: the framework checks structurally
    (``hasattr(resource, "description")`` + falsy test), not by kind, so
    any future kind that acquires a ``description`` field benefits
    automatically. No-op for resources without a ``description`` field,
    operator-set descriptions, or non-auto-declared origins.
    """
    if not hasattr(resource, "description"):
        return resource
    if resource.description:  # operator-set description honored verbatim
        return resource
    origin = getattr(resource, "origin", None)
    if origin is None or origin.variant != "auto-declared":
        return resource
    references = getattr(resource, "references", ())
    if not references:
        # Always-materialized default with no static incoming references.
        description = f"(auto) auto-declared default {kind}"
    else:
        first = references[0]
        # ReferenceEntry.source is typed tuple[str, str]; the framework
        # guarantees the shape at finalize time. No runtime guard.
        distinct_other = {entry.source for entry in references} - {first.source}
        suffix = f" (and {len(distinct_other)} more)" if distinct_other else ""
        description = (
            f"(auto) {first.usage} for {first.source[0]}:{first.source[1]}{suffix}"
        )
    return dataclasses.replace(resource, description=description)


def _references_tuple(
    refs: Sequence[ResourceReference],
) -> tuple[Any, ...]:
    """Build the ``references`` tuple a Resource carries on it after
    ``finalize``. Lives here (rather than imported from reference.py
    at runtime) to keep the import graph minimal; the return tuple
    holds ``ReferenceEntry`` instances but is typed loosely so this module
    doesn't need a TYPE_CHECKING import for the static typing.

    Projects each outbound ``ResourceReference`` to an inbound
    ``ReferenceEntry`` by keeping ``source`` and ``usage`` (the prose);
    the ``kind``/``name`` fields drop because they're implicit from the
    container Resource the entry attaches to.
    """
    from agentworks.resources.reference import ReferenceEntry

    return tuple(ReferenceEntry(source=r.source, usage=r.usage) for r in refs)


def _detect_cycles(resources: dict[str, dict[str, Any]]) -> None:
    """Detect cycles across the reference graph via iterative DFS
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
    ``(kind, name)``) via the Resource's ``referenced_resources()``.
    Empty iterator if the node isn't in the Registry (defensive; the
    finalize worklist ensures every reachable node has a Resource).
    """
    kind, name = node
    resource = resources.get(kind, {}).get(name)
    if resource is None:
        return
    for req in _referenced_resources(resource):
        yield (req.kind, req.name)
