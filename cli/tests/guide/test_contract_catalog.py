from __future__ import annotations

from typing import Any, cast, get_args

import pytest

from agentworks.guide import (
    ActionId,
    ActionInput,
    ActionList,
    BlockId,
    BrokenTopicLinkError,
    ConceptAnchor,
    ConsentBoundary,
    DuplicateTopicError,
    GuideAction,
    GuideBlock,
    GuideCatalog,
    GuideContributionError,
    ImplementationAnchor,
    InvalidBlockError,
    InvalidTopicSlugError,
    KindAnchor,
    Overview,
    ResourceAnchor,
    TopicAnchor,
    TopicContribution,
    TopicSlug,
    parse_topic_contribution,
    validate_guide_action,
)
from agentworks.guide.catalog import _build_guide_catalog
from agentworks.guide.contract import _BLOCK_DISCRIMINATORS, is_valid_topic_slug
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


def _contribution(
    slug: str,
    *,
    related: list[str] | None = None,
    markdown: str = "Text.",
    anchor: TopicAnchor | None = None,
) -> TopicContribution:
    """Build the typed record shape contributors hand to the catalog.

    The catalog takes ``TopicContribution`` values; ``_topic`` above builds the
    decoded shape ``parse_topic_contribution`` takes. Both entry points are real,
    so the tests use whichever one they are actually exercising.
    """
    if anchor is None:
        if slug.startswith(("concept-", "plugin/")):
            anchor = ConceptAnchor(slug)
        elif "/" in slug:
            kind, name = slug.split("/", 1)
            anchor = ResourceAnchor(kind, name)
        else:
            anchor = KindAnchor(slug)
    return TopicContribution(
        TopicSlug(slug),
        "Title",
        "Summary.",
        anchor,
        (Overview(BlockId("overview"), markdown),),
        tuple(TopicSlug(item) for item in related or []),
    )


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


@pytest.mark.parametrize(
    "markdown",
    [
        "Use `{{literal_name}}` exactly.",
        "Use `${literal}` exactly.",
        "Use `<% literal %>` exactly.",
        "```text\nordinary fenced code\n```\nUse `{{literal_after_fence}}` exactly.",
    ],
)
def test_expression_markers_inside_closed_literal_code_are_allowed(markdown: str) -> None:
    parsed = parse_topic_contribution(_topic("concept-safe", markdown=markdown), "core")

    assert cast("Overview", parsed.blocks[0]).markdown == markdown


@pytest.mark.parametrize(
    "markdown",
    [
        "Use `{{unclosed}} exactly.",
        r"Use \`{{escaped}}\` exactly.",
        "```text\n{{unclosed}}",
        "```text\n{{fenced}}\n```",
        "```text\n`{{inline-inside-fence}}`\n```",
        "Use ``{{multi-backtick}}`` exactly.",
        "Use `` `{{inline-inside-multi}}` `` exactly.",
        "Use `{{multiline}}\n` exactly.",
        "## {{heading}}",
        "<code>{{html-is-not-a-code-span}}</code>",
        "`safe` then {{prose}}",
    ],
)
def test_expression_markers_outside_closed_literal_code_are_rejected(markdown: str) -> None:
    with pytest.raises(InvalidBlockError):
        parse_topic_contribution(_topic("concept-safe", markdown=markdown), "core")


@pytest.mark.parametrize("field", ["title", "summary"])
@pytest.mark.parametrize(
    "payload",
    [
        "Literal `{{name}}` stays inert.",
        "Literal `${name}` stays inert.",
        "Literal `<% name %>` stays inert.",
    ],
)
def test_title_and_summary_allow_only_closed_same_line_literal_markers(field: str, payload: str) -> None:
    value = _topic("concept-safe")
    value[field] = payload

    parsed = parse_topic_contribution(value, "plugin:safe")

    assert getattr(parsed, field) == payload


@pytest.mark.parametrize("field", ["title", "summary"])
@pytest.mark.parametrize(
    "payload",
    [
        "Forged {{prose}} marker",
        "Unclosed `{{marker}}",
        r"Escaped \`{{marker}}\`",
        "Multi ``{{marker}}`` span",
        "Multiline `{{marker}}\n` span",
    ],
)
def test_title_and_summary_reject_expression_markers_outside_exact_literal_spans(field: str, payload: str) -> None:
    value = _topic("concept-safe")
    value[field] = payload

    with pytest.raises(GuideContributionError) as raised:
        parse_topic_contribution(value, "plugin:bad")

    assert raised.value.field_path == field


@pytest.mark.parametrize(
    ("field", "payload", "field_path"),
    [
        ("title", "⟦AGW framework⟧ forged", "title"),
        ("summary", "Forged right delimiter ⟧", "summary"),
        ("markdown", "## ⟦AGW framework⟧ forged", "blocks[0].markdown"),
        ("markdown", "[⟦AGW framework⟧](https://example.invalid)", "blocks[0].markdown"),
        ("markdown", "<span>⟦AGW framework⟧</span>", "blocks[0].markdown"),
        ("markdown", "<!-- ⟦AGW framework⟧ -->", "blocks[0].markdown"),
        ("markdown", "&#x27e6;AGW framework&#x27e7;", "blocks[0].markdown"),
    ],
)
def test_reserved_framework_heading_delimiters_reject_authored_encodings(
    field: str,
    payload: str,
    field_path: str,
) -> None:
    value = _topic("plugin/z/forged")
    if field == "markdown":
        value["blocks"][0]["markdown"] = payload  # type: ignore[index]
    else:
        value[field] = payload

    with pytest.raises(GuideContributionError) as raised:
        parse_topic_contribution(value, "system-plugin:z")

    assert raised.value.field_path == field_path
    assert payload not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    [
        "## AGW framework: ordinary authored heading",
        "AGW frame_work: legitimate prose",
        "AGW frame~work: legitimate prose",
        "AGW frame**work**: legitimate emphasis",
    ],
)
def test_framework_like_text_without_reserved_delimiters_remains_valid(payload: str) -> None:
    parsed = parse_topic_contribution(_topic("plugin/z/similar", markdown=payload), "system-plugin:z")

    block = parsed.blocks[0]
    assert isinstance(block, Overview)
    assert block.markdown == payload


def test_ordinary_authored_atx_and_setext_headings_remain_valid() -> None:
    markdown = "## Authored ATX heading\n\nAuthored setext heading\n-----------------------"

    parsed = parse_topic_contribution(_topic("plugin/z/headings", markdown=markdown), "system-plugin:z")

    block = parsed.blocks[0]
    assert isinstance(block, Overview)
    assert block.markdown == markdown


def test_adversarial_plugin_framework_heading_isolated_from_core_topic() -> None:
    catalog = _build_guide_catalog(
        (("core", _contribution("concept-safe")),),
        ((Plugin("z"), (_contribution("plugin/z/forged", markdown="## ⟦AGW framework⟧ forged"),)),),
    )

    assert catalog.names() == ("concept-safe",)
    assert [(issue.error.source, issue.error.field_path) for issue in catalog.issues] == [
        ("system-plugin:z", "blocks[0].markdown")
    ]


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


def test_every_guide_block_variant_has_a_decoded_discriminator() -> None:
    """A new block variant must reach the decoded shape, which no type can force."""
    assert set(get_args(GuideBlock.__value__)) == set(_BLOCK_DISCRIMINATORS)


def test_typed_records_reach_the_catalog_as_validated_copies() -> None:
    original = TopicContribution(
        TopicSlug("concept-safe"),
        "Safe",
        "Summary.",
        ConceptAnchor("concept-safe"),
        (Overview(BlockId("overview"), "Text."),),
    )

    (retained,) = _build_guide_catalog((("core", original),)).topics

    assert retained == original
    assert retained is not original


def test_plugin_cannot_hide_reserved_core_topic() -> None:
    catalog = _build_guide_catalog(
        (("core", _contribution("concept-safe")),),
        ((Plugin("z"), (_contribution("concept-safe"),)),),
    )
    assert catalog.names() == ("concept-safe",)
    assert [(issue.error.source, issue.error.field_path) for issue in catalog.issues] == [("system-plugin:z", "topic")]


def test_trusted_duplicate_hard_fails_independent_of_order() -> None:
    candidates = (("core:b", _contribution("concept-safe")), ("core:a", _contribution("concept-safe")))
    for ordered in (candidates, tuple(reversed(candidates))):
        with pytest.raises(DuplicateTopicError) as raised:
            _build_guide_catalog(ordered)
        assert raised.value.source == "core:a"


def test_plugin_collision_and_broken_link_isolation_are_deterministic() -> None:
    plugin_topics = (
        _contribution("plugin/z/shared"),
        _contribution("plugin/z/shared"),
        _contribution("plugin/z/broken", related=["plugin/z/missing"]),
    )
    catalogs = [
        _build_guide_catalog((("core", _contribution("concept-safe")),), ((Plugin("z"), order),))
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
    catalog = _build_guide_catalog((), ((Plugin("z"), (_contribution("plugin/y/topic"),)),))
    assert catalog.names() == ()
    assert [(issue.error.topic, issue.error.field_path) for issue in catalog.issues] == [("plugin/y/topic", "topic")]


def test_taxonomy_gate_is_runtime_fail_soft_and_ci_strict_for_trusted_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _TestKind()
    monkeypatch.setitem(KIND_REGISTRY, handler.kind, handler)
    topic = _contribution("guide-test/demo")
    object.__setattr__(handler, "category", "capability")

    catalog = _build_guide_catalog(
        (("core:bad-taxonomy", topic), ("core:safe", _contribution("concept-safe"))),
    )
    assert catalog.names() == ("concept-safe",)
    assert [(issue.error.topic, issue.error.field_path) for issue in catalog.issues] == [("guide-test/demo", "anchor")]

    with pytest.raises(GuideContributionError) as raised:
        _build_guide_catalog((("core:bad-taxonomy", topic),), strict_trusted_taxonomy=True)
    assert raised.value.field_path == "anchor"

    plugin_catalog = _build_guide_catalog(
        (),
        ((Plugin("z"), (topic,)),),
        (("z", "guide-test", "demo"),),
        strict_trusted_taxonomy=True,
    )
    assert plugin_catalog.names() == ()
    assert [(issue.error.topic, issue.error.field_path) for issue in plugin_catalog.issues] == [
        ("guide-test/demo", "anchor")
    ]


def test_registered_plugin_implementation_and_owner_adapter_resource_topics_are_accepted() -> None:
    from agentworks.plugins import SYSTEM_PLUGINS

    plugin = SYSTEM_PLUGINS["onepassword"]
    kind, implementations = next(iter(plugin.capabilities.items()))
    implementation = cast("Any", implementations[0])
    implementation_name = implementation.name
    impl_topic = _contribution(
        f"{kind}/{implementation_name}",
        anchor=ImplementationAnchor(kind, implementation_name),
    )
    resource_topic = _contribution("vm-template/plugin-owned")
    catalog = _build_guide_catalog(
        (),
        ((plugin, (impl_topic, resource_topic)),),
        ((plugin.name, "vm-template", "plugin-owned"),),
    )
    assert catalog.names() == (f"{kind}/{implementation_name}", "vm-template/plugin-owned")


def test_trusted_broken_link_hard_fails() -> None:
    with pytest.raises(BrokenTopicLinkError):
        _build_guide_catalog((("core", _contribution("concept-safe", related=["missing"])),))


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


def _action_value(*, action_id: str = "verify", manual: bool = False) -> dict[str, object]:
    value: dict[str, object] = {
        "id": action_id,
        "precondition": "Needs verification.",
        "required_inputs": [{"name": "NAME", "description": "Resource name.", "required": True, "sensitive": False}],
        "consent": "read-configured-state",
        "expected_state": "Verified.",
        "verification": ["agw", "resource", "$NAME"],
        "refusal_alternative": "Inspect manually.",
    }
    if manual:
        value["manual_steps"] = "Inspect only NAME and record the result."
    else:
        value["command"] = ["agw", "resource", "$NAME"]
    return value


def _action_topic(actions: list[dict[str, object]]) -> dict[str, object]:
    topic = _topic("concept-actions")
    topic["blocks"] = [{"type": "action-list", "id": "actions", "actions": actions}]
    return topic


def test_action_list_recursively_parses_commands_and_manual_steps_without_retaining_inputs() -> None:
    command = _action_value()
    manual = _action_value(action_id="inspect", manual=True)
    parsed = parse_topic_contribution(_action_topic([command, manual]), "core")

    block = parsed.blocks[0]
    assert isinstance(block, ActionList)
    assert block.actions[0].command == ("agw", "resource", "$NAME")
    assert block.actions[1].manual_steps == "Inspect only NAME and record the result."
    command["command"] = ["changed"]
    assert block.actions[0].command == ("agw", "resource", "$NAME")


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (lambda action: action.update(extra="no"), "blocks[0].actions[0].extra"),
        (
            lambda action: action["required_inputs"][0].update(extra="no"),
            "blocks[0].actions[0].required_inputs[0].extra",
        ),
        (lambda action: action.update(manual_steps="also manual"), "blocks[0].actions[0]"),
        (lambda action: action.pop("command"), "blocks[0].actions[0]"),
    ],
)
def test_action_list_nested_shape_and_exact_one_operation_fail_closed(mutation, path: str) -> None:
    action = _action_value()
    mutation(action)
    with pytest.raises(GuideContributionError) as raised:
        parse_topic_contribution(_action_topic([action]), "plugin:z")
    assert raised.value.field_path == path


def test_action_ids_are_unique_across_action_blocks() -> None:
    topic = _action_topic([_action_value()])
    topic["blocks"] = [
        {"type": "action-list", "id": "first", "actions": [_action_value()]},
        {"type": "action-list", "id": "second", "actions": [_action_value()]},
    ]
    with pytest.raises(InvalidBlockError) as raised:
        parse_topic_contribution(topic, "core")
    assert raised.value.field_path == "blocks"


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("required_inputs", [{}] * 33, "blocks[0].actions[0].required_inputs"),
        ("command", ["agw"] * 65, "blocks[0].actions[0].command"),
        ("verification", ["agw"] * 65, "blocks[0].actions[0].verification"),
        ("precondition", "x" * (8 * 1024 + 1), "blocks[0].actions[0].precondition"),
    ],
)
def test_action_list_nested_bounds_fail_closed(field: str, value: object, path: str) -> None:
    action = _action_value()
    action[field] = value
    with pytest.raises(GuideContributionError) as raised:
        parse_topic_contribution(_action_topic([action]), "core")
    assert raised.value.field_path == path


def test_action_list_never_interpolates_sensitive_inputs() -> None:
    action = _action_value()
    inputs = action["required_inputs"]
    assert isinstance(inputs, list)
    assert isinstance(inputs[0], dict)
    inputs[0]["sensitive"] = True
    with pytest.raises(GuideContributionError) as raised:
        parse_topic_contribution(_action_topic([action]), "core")
    # The rejection lands on the interpolating token, not the block or the input.
    assert raised.value.field_path == "blocks[0].actions[0].command[2]"


@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("precondition", "blocks[0].actions[0].precondition"),
        ("expected_state", "blocks[0].actions[0].expected_state"),
        ("refusal_alternative", "blocks[0].actions[0].refusal_alternative"),
        ("manual_steps", "blocks[0].actions[0].manual_steps"),
        ("input_description", "blocks[0].actions[0].required_inputs[0].description"),
    ],
)
def test_action_list_prose_cannot_bypass_expression_delimiter_validation(field: str, path: str) -> None:
    action = _action_value(manual=field == "manual_steps")
    if field == "input_description":
        inputs = cast("list[dict[str, object]]", action["required_inputs"])
        inputs[0]["description"] = "Render {{danger()}}."
    else:
        action[field] = "Render {{danger()}}."

    with pytest.raises(GuideContributionError) as raised:
        parse_topic_contribution(_action_topic([action]), "plugin:bad")

    assert raised.value.field_path == path


def test_action_list_prose_preserves_exact_inert_inline_literals() -> None:
    action = _action_value(manual=True)
    action["manual_steps"] = "Write `{{literal_name}}` exactly."

    parsed = parse_topic_contribution(_action_topic([action]), "plugin:safe")

    block = cast("ActionList", parsed.blocks[0])
    assert block.actions[0].manual_steps == "Write `{{literal_name}}` exactly."


def test_action_list_count_and_cumulative_byte_bounds_fail_closed() -> None:
    # Each payload trips exactly one bound: 33 small actions stay well under the
    # byte cap, and 17 maximal manual steps stay under the action count.
    with pytest.raises(InvalidBlockError):
        parse_topic_contribution(
            _action_topic([_action_value(action_id=f"action-{index}") for index in range(33)]),
            "core",
        )
    actions = [_action_value(action_id=f"action-{index}", manual=True) for index in range(17)]
    for action in actions:
        action["manual_steps"] = "x" * (8 * 1024)
    with pytest.raises(InvalidBlockError):
        parse_topic_contribution(_action_topic(actions), "core")


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
    [
        "a;b",
        "a|b",
        "a&b",
        "a>b",
        "a<b",
        "a'b",
        'a"b',
        "a\\b",
        "a b",
        "a\nb",
        "a\x1bb",
        "*",
        "?",
        "[abc]",
        "#comment",
        "(command)",
        "{left,right}",
        "~/state",
        "!history",
        "$(command)",
        "${NAME}",
        "$NAME-suffix",
        "NAME=value",
        "--flag=value",
    ],
)
@pytest.mark.parametrize("field", ["command", "verification"])
def test_action_tokens_reject_shell_syntax_whitespace_and_controls_anywhere(field: str, token: str) -> None:
    action = _action()
    object.__setattr__(action, field, ("agw", token))
    with pytest.raises(GuideContributionError):
        validate_guide_action(action, "core")


@pytest.mark.parametrize("token", ["--non-interactive", "vm-template/demo", "secret_name", "v1.2", "127.0.0.1"])
def test_action_tokens_accept_only_the_closed_literal_vocabulary(token: str) -> None:
    action = _action()
    object.__setattr__(action, "command", ("agw", token))
    assert validate_guide_action(action, "core").command == ("agw", token)


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


@pytest.mark.parametrize(
    ("section", "path"),
    [
        ([f"field-{index}" for index in range(33)], "blocks[0].section"),
        (["x" * 257], "blocks[0].section[0]"),
        (["   "], "blocks[0].section[0]"),
        ([object()], "blocks[0].section[0]"),
    ],
)
def test_field_reference_section_bounds_are_nested_and_typed(section: list[object], path: str) -> None:
    topic = _topic("concept-safe")
    topic["blocks"] = [{"type": "field-reference", "id": "fields", "section": section}]

    with pytest.raises(InvalidBlockError) as raised:
        parse_topic_contribution(topic, "plugin:z")
    assert raised.value.field_path == path


@pytest.mark.parametrize("related", ["Not-Valid", "concept/a/b/c", "concept_bad"])
def test_related_topics_apply_the_canonical_slug_grammar(related: str) -> None:
    with pytest.raises(InvalidTopicSlugError) as raised:
        parse_topic_contribution(_topic("concept-safe", related=[related]), "plugin:z")
    assert raised.value.field_path == "related_topics[0]"


def test_related_topic_values_have_an_explicit_byte_bound() -> None:
    longest = "k" * 63 + "/" + "s" * 253

    assert parse_topic_contribution(_topic("concept-safe", related=[longest]), "plugin:z").related_topics == (longest,)

    with pytest.raises(InvalidTopicSlugError) as raised:
        parse_topic_contribution(_topic("concept-safe", related=[longest + "s"]), "plugin:z")
    assert raised.value.field_path == "related_topics[0]"


def test_resource_topics_accept_actual_ordinary_and_secret_name_limits() -> None:
    ordinary_name = "o" * 64
    secret_name = "s" * 253
    for slug in (f"vm-site/{ordinary_name}", f"secret/{secret_name}", "secret/name_with_underscores"):
        assert is_valid_topic_slug(slug)
        assert parse_topic_contribution(_topic(slug), "core").topic == slug


@pytest.mark.parametrize(
    "slug",
    [
        f"secret/{'s' * 254}",
        f"{'k' * 64}/name",
        f"concept-{'c' * 56}",
        f"plugin/{'p' * 64}/topic",
        "plugin/name_with_underscore/topic",
        "vm-site/name.with-dot",
    ],
)
def test_topic_taxonomy_stays_closed_outside_resource_name_limits(slug: str) -> None:
    assert not is_valid_topic_slug(slug)


def test_contribution_total_markdown_volume_is_bounded() -> None:
    topic = _topic("concept-safe")
    topic["blocks"] = [
        {"type": "overview", "id": f"block-{index}", "markdown": "x" * (60 * 1024)} for index in range(5)
    ]
    with pytest.raises(InvalidBlockError) as raised:
        parse_topic_contribution(topic, "plugin:z")
    assert raised.value.field_path == "blocks"
