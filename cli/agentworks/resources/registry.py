"""``Registry``: the framework's typed, queryable Resource store.

The Registry is a publish destination, not a parser. Publishers
(``agentworks.config``, ``agentworks.apt``, ``agentworks.install_commands``,
the bundled built-in manifests, future plugin / YAML manifest publishers)
push composed Resources in via ``Registry.add(kind, name, resource,
origin)``. After all publishers have contributed, ``Registry.finalize()``
runs the framework pass: walks the reference graph, dispatches per-kind
miss policies (auto-declare may
synthesize new Resources; error raises ``ConfigError``), builds the
retained ``DependencyGraph``, detects cycles, and freezes the Registry.
After ``finalize`` returns, the Registry is read-only and queryable via
``lookup`` / ``iter_kind`` / ``graph``.

The convenience that orchestrates the standard set of publishers lives in
``agentworks.bootstrap.build_registry``. The Registry itself doesn't know
which publishers exist; that's application-level knowledge.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from agentworks.errors import ConfigError, StateError
from agentworks.resources.kind import KIND_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from agentworks.resources.graph import DependencyGraph, Enablement, Readiness
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


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
        # The retained dependency graph, built at the end of ``finalize``.
        # ``None`` until then; the ``graph`` property enforces the ordering.
        self._graph: DependencyGraph | None = None

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

        Collisions (same ``(kind, name)`` added twice) are handled
        explicitly (dual-path sources, ADR 0016):

        - operator row over operator row: ``ConfigError`` citing both
          declaration locations. The manifest loader catches duplicates
          within its own set; this is the backstop that also catches a
          resource declared in both TOML and a manifest (a permanent
          dual-path condition).
        - operator row over built-in row: consults the kind's
          ``builtin_override`` flag. ``"allow"`` keeps the operator
          override (operator row replaces the built-in); ``"reserved"``
          raises ``ConfigError`` naming the reserved built-in.
        - built-in row over built-in row: replaces (idempotent
          republish).
        - anything else (built-in over operator) is a publisher
          ordering conflict and raises ``ConfigError`` (operator data
          can reach it, so it must not be an assert).
        """
        if self._frozen:
            raise RuntimeError("registry is frozen; add must precede finalize")
        if "/" in name:
            # Uniform, source-independent rule (maintainer ruling,
            # 2026-07-05): '/' is reserved -- kind/name selectors,
            # per-resource manifest filenames -- so no publisher may
            # register a name containing it. Enforced here rather than
            # per-loader so TOML, YAML, and future plugin publishers
            # cannot drift.
            from agentworks.errors import ConfigError
            from agentworks.resources.render import format_origin_line

            raise ConfigError(
                f"{kind} name {name!r} contains '/', which is not allowed "
                f"in resource names ({format_origin_line(origin)})",
                hint=(
                    "Rename the resource: '/' is reserved for kind/name selectors and per-resource manifest filenames."
                ),
            )
        existing = self._resources.get(kind, {}).get(name)
        if existing is not None:
            self._check_collision(kind, name, existing, origin)
        stamped = dataclasses.replace(resource, origin=origin)
        self._resources.setdefault(kind, {})[name] = stamped

    @staticmethod
    def _check_collision(kind: str, name: str, existing: Any, incoming: Origin) -> None:
        """Raise unless the incoming publish may replace ``existing``.

        Built-in over built-in replaces unconditionally: republish is
        idempotent, and a future app publisher shadowing another's row
        is accepted (the surviving origin's source says who won).
        """
        from agentworks.resources.render import format_origin_line

        existing_origin: Origin | None = getattr(existing, "origin", None)
        existing_variant = getattr(existing_origin, "variant", None)
        if existing_variant == "built-in" and incoming.variant == "built-in":
            return
        if existing_variant == "built-in" and incoming.variant == "operator-declared":
            handler = KIND_REGISTRY.get(kind)
            if handler is not None and handler.builtin_override == "allow":
                return
            raise ConfigError(
                f'{kind} "{name}" is a built-in resource with a reserved '
                f"name; declare a differently-named {kind} instead",
            )
        if existing_variant == "operator-declared" and incoming.variant == "operator-declared":
            raise ConfigError(
                f'duplicate {kind} "{name}": declared at '
                f"{format_origin_line(existing_origin)} and "
                f"{format_origin_line(incoming)}",
                hint=(
                    "Remove one of the two declarations. If one origin is a "
                    "legacy config.toml section, delete that section (or the "
                    "conflicting YAML manifest) to resolve."
                ),
            )
        raise ConfigError(
            f"publisher ordering conflict: {incoming.variant} row published "
            f"over {existing_variant} row for {kind}/{name}"
        )

    # -- Finalize phase ------------------------------------------------

    def finalize(self) -> None:
        """Run the framework pass over published Resources, then freeze.

        The passes over the single retained graph (LLD b), in order:

        0. **reserved-defaults**: always-materialize reserved-default
           names (kinds whose ``auto_declare_names`` is a non-None set)
           so they are present before the walk. Kinds with
           ``auto_declare_names = None`` (secrets) stay reference-driven.
        1. **build**: walk every present Resource's
           ``referenced_resources()`` (total, non-throwing) into the
           outbound edge map. No validation, no throwing on a bad block.
        2. **resolve**: for each edge whose target has no node, dispatch
           the miss policy: ``"error"`` and a disallowed-name
           ``"auto-declare"`` hard-error NOW (ungated); an allowed-name
           ``"auto-declare"`` DEFERS to materialize (pass 5), so a
           not-ready node cannot force it (R12).
        3. **cycle-detect**: three-coloring over the BUILT edge map (no
           re-derivation); raises on the first cycle.
        4. **fold**: reverse-topological readiness (LLD c); each node is
           handed its dependencies' ``DependencyState`` and stores its own
           verdict. Imposes no propagation rule (R4).
        5. **materialize**: synthesize each deferred auto-declare target
           the reference set requires, but ONLY when a READY, ENABLED node
           references it (R12); then walk / resolve / fold the new node,
           looping to a fixpoint (a materialized node's out-edges point
           only back into the settled set; LLD b subtlety 3). Dormant for
           secrets this effort (they emit no ``secret -> secret-backend``
           edges until phase 4), so a materialized secret has no out-edges
           to walk yet.
        6. **attach**: build the retained frozen ``DependencyGraph``
           (inbound references + fold verdicts on the node), then synthesize
           descriptions for auto-declared rows from the graph's inbound refs.
        7. **validate**: run each READY + ENABLED Resource's ``validate()``
           (throwing, ``file:line``-framed). A not-ready or disabled row's
           block is deferred, not validated (R9.4). Distinct from graph
           construction (R3): the build passes never throw on a malformed
           block, so a config with both a malformed block and a cycle
           reports the cycle first (R9.3).
        8. **freeze**.

        Semantic checks that need CONFIG alongside the finalized graph
        (e.g. the secret chain's names and reachability) are not the
        Registry's job -- config isn't published here. They run at the
        boundary that holds both worlds: ``bootstrap.build_registry``
        invokes the owning subsystem's check
        (``secrets.validate_chain``) right after finalize returns.

        First-encountered reference order (for the
        ``Origin.auto_declared(source=...)`` rule) is preserved by
        ``dict``'s guaranteed insertion order: each Resource's edges are
        appended contiguously during its own walk.

        Raises ``RuntimeError`` if already finalized. Raises
        ``ConfigError`` for unresolved references under an error policy,
        disallowed auto-declare names, reserved-name violations, and cycles.
        """
        if self._frozen:
            raise RuntimeError("registry has already been finalized")
        from agentworks.resources.graph import build_graph, fold_readiness

        # 0: reserved-defaults. Seed always-materialize rows so the build
        # walk sees them alongside operator-published ones.
        self._materialize_reserved_defaults()

        # ``all_refs`` accumulates edges keyed by target (for inbound and
        # miss dispatch); ``all_outbound`` keyed by source, each source's
        # edges contiguous, so the graph's outbound preserves
        # first-encountered order (LLD a). Both are filled in one walk.
        all_refs: dict[tuple[str, str], list[ResourceReference]] = {}
        all_outbound: dict[tuple[str, str], list[ResourceReference]] = {}

        # 1: build. Walk every currently-present Resource once (secrets are
        # not present yet, so this is operator + reserved-default rows).
        for kind in list(self._resources.keys()):
            for name in list(self._resources[kind].keys()):
                self._walk_into((kind, name), all_refs, all_outbound)

        # 2: resolve. Classify misses; allowed auto-declares defer to pass 5.
        deferred: set[tuple[str, str]] = set()
        self._resolve_misses(all_refs, deferred)

        # 3: cycle-detect over the built edge map (no re-walk).
        _detect_cycles(all_outbound, self._present_keys())

        # 4: fold readiness for the present nodes (LLD c). ``enablement`` is
        # the opt-in seam: all-enabled today (no producer, R7), overridable so
        # the fold's distribution of a disabled dependency is exercisable and
        # the plugin rebuild has a single place to fill.
        enablement = self._node_enablement()
        readiness = fold_readiness(self._resources, all_outbound, enablement)

        # 5: readiness-gated materialization (R12), looping to a fixpoint.
        self._materialize_deferred(deferred, all_refs, all_outbound, readiness, enablement)

        # 6: attach. Build the retained graph (inbound + readiness + enablement
        # on the node), then polish auto-declared descriptions off its inbound
        # refs.
        self._graph = build_graph(self._resources, all_refs, all_outbound, readiness, enablement)
        for kind in list(self._resources.keys()):
            for name in list(self._resources[kind].keys()):
                existing = self._resources[kind][name]
                inbound = self._graph.dependents_of(kind, name)
                self._resources[kind][name] = _polish_auto_declared_description(existing, kind, inbound)

        # 7: validate the ready + enabled set only (R3, R9.4).
        self._validate_resources(enablement)

        # 8: freeze.
        self._frozen = True

    def _validate_resources(self, enablement: Mapping[tuple[str, str], Enablement]) -> None:
        """Run each READY + ENABLED Resource's ``validate()`` (the throwing
        correctness check for its capability config sub-block), raising on
        the first malformed block.

        Scoped to the READY + ENABLED set (R3, R9.4): a not-ready or disabled
        resource's block is DEFERRED, not validated (there is nothing to run,
        and its capability may be unavailable), so a malformed ``platform_config``
        on a site the host cannot run does not abort every command; it is
        validated when the resource becomes ready. A disabled node's stored
        readiness is a placeholder (enablement answers for it), so the gate
        checks enablement explicitly rather than leaning on ``is_ready``.

        The capability's ``validate`` frames its message with the logical
        owner label (``kind/name``); the source location that decode/load
        used to supply (the manifest ``file:line``, the TOML section) is
        gone once validation runs here, so this pass re-attaches it from
        the Resource's ``origin`` (the same provenance operators see in
        ``describe`` / ``doctor``, rendered location-only for the inline
        message).

        The ``getattr(resource, "validate", None)`` guard skips the
        capability marker rows (``VMPlatformEntry`` and friends), which
        carry no ``validate`` attribute; a ``DeclaredResource`` subclass
        with no capability config (a secret, an apt entry) validates via
        the no-op base ``validate`` and passes.
        """
        from agentworks.resources.graph import Enablement
        from agentworks.resources.render import format_origin_location

        assert self._graph is not None  # built in pass 6, before this pass
        for kind in list(self._resources.keys()):
            for name in list(self._resources[kind].keys()):
                if enablement.get((kind, name), Enablement.enabled) is Enablement.disabled:
                    continue
                if not self._graph.is_ready(kind, name):
                    continue
                resource = self._resources[kind][name]
                validate = getattr(resource, "validate", None)
                if validate is None:
                    continue
                try:
                    validate()
                except ConfigError as exc:
                    origin = getattr(resource, "origin", None)
                    raise ConfigError(
                        f"{exc} ({format_origin_location(origin)})",
                        hint=exc.hint,
                    ) from exc

    def _present_keys(self) -> set[tuple[str, str]]:
        """The ``(kind, name)`` of every currently-published Resource."""
        return {(kind, name) for kind, kind_dict in self._resources.items() for name in kind_dict}

    def _node_enablement(self) -> dict[tuple[str, str], Enablement]:
        """Each present node's enablement (opt-in) axis, the seam the fold and
        graph consume.

        Every node is ``enabled`` this effort: no producer of ``disabled`` nodes
        ships (R7). This is the single, minimal extension point the plugin
        rebuild fills to mark an opted-out unit ``disabled`` (and a test
        overrides to exercise the fold's distribution of a disabled dependency's
        state); nodes materialized after this pass default to ``enabled``.
        """
        from agentworks.resources.graph import Enablement

        return {(kind, name): Enablement.enabled for kind, kind_dict in self._resources.items() for name in kind_dict}

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
        empty-references path. By contract, kinds with
        ``auto_declare_names`` non-None synthesize with
        ``Origin.auto_declared(source=ALWAYS_MATERIALIZE_SOURCE)``
        themselves. The Registry does NOT stamp origin here, distinct
        from ``add``'s stamp-by-the-registry pattern -- the seeded row
        already carries its origin when it reaches this method.

        Called at the start of ``finalize`` before the worklist loop so
        the seeded Resources participate in the reference walk
        alongside operator-published ones.
        """
        for kind, kind_handler in KIND_REGISTRY.items():
            if kind_handler.auto_declare_names is None:
                continue
            for name in kind_handler.auto_declare_names:
                if name in self._resources.get(kind, {}):
                    continue
                self._resources.setdefault(kind, {})[name] = kind_handler.synthesize(())

    def _walk_into(
        self,
        key: tuple[str, str],
        all_refs: dict[tuple[str, str], list[ResourceReference]],
        all_outbound: dict[tuple[str, str], list[ResourceReference]],
    ) -> None:
        """Walk one Resource's ``referenced_resources()``, appending its
        edges into ``all_refs`` (keyed by target) and ``all_outbound``
        (keyed by source). The source of every edge is ``key``, so
        ``all_outbound[key]`` holds this Resource's edges contiguously in
        emission order (LLD a's first-encountered guarantee).
        """
        kind, name = key
        resource = self._resources[kind][name]
        for req in _referenced_resources(resource):
            all_refs.setdefault((req.kind, req.name), []).append(req)
            all_outbound.setdefault(req.source, []).append(req)

    def _resolve_misses(
        self,
        all_refs: dict[tuple[str, str], list[ResourceReference]],
        deferred: set[tuple[str, str]],
    ) -> None:
        """Classify every edge whose target is not (yet) a Resource: an
        ``"error"`` policy or a disallowed ``"auto-declare"`` name is a hard
        error NOW (ungated by any referrer's readiness, LLD b subtlety 1); an
        allowed ``"auto-declare"`` name is added to ``deferred`` for the
        readiness-gated materialize pass (R12). A target already deferred or
        already present is skipped.
        """
        for target, refs in list(all_refs.items()):
            kind, name = target
            if name in self._resources.get(kind, {}) or target in deferred:
                continue
            kind_handler = _lookup_kind(kind, refs[0])
            first = refs[0]
            if kind_handler.miss_policy == "auto-declare":
                allowed = kind_handler.auto_declare_names
                if allowed is not None and name not in allowed:
                    raise ConfigError(
                        f"{kind} kind only auto-declares the reserved name(s) "
                        f"{sorted(allowed)!r}; got {name!r} "
                        f"(required by {first.source[0]}/{first.source[1]})"
                    )
                deferred.add(target)
            elif kind_handler.miss_policy == "error":
                raise ConfigError(
                    f"{first.source[0]} {first.source[1]!r} references unknown {kind} {name!r} ({first.usage})"
                )
            else:
                raise StateError(f"unexpected miss_policy {kind_handler.miss_policy!r} on KIND_REGISTRY[{kind!r}]")

    def _materialize_deferred(
        self,
        deferred: set[tuple[str, str]],
        all_refs: dict[tuple[str, str], list[ResourceReference]],
        all_outbound: dict[tuple[str, str], list[ResourceReference]],
        readiness: dict[tuple[str, str], Readiness],
        enablement: Mapping[tuple[str, str], Enablement],
    ) -> None:
        """Materialize deferred auto-declare targets, readiness-gated (R12),
        looping to a fixpoint (LLD b subtlety 3).

        A deferred target is synthesized ONLY when at least one PRESENT, ENABLED,
        READY node references it (a target referenced solely by not-ready or
        disabled nodes is left unmaterialized, so its would-be secrets never
        enter ``secret list`` / doctor / resolution: the behavior the removed
        vm-site suppression used to achieve). A newly-materialized node is then
        walked (its edges resolved and it folded) so a materialized node that
        itself references others still resolves them. The loop terminates because
        a late-materialized node's out-edges point only back into the settled
        set; a hard cap on iterations guards against a framework bug rather than
        truncating silently.
        """
        from agentworks.resources.graph import Enablement, node_readiness

        def has_ready_referrer(target: tuple[str, str]) -> bool:
            for ref in all_refs.get(target, ()):
                source = ref.source
                verdict = readiness.get(source)
                if verdict is None:
                    # A referrer is always a present, folded node. A missing
                    # verdict means that build/fold invariant broke; fail loud
                    # rather than silently defeating the R12 gate.
                    raise StateError(
                        f"referrer {source!r} of deferred target {target!r} has no readiness verdict; "
                        f"the build/fold invariant is broken (this is a framework bug)"
                    )
                if enablement.get(source, Enablement.enabled) is Enablement.disabled:
                    # A disabled referrer does not drive materialization (R12):
                    # its readiness is not even computed.
                    continue
                if verdict.is_ready:
                    return True
            return False

        cap = sum(len(kind_dict) for kind_dict in self._resources.values()) + len(deferred) + 1
        for _ in range(cap):
            if not deferred:
                return
            progressed = False
            for target in sorted(deferred):
                if not has_ready_referrer(target):
                    continue
                kind, name = target
                refs = all_refs.get(target, [])
                kind_handler = _lookup_kind(kind, refs[0])
                self._resources.setdefault(kind, {})[name] = kind_handler.synthesize(refs)
                deferred.discard(target)
                progressed = True
                # Walk the new node, resolve its misses, then fold it (its
                # deps are already folded, so the reverse-topo invariant holds).
                self._walk_into(target, all_refs, all_outbound)
                self._resolve_misses(all_refs, deferred)
                readiness[target] = node_readiness(target, self._resources, all_outbound, readiness, enablement)
            if not progressed:
                # Every remaining deferred target lacks a ready referrer (R12):
                # leave it unmaterialized.
                return
        raise StateError(
            "readiness-gated materialization did not reach a fixpoint within "
            "the node-count cap; this is a framework bug (a materialized node's "
            "out-edges should point only back into the already-folded set)"
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
        (capability resources). Empty iterator if the kind has
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

    @property
    def graph(self) -> DependencyGraph:
        """The retained ``DependencyGraph``, available only after
        ``finalize``. Raises ``RuntimeError`` if accessed before then
        (the graph is built from the complete reference walk, so there is
        no meaningful pre-finalize graph to return).
        """
        if self._graph is None:
            raise RuntimeError("registry graph is available only after finalize")
        return self._graph


# -- Internal helpers --------------------------------------------------


def _referenced_resources(resource: Any) -> Sequence[ResourceReference]:
    """Return the Resource's ``referenced_resources()`` or an empty
    sequence if it doesn't define one. Not every Resource type defines
    the method, so the ``getattr`` fallback keeps the walk safe.
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
        raise ConfigError(f"{req.source[0]} {req.source[1]!r} references unregistered kind {kind!r}") from None


def _polish_auto_declared_description(
    resource: Any,
    kind: str,
    references: Sequence[ReferenceEntry],
) -> Any:
    """Synthesize a description for an auto-declared Resource when its
    description is empty. Operators rely on a non-empty Description in
    ``agw resource list`` / ``agw secret list``; the framework derives
    one so the row reads as "what this resource is for and who's asking".

    ``references`` is the resource's inbound reference list, read off the
    graph node by the caller (they no longer live on the dataclass).

    Two cases share this polish step:

    - **Usage-driven** (auto-declared via incoming reference): set
      from the first matching reference as
      ``"(auto) <usage> for <kind>/<name>"`` plus ``" (and N more)"``
      when more than one distinct source matches.
    - **Empty-usage** (always-materialized reserved default; no incoming
      references): set as ``"(auto) auto-declared default <kind>"``,
      e.g. ``"(auto) auto-declared default vm-template"``.

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
    if not references:
        # Always-materialized default with no static incoming references.
        description = f"(auto) auto-declared default {kind}"
    else:
        first = references[0]
        # ReferenceEntry.source is typed tuple[str, str]; the framework
        # guarantees the shape at finalize time. No runtime guard.
        distinct_other = {entry.source for entry in references} - {first.source}
        suffix = f" (and {len(distinct_other)} more)" if distinct_other else ""
        description = f"(auto) {first.usage} for {first.source[0]}/{first.source[1]}{suffix}"
    return dataclasses.replace(resource, description=description)


def _detect_cycles(
    all_outbound: dict[tuple[str, str], list[ResourceReference]],
    present: set[tuple[str, str]],
) -> None:
    """Detect cycles over the BUILT edge map via iterative DFS
    three-coloring (no re-walk of ``referenced_resources()``; the finalize
    build pass already accumulated the edges).

    Only edges to present nodes can close a cycle (a deferred / absent target
    has no outbound edges of its own), so the walk follows edges to present
    targets. Secrets don't reference secrets, so they can't form cycles; the
    check exists to guard template-inheritance chains, which can. Implemented
    iteratively so deep inheritance chains don't risk CPython's default
    recursion limit.

    Raises ``ConfigError`` with the cycle path on the first cycle.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[tuple[str, str], int] = {}

    def edges_from(node: tuple[str, str]) -> Iterator[tuple[str, str]]:
        for ref in all_outbound.get(node, ()):
            target = (ref.kind, ref.name)
            if target in present:
                yield target

    for start_node in present:
        if color.get(start_node, WHITE) != WHITE:
            continue

        # Iterative DFS via a work stack. Each frame is a tuple of
        # (node, edge_iterator). When we descend, push the parent
        # frame back with its iterator; when we exhaust the iterator,
        # color the node BLACK and pop.
        color[start_node] = GRAY
        path: list[tuple[str, str]] = [start_node]
        edge_stack: list[Iterator[tuple[str, str]]] = [edges_from(start_node)]
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
                cycle = path[path.index(target) :] + [target]
                cycle_path = " -> ".join(f"{k}/{n}" for k, n in cycle)
                raise ConfigError(f"resource reference cycle detected: {cycle_path}")
            if target_color == BLACK:
                continue
            color[target] = GRAY
            path.append(target)
            edge_stack.append(edges_from(target))
