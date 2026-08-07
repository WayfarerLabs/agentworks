from __future__ import annotations

from typing import Any, cast

import pytest

from agentworks.guide import (
    ActionId,
    ActionInput,
    BlockId,
    BrokenTopicLinkError,
    ConceptAnchor,
    ConsentBoundary,
    DuplicateTopicError,
    GuideAction,
    GuideCatalog,
    GuideContributionError,
    InvalidBlockError,
    Overview,
    TopicContribution,
    TopicSlug,
    parse_topic_contribution,
    validate_guide_action,
)
from agentworks.guide.catalog import _build_guide_catalog
from agentworks.plugins.base import Plugin
from agentworks.resources import KIND_REGISTRY


def _topic(slug: str, *, related: list[str] | None = None, markdown: object = "Text.") -> dict[str, object]:
    anchor: dict[str, str]
    if slug.startswith(("concept-", "plugin/")):
        anchor = {"type": "concept", "name": slug}
    elif "/" in slug:
        kind, name = slug.split("/", 1)
        anchor = {"type": "resource", "kind": kind, "name": name}
    else:
        anchor = {"type": "kind", "kind": slug}
    return {
        "topic": slug,
        "title": "Title",
        "summary": "Summary.",
        "anchor": anchor,
        "blocks": [{"type": "overview", "id": "overview", "markdown": markdown}],
        "related_topics": related or [],
    }


@pytest.mark.parametrize("payload", ["{{danger()}}", "}}", "${secret}", "<% run %>", "%>", "{% include x %}", "%}"])
def test_expression_payloads_are_rejected_without_execution(payload: str) -> None:
    called = False

    def danger() -> str:
        nonlocal called
        called = True
        return "ran"

    value = _topic("concept-safe", markdown=payload)
    with pytest.raises(InvalidBlockError) as raised:
        parse_topic_contribution(value, "plugin:bad")
    assert raised.value.field_path == "blocks[0].markdown"
    assert payload not in str(raised.value)
    assert not called
    with pytest.raises(GuideContributionError):
        parse_topic_contribution(_topic("concept-safe", markdown=danger), "plugin:bad")
    assert not called


@pytest.mark.parametrize("executable", [lambda: None, str, object()])
def test_executable_or_object_values_are_rejected(executable: object) -> None:
    with pytest.raises(GuideContributionError):
        parse_topic_contribution(_topic("concept-safe", markdown=executable), "source")


def test_unknown_nested_field_and_duplicate_block_are_rejected() -> None:
    value = _topic("concept-safe")
    blocks = value["blocks"]
    assert isinstance(blocks, list)
    blocks[0]["renderer"] = "eval"  # type: ignore[index]
    with pytest.raises(GuideContributionError) as raised:
        parse_topic_contribution(value, "source")
    assert raised.value.field_path == "blocks[0].renderer"

    value = _topic("concept-safe")
    value["blocks"] = [value["blocks"][0], value["blocks"][0]]  # type: ignore[index]
    with pytest.raises(InvalidBlockError):
        parse_topic_contribution(value, "source")


def test_programmatic_records_are_revalidated_and_copied() -> None:
    original = TopicContribution(
        TopicSlug("concept-safe"),
        "Safe",
        "Summary.",
        ConceptAnchor("concept-safe"),
        (Overview(BlockId("overview"), "Text."),),
    )
    parsed = parse_topic_contribution(original, "core")
    assert parsed == original
    assert parsed is not original


@pytest.mark.parametrize("field", ["topic", "title", "summary", "anchor", "blocks", "related_topics"])
def test_malformed_programmatic_records_fail_with_typed_errors_without_execution(field: str) -> None:
    called = False

    def payload() -> None:
        nonlocal called
        called = True

    original = TopicContribution(
        TopicSlug("concept-safe"),
        "Safe",
        "Summary.",
        ConceptAnchor("concept-safe"),
        (Overview(BlockId("overview"), "Text."),),
    )
    object.__setattr__(original, field, payload)
    with pytest.raises(GuideContributionError):
        parse_topic_contribution(original, "core")
    assert not called


def test_plugin_cannot_hide_reserved_core_topic() -> None:
    catalog = _build_guide_catalog(
        (("core", _topic("concept-safe")),),
        ((Plugin("z"), (_topic("concept-safe"),)),),
    )
    assert catalog.names() == ("concept-safe",)
    assert [(issue.error.source, issue.error.field_path) for issue in catalog.issues] == [("system-plugin:z", "topic")]


def test_trusted_duplicate_hard_fails_independent_of_order() -> None:
    candidates = (("core:b", _topic("concept-safe")), ("core:a", _topic("concept-safe")))
    for ordered in (candidates, tuple(reversed(candidates))):
        with pytest.raises(DuplicateTopicError) as raised:
            _build_guide_catalog(ordered)
        assert raised.value.source == "core:a"


def test_plugin_collision_and_broken_link_isolation_are_deterministic() -> None:
    plugin_topics = (
        _topic("plugin/z/shared"),
        _topic("plugin/z/shared"),
        _topic("plugin/z/broken", related=["plugin/z/missing"]),
    )
    catalogs = [
        _build_guide_catalog((("core", _topic("concept-safe")),), ((Plugin("z"), order),))
        for order in (plugin_topics, tuple(reversed(plugin_topics)))
    ]
    assert [catalog.names() for catalog in catalogs] == [("concept-safe",), ("concept-safe",)]

    def issue_shapes(catalog: GuideCatalog) -> list[tuple[str, str | None, str]]:
        return [(issue.error.source, issue.error.topic, issue.error.field_path) for issue in catalog.issues]

    expected = [
        ("system-plugin:z", "plugin/z/broken", "related_topics"),
        ("system-plugin:z", "plugin/z/shared", "topic"),
        ("system-plugin:z", "plugin/z/shared", "topic"),
    ]
    assert issue_shapes(catalogs[0]) == issue_shapes(catalogs[1]) == expected
    assert isinstance(catalogs[0].issues[0].error, BrokenTopicLinkError)
    assert all(isinstance(issue.error, DuplicateTopicError) for issue in catalogs[0].issues[1:])


class _TestKind:
    kind = "guide-test"
    miss_policy = "error"
    auto_declare_names = None
    category = "declarable"
    description = "Test guide kind."
    builtin_override = "allow"

    def synthesize(self, references: object) -> object:
        raise AssertionError("not used")


def test_plugin_ownership_gate_rejects_another_plugin_namespace() -> None:
    catalog = _build_guide_catalog((), ((Plugin("z"), (_topic("plugin/y/topic"),)),))
    assert catalog.names() == ()
    assert [(issue.error.topic, issue.error.field_path) for issue in catalog.issues] == [("plugin/y/topic", "topic")]


def test_taxonomy_gate_is_ci_fatal_for_trusted_content_and_fail_soft_for_plugin_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _TestKind()
    monkeypatch.setitem(KIND_REGISTRY, handler.kind, handler)
    topic = _topic("guide-test/demo")
    object.__setattr__(handler, "category", "capability")

    with pytest.raises(GuideContributionError) as raised:
        _build_guide_catalog((("core:bad-taxonomy", topic),))
    assert raised.value.field_path == "anchor"

    catalog = _build_guide_catalog(
        (),
        ((Plugin("z"), (topic,)),),
        (("z", "guide-test", "demo"),),
    )
    assert catalog.names() == ()
    assert [(issue.error.topic, issue.error.field_path) for issue in catalog.issues] == [("guide-test/demo", "anchor")]


def test_registered_plugin_implementation_and_owner_adapter_resource_topics_are_accepted() -> None:
    from agentworks.plugins import SYSTEM_PLUGINS

    plugin = SYSTEM_PLUGINS["onepassword"]
    kind, implementations = next(iter(plugin.capabilities.items()))
    implementation = cast("Any", implementations[0])
    implementation_name = implementation.name
    impl_topic = _topic(f"{kind}/{implementation_name}")
    impl_topic["anchor"] = {"type": "implementation", "kind": kind, "name": implementation_name}
    resource_topic = _topic("vm-template/plugin-owned")
    catalog = _build_guide_catalog(
        (),
        ((plugin, (impl_topic, resource_topic)),),
        ((plugin.name, "vm-template", "plugin-owned"),),
    )
    assert catalog.names() == (f"{kind}/{implementation_name}", "vm-template/plugin-owned")


def test_trusted_broken_link_hard_fails() -> None:
    with pytest.raises(BrokenTopicLinkError):
        _build_guide_catalog((("core", _topic("concept-safe", related=["missing"])),))


def _action() -> GuideAction:
    return GuideAction(
        ActionId("verify"),
        "Needs verification.",
        (ActionInput("NAME", "Resource name.", True),),
        ConsentBoundary.READ_CONFIGURED_STATE,
        ("agw", "resource", "$NAME"),
        "Verified.",
        ("agw", "resource", "$NAME"),
        "Inspect manually.",
    )


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("id", object()),
        ("precondition", lambda: None),
        ("required_inputs", [ActionInput("NAME", "Name.", True)]),
        ("consent", "read-configured-state"),
        ("command", ["agw"]),
        ("expected_state", object()),
        ("verification", ["agw"]),
        ("refusal_alternative", object()),
    ],
)
def test_action_validation_is_closed_and_typed(field: str, bad: object) -> None:
    action = _action()
    object.__setattr__(action, field, bad)
    with pytest.raises(GuideContributionError):
        validate_guide_action(action, "core")


def test_action_validation_deep_copies_and_normalizes() -> None:
    original = _action()
    validated = validate_guide_action(original, "core")
    assert validated == original
    assert validated is not original
    assert validated.required_inputs[0] is not original.required_inputs[0]


@pytest.mark.parametrize(
    ("field", "bad"),
    [("name", object()), ("description", lambda: None), ("required", 1), ("sensitive", "no")],
)
def test_action_nested_input_validation_is_exact(field: str, bad: object) -> None:
    action = _action()
    object.__setattr__(action.required_inputs[0], field, bad)
    with pytest.raises(GuideContributionError):
        validate_guide_action(action, "core")


@pytest.mark.parametrize(
    "token",
    ["a;b", "a|b", "a&b", "a>b", "a<b", "a'b", 'a"b', "a\\b", "a b", "a\nb", "a\x1bb"],
)
@pytest.mark.parametrize("field", ["command", "verification"])
def test_action_tokens_reject_shell_syntax_whitespace_and_controls_anywhere(field: str, token: str) -> None:
    action = _action()
    object.__setattr__(action, field, ("agw", token))
    with pytest.raises(GuideContributionError):
        validate_guide_action(action, "core")


@pytest.mark.parametrize("verification", [(), ("&&",), ("${NAME}",), ("$UNKNOWN",)])
def test_action_verification_uses_literal_argv_vocabulary(verification: tuple[str, ...]) -> None:
    action = _action()
    object.__setattr__(action, "verification", verification)
    with pytest.raises(GuideContributionError):
        validate_guide_action(action, "core")


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("title", "x" * 257, "title"),
        ("summary", "x" * 2049, "summary"),
        ("blocks", [{"type": "overview", "id": "overview", "markdown": "x" * (64 * 1024 + 1)}], "blocks[0].markdown"),
        ("blocks", [{"type": "overview", "id": f"block-{index}", "markdown": "x"} for index in range(65)], "blocks"),
        ("related_topics", [f"concept-related-{index}" for index in range(65)], "related_topics"),
    ],
)
def test_contribution_volume_bounds_fail_closed(field: str, value: object, path: str) -> None:
    topic = _topic("concept-safe")
    topic[field] = value
    with pytest.raises(GuideContributionError) as raised:
        parse_topic_contribution(topic, "plugin:z")
    assert raised.value.field_path == path


def test_contribution_total_markdown_volume_is_bounded() -> None:
    topic = _topic("concept-safe")
    topic["blocks"] = [
        {"type": "overview", "id": f"block-{index}", "markdown": "x" * (60 * 1024)} for index in range(5)
    ]
    with pytest.raises(InvalidBlockError) as raised:
        parse_topic_contribution(topic, "plugin:z")
    assert raised.value.field_path == "blocks"
