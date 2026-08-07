"""Deny-by-construction projection of finalized registry facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal, cast

from agentworks.guide.contract import (
    ConceptAnchor,
    GuideTraversalError,
    ImplementationAnchor,
    InstanceList,
    KindAnchor,
    ResourceAnchor,
    TopicContribution,
    parse_topic_contribution,
)
from agentworks.resources import KIND_REGISTRY
from agentworks.resources.graph import Enablement

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.resources import Origin, Registry
    from agentworks.resources.kind import ResourceKind


class GuideRoot(Enum):
    KINDS = "kinds"
    IMPLEMENTATIONS = "implementations"


@dataclass(frozen=True, slots=True)
class GuideIdentity:
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class GuideOrigin:
    variant: Literal["operator-declared", "built-in", "auto-declared", "system-plugin"]
    plugin: str | None


@dataclass(frozen=True, slots=True)
class GuideVerdict:
    enabled: bool
    ready: bool
    reason: str | None
    is_available: bool = True


@dataclass(frozen=True, slots=True)
class GuideResourceFact:
    identity: GuideIdentity
    category: Literal["declarable", "capability"]
    description: str | None
    origin: GuideOrigin
    verdict: GuideVerdict


@dataclass(frozen=True, slots=True)
class GuideRelationship:
    source: GuideIdentity
    target: GuideIdentity
    usage: str


@dataclass(frozen=True, slots=True)
class GuideInstanceFact:
    kind: str
    name: str


_CONSTRUCTION_TOKEN = object()
_CONCEPT_RESOLVER_ROOTS: dict[str, frozenset[GuideRoot]] = {
    "concept-onboarding": frozenset({GuideRoot.KINDS, GuideRoot.IMPLEMENTATIONS}),
    "concept-secrets": frozenset({GuideRoot.IMPLEMENTATIONS}),
}


class GuideView:
    """A frozen fact snapshot exposing only anchor-permitted traversals."""

    __slots__ = ("_inbound", "_instances", "_inventory", "_kind", "_me", "_outbound", "_permitted_roots", "_sealed")

    _me: GuideResourceFact | None
    _kind: GuideResourceFact | None
    _instances: tuple[GuideInstanceFact, ...]
    _inbound: tuple[GuideRelationship, ...]
    _outbound: tuple[GuideRelationship, ...]
    _inventory: tuple[tuple[GuideRoot, tuple[GuideResourceFact, ...]], ...]
    _permitted_roots: frozenset[GuideRoot]
    _sealed: bool

    def __init__(
        self,
        token: object,
        *,
        me: GuideResourceFact | None,
        kind: GuideResourceFact | None,
        instances: tuple[GuideInstanceFact, ...],
        inbound: tuple[GuideRelationship, ...],
        outbound: tuple[GuideRelationship, ...],
        inventory: dict[GuideRoot, tuple[GuideResourceFact, ...]],
        permitted_roots: frozenset[GuideRoot],
    ) -> None:
        if token is not _CONSTRUCTION_TOKEN:
            raise TypeError("GuideView must be built with build_guide_view")
        object.__setattr__(self, "_me", me)
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_instances", instances)
        object.__setattr__(self, "_inbound", inbound)
        object.__setattr__(self, "_outbound", outbound)
        object.__setattr__(self, "_inventory", tuple((root, tuple(facts)) for root, facts in inventory.items()))
        object.__setattr__(self, "_permitted_roots", permitted_roots)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("GuideView is immutable")
        object.__setattr__(self, name, value)

    def me(self) -> GuideResourceFact:
        if self._me is None:
            raise GuideTraversalError("this guide topic has no me resource")
        return self._me

    def kind(self) -> GuideResourceFact:
        if self._kind is None:
            raise GuideTraversalError("this guide topic has no kind")
        return self._kind

    def instances(self) -> tuple[GuideInstanceFact, ...]:
        return self._instances

    def inbound(self) -> tuple[GuideRelationship, ...]:
        if self._me is None:
            raise GuideTraversalError("this guide topic has no inbound relationships")
        return self._inbound

    def outbound(self) -> tuple[GuideRelationship, ...]:
        if self._me is None:
            raise GuideTraversalError("this guide topic has no outbound relationships")
        return self._outbound

    def inventory(self, root: GuideRoot) -> tuple[GuideResourceFact, ...]:
        if root not in self._permitted_roots:
            raise GuideTraversalError(f"the {root.value} root is not available to this guide topic")
        return dict(self._inventory)[root]


def _origin(origin: Origin | None) -> GuideOrigin:
    if origin is None:
        return GuideOrigin("built-in", None)
    return GuideOrigin(origin.variant, origin.plugin)


def _kind_fact(kind: str, handler: ResourceKind) -> GuideResourceFact:
    return GuideResourceFact(
        GuideIdentity(kind, kind),
        handler.category,
        handler.description,
        GuideOrigin("built-in", None),
        GuideVerdict(True, True, None),
    )


def _resource_fact(registry: Registry, kind: str, name: str, resource: object) -> GuideResourceFact:
    handler = KIND_REGISTRY[kind]
    readiness = registry.graph.readiness_of(kind, name)
    return GuideResourceFact(
        GuideIdentity(kind, name),
        handler.category,
        cast("str | None", getattr(resource, "description", None)),
        _origin(getattr(resource, "origin", None)),
        GuideVerdict(
            registry.graph.enablement_of(kind, name) is Enablement.enabled,
            readiness.is_ready,
            readiness.reason,
            is_available=readiness.is_available,
        ),
    )


def build_guide_view(contribution: TopicContribution, registry: Registry, db: Database) -> GuideView:
    """Eagerly copy permitted facts from an already-finalized registry."""
    validated = parse_topic_contribution(contribution, "guide-view")
    anchor = validated.anchor
    if not registry.is_finalized:
        raise GuideTraversalError("guide facts require an already-finalized registry")

    kinds = tuple(_kind_fact(kind, handler) for kind, handler in sorted(KIND_REGISTRY.items()))
    implementations = tuple(
        _resource_fact(registry, kind, name, resource)
        for kind, handler in sorted(KIND_REGISTRY.items())
        if handler.category == "capability"
        for name, resource in sorted(registry.iter_kind_items(kind))
    )
    inventory = {GuideRoot.KINDS: kinds, GuideRoot.IMPLEMENTATIONS: implementations}
    me: GuideResourceFact | None = None
    kind_fact: GuideResourceFact | None = None
    instances: tuple[GuideInstanceFact, ...] = ()
    inbound: tuple[GuideRelationship, ...] = ()
    outbound: tuple[GuideRelationship, ...] = ()
    permitted: frozenset[GuideRoot] = frozenset()
    if isinstance(anchor, ConceptAnchor):
        has_inventory = any(isinstance(block, InstanceList) for block in validated.blocks)
        permitted = _CONCEPT_RESOLVER_ROOTS.get(anchor.name, frozenset())
        if has_inventory != bool(permitted):
            raise GuideTraversalError(
                f"concept topic {anchor.name!r} does not match a registered inventory resolver plan"
            )

    if isinstance(anchor, KindAnchor):
        handler = KIND_REGISTRY.get(anchor.kind)
        if handler is None:
            raise GuideTraversalError(f"unknown resource kind {anchor.kind!r}")
        kind_fact = _kind_fact(anchor.kind, handler)
        me = kind_fact
        instances = tuple(
            GuideInstanceFact(anchor.kind, name) for name, _ in sorted(registry.iter_kind_items(anchor.kind))
        )
    elif isinstance(anchor, (ResourceAnchor, ImplementationAnchor)):
        handler = KIND_REGISTRY.get(anchor.kind)
        if handler is None:
            raise GuideTraversalError(f"unknown resource kind {anchor.kind!r}")
        if (
            isinstance(anchor, ResourceAnchor)
            and handler.category != "declarable"
            or isinstance(anchor, ImplementationAnchor)
            and handler.category != "capability"
        ):
            raise GuideTraversalError("guide anchor category does not match its registered kind")
        try:
            resource = registry.lookup(anchor.kind, anchor.name)
        except KeyError:
            raise GuideTraversalError(
                f"guide resource {anchor.kind}/{anchor.name} is absent from the finalized registry"
            ) from None
        me = _resource_fact(registry, anchor.kind, anchor.name, resource)
        kind_fact = _kind_fact(anchor.kind, handler)
        hook = getattr(handler, "instances", None)
        if hook is not None:
            instances = tuple(
                sorted(
                    (GuideInstanceFact(ref.instance_kind, ref.instance_name) for ref in hook(db, registry, resource)),
                    key=lambda item: (item.kind, item.name),
                )
            )
        outbound = tuple(
            GuideRelationship(me.identity, GuideIdentity(ref.kind, ref.name), ref.usage)
            for ref in registry.graph.edges_of(anchor.kind, anchor.name)
        )
        inbound = tuple(
            GuideRelationship(GuideIdentity(*entry.source), me.identity, entry.usage)
            for entry in registry.graph.dependents_of(anchor.kind, anchor.name)
        )

    return GuideView(
        _CONSTRUCTION_TOKEN,
        me=me,
        kind=kind_fact,
        instances=instances,
        inbound=inbound,
        outbound=outbound,
        inventory=inventory,
        permitted_roots=permitted,
    )
