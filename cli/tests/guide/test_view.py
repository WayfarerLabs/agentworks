from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from types import SimpleNamespace

import pytest

from agentworks.guide import (
    BlockId,
    ConceptAnchor,
    GuideBlock,
    GuideInstanceFact,
    GuideRoot,
    GuideTraversalError,
    InstanceList,
    Overview,
    ResourceAnchor,
    TopicContribution,
    TopicSlug,
    build_guide_view,
)
from agentworks.resources import KIND_REGISTRY, Origin, Registry, ResourceReference
from agentworks.resources.graph import Enablement, Readiness
from agentworks.resources.kind import InstanceRef


class _RaisingPower:
    def __call__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("forbidden power was invoked")


class _Graph:
    def readiness_of(self, kind: str, name: str) -> Readiness:
        return Readiness.ready()

    def enablement_of(self, kind: str, name: str) -> Enablement:
        return Enablement.enabled

    def edges_of(self, kind: str, name: str) -> tuple[object, ...]:
        return ()

    def dependents_of(self, kind: str, name: str) -> tuple[object, ...]:
        return ()


class _Registry:
    is_finalized = True
    graph = _Graph()

    def iter_kind_items(self, kind: str):
        return iter((("demo", SimpleNamespace(origin=None, description="Safe.")),))

    def lookup(self, kind: str, name: str) -> object:
        return SimpleNamespace(origin=None, description="Safe.")


class _Handler:
    kind = "guide-test"
    category = "declarable"
    description = "Test kind."


class _NoInstanceHandler:
    kind = "guide-test"
    category = "declarable"
    description = "Test kind."


def _walk(value: object) -> list[object]:
    result = [value]
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            result.extend(_walk(getattr(value, field.name)))
    elif isinstance(value, tuple):
        for item in value:
            result.extend(_walk(item))
    return result


def _deny_production_powers(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.output as output
    import agentworks.secrets.resolve as secret_resolve
    import agentworks.transports as transports
    from agentworks.capabilities.vm_platform.lima import LimaPlatform
    from agentworks.db import Database

    denied = _RaisingPower()
    monkeypatch.setattr(secret_resolve, "resolve_secrets", denied)
    monkeypatch.setattr(transports, "transport", denied)
    monkeypatch.setattr(output, "prompt", denied)
    monkeypatch.setattr(output, "prompt_secret", denied)
    monkeypatch.setattr(Database, "insert_vm", denied)
    monkeypatch.setattr(LimaPlatform, "start", denied)
    monkeypatch.setattr(LimaPlatform, "not_ready", denied)


def _topic_for(anchor: ConceptAnchor | ResourceAnchor, *, inventory: bool = False) -> TopicContribution:
    slug = anchor.name if isinstance(anchor, ConceptAnchor) else f"{anchor.kind}/{anchor.name}"
    blocks: tuple[GuideBlock, ...] = (Overview(BlockId("overview"), "Text."),)
    if inventory:
        blocks += (InstanceList(BlockId("inventory")),)
    return TopicContribution(TopicSlug(slug), "Title", "Summary.", anchor, blocks)


def test_concept_view_copies_facts_without_invoking_forbidden_powers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(KIND_REGISTRY, "guide-test", _Handler())
    _deny_production_powers(monkeypatch)
    registry = _Registry()
    db = SimpleNamespace()

    view = build_guide_view(_topic_for(ConceptAnchor("concept-onboarding"), inventory=True), registry, db)  # type: ignore[arg-type]
    facts = view.inventory(GuideRoot.KINDS) + view.inventory(GuideRoot.IMPLEMENTATIONS)
    denied = (type(registry), _Graph, _RaisingPower, type(db))
    assert not any(isinstance(item, denied) for item in _walk(facts))
    assert {name for name in dir(view) if not name.startswith("_")} == {
        "inbound",
        "instances",
        "inventory",
        "kind",
        "me",
        "outbound",
    }
    with pytest.raises(AttributeError):
        view._me = None  # type: ignore[misc]


def test_anchor_enforces_traversal_and_never_finalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(KIND_REGISTRY, "guide-test", _NoInstanceHandler())
    view = build_guide_view(_topic_for(ResourceAnchor("guide-test", "demo")), _Registry(), SimpleNamespace())  # type: ignore[arg-type]
    assert view.me().identity.name == "demo"
    with pytest.raises(GuideTraversalError):
        view.inventory(GuideRoot.KINDS)


def test_each_concept_receives_only_planned_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(KIND_REGISTRY, "guide-test", _NoInstanceHandler())
    view = build_guide_view(
        _topic_for(ConceptAnchor("concept-secrets"), inventory=True), _Registry(), SimpleNamespace()
    )  # type: ignore[arg-type]
    assert view.inventory(GuideRoot.IMPLEMENTATIONS)
    with pytest.raises(GuideTraversalError):
        view.inventory(GuideRoot.KINDS)


def test_concept_roots_must_match_validated_block_plan() -> None:
    topic = TopicContribution(
        TopicSlug("concept-secrets"),
        "Title",
        "Summary.",
        ConceptAnchor("concept-secrets"),
        (Overview(BlockId("overview"), "Text."),),
    )
    with pytest.raises(GuideTraversalError):
        build_guide_view(topic, _Registry(), SimpleNamespace())  # type: ignore[arg-type]


def test_view_refuses_registry_before_finalization() -> None:
    registry = _Registry()
    registry.is_finalized = False
    with pytest.raises(GuideTraversalError, match="already-finalized registry"):
        build_guide_view(
            _topic_for(ResourceAnchor("vm-template", "demo")),
            registry,  # type: ignore[arg-type]
            SimpleNamespace(),
        )


def test_missing_anchor_resource_becomes_a_typed_traversal_failure() -> None:
    class MissingRegistry(_Registry):
        def lookup(self, kind: str, name: str) -> object:
            raise KeyError((kind, name))

    with pytest.raises(GuideTraversalError, match="guide resource vm-template/missing") as raised:
        build_guide_view(
            _topic_for(ResourceAnchor("vm-template", "missing")),
            MissingRegistry(),  # type: ignore[arg-type]
            SimpleNamespace(),
        )
    assert raised.value.__suppress_context__


def test_non_lookup_key_error_is_not_translated_to_a_missing_resource() -> None:
    class BrokenGraph(_Graph):
        def readiness_of(self, kind: str, name: str) -> Readiness:
            raise KeyError("graph invariant")

    class BrokenRegistry(_Registry):
        graph = BrokenGraph()

    with pytest.raises(KeyError, match="graph invariant"):
        build_guide_view(
            _topic_for(ResourceAnchor("vm-template", "demo")),
            BrokenRegistry(),  # type: ignore[arg-type]
            SimpleNamespace(),
        )


def test_real_finalized_registry_relationships_and_instance_hook_are_eager(monkeypatch: pytest.MonkeyPatch) -> None:
    exhausted = False

    class Handler(_NoInstanceHandler):
        miss_policy = "error"
        auto_declare_names: frozenset[str] = frozenset()
        builtin_override = "allow"

        def instances(self, db: object, registry: Registry, resource: object):
            nonlocal exhausted
            yield InstanceRef("vm", "zeta")
            yield InstanceRef("vm", "alpha")
            exhausted = True

    @dataclass(frozen=True)
    class Node:
        reqs: tuple[ResourceReference, ...] = ()
        origin: Origin | None = None

        def dependencies(self, context: object) -> tuple[ResourceReference, ...]:
            return self.reqs

    monkeypatch.setitem(KIND_REGISTRY, "guide-test", Handler())
    registry = Registry.empty()
    origin = Origin.built_in(source="test")
    registry.add(
        "guide-test",
        "a",
        Node((ResourceReference("b", "guide-test", "a uses b", ("guide-test", "a")),)),
        origin,
    )
    registry.add("guide-test", "b", Node(), origin)
    registry.finalize()
    _deny_production_powers(monkeypatch)
    monkeypatch.setattr(registry, "finalize", _RaisingPower())
    view = build_guide_view(_topic_for(ResourceAnchor("guide-test", "a")), registry, SimpleNamespace())  # type: ignore[arg-type]
    assert view.instances() == (GuideInstanceFact("vm", "alpha"), GuideInstanceFact("vm", "zeta"))
    assert exhausted
    assert [(edge.source.name, edge.target.name, edge.usage) for edge in view.outbound()] == [("a", "b", "a uses b")]
    public_results = (view.me(), view.kind(), view.instances(), view.inbound(), view.outbound())
    forbidden = (Registry, Handler, Node)
    assert not any(isinstance(item, forbidden) or callable(item) for result in public_results for item in _walk(result))
