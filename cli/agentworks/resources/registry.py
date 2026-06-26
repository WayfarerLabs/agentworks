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

        1. Walk every Resource's ``required_resources()`` (if defined;
           Phase 1a has no producers wired beyond what tests synthesize,
           so most Resources return an empty list).
        2. Group requirements by ``(kind, name)`` target, preserving
           first-encountered order for the ``Origin.auto_declared``
           source rule.
        3. For each ``(kind, name)``:
           - If a Resource is already published, attach a ``usage`` list
             built from the matching requirements via
             ``dataclasses.replace``.
           - Otherwise, look up the kind in ``KIND_REGISTRY`` and
             dispatch its ``miss_policy``: ``auto-declare`` (subject to
             ``auto_declare_names``) synthesizes a Resource via the
             kind's ``synthesize``; ``error`` raises ``ConfigError``.
        4. Detect cycles in the now-complete requirement graph via DFS
           three-coloring; raise ``ConfigError`` on the first cycle.
        5. Freeze.

        Raises ``RuntimeError`` if already finalized. Raises
        ``ConfigError`` for unresolved references under an error policy,
        reserved-name violations, and cycles.
        """
        if self._frozen:
            raise RuntimeError("registry has already been finalized")

        # 1 + 2: collect and group.
        by_target: dict[tuple[str, str], list[ResourceRequirement]] = {}
        for kind_dict in self._resources.values():
            for resource in kind_dict.values():
                for req in _required_resources(resource):
                    by_target.setdefault((req.kind, req.name), []).append(req)

        # 3: dispatch.
        for (kind, name), reqs in by_target.items():
            kind_handler = _lookup_kind(kind, reqs[0])
            existing = self._resources.get(kind, {}).get(name)
            if existing is not None:
                stamped = dataclasses.replace(
                    existing, usage=_usage_tuple(reqs)
                )
                self._resources[kind][name] = stamped
            else:
                self._handle_miss(kind, name, kind_handler, reqs)

        # 4: cycle detection across the now-complete graph.
        _detect_cycles(self._resources)

        # 5: freeze.
        self._frozen = True

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
    """Detect cycles across the requirement graph via DFS three-coloring.

    Phase 1 has no producers that introduce cycles (secrets don't
    reference secrets); the check runs for completeness and is the
    backbone of Phase 2's template-inheritance validation. Raises
    ``ConfigError`` with the cycle path on the first cycle.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[tuple[str, str], int] = {}
    stack: list[tuple[str, str]] = []

    def visit(node: tuple[str, str]) -> None:
        color[node] = GRAY
        stack.append(node)
        kind, name = node
        resource = resources.get(kind, {}).get(name)
        if resource is not None:
            for req in _required_resources(resource):
                target = (req.kind, req.name)
                target_color = color.get(target, WHITE)
                if target_color == GRAY:
                    cycle = stack[stack.index(target):] + [target]
                    path = " -> ".join(f"{k}:{n}" for k, n in cycle)
                    raise ConfigError(f"resource reference cycle detected: {path}")
                if target_color == WHITE:
                    visit(target)
        stack.pop()
        color[node] = BLACK

    for kind, kind_dict in resources.items():
        for name in kind_dict:
            node = (kind, name)
            if color.get(node, WHITE) == WHITE:
                visit(node)
