from __future__ import annotations

from dataclasses import replace

import pytest

from agentworks.errors import ConfigError
from agentworks.guide import (
    BlockId,
    FieldReference,
    GuideContributionError,
    GuideMode,
    InvalidBlockError,
    KindAnchor,
    Overview,
    Sample,
    TopicContribution,
    TopicSlug,
    parse_topic_contribution,
)
from agentworks.guide.catalog import _build_guide_catalog
from agentworks.guide.render import render_topic
from agentworks.guide.service import _dynamic_names, _dynamic_topic, render_guide
from agentworks.manifests.reference import reference_for


def _broken_config() -> object:
    raise ConfigError("retired resource sections remain", hint="Rewrite the named sections as manifests.")


def test_config_free_names_include_every_schema_target_without_a_registry() -> None:
    names = _dynamic_names(None)

    assert "vm-template" in names
    assert "vm-platform" in names
    assert "vm-platform/wsl2" in names


def test_schema_reference_prose_maps_once_and_preserves_authored_markdown() -> None:
    reference = reference_for("session-template")
    topic = _dynamic_topic(None, "session-template")

    assert topic.title == reference.title
    assert topic.summary == reference.summary
    assert reference.summary is not None
    assert reference.overview is not None
    overview = next(block for block in topic.blocks if isinstance(block, Overview))
    assert overview.markdown == reference.overview
    assert "`{{session_name}}`" in overview.markdown
    rendered = render_topic(topic, None, GuideMode.AGENT, unavailable="registry unavailable")
    assert rendered.markdown.count(reference.overview) == 1
    fields = next(block.source_payload for block in rendered.blocks if block.key.block_id == "fields")
    assert fields is not None
    assert reference.summary not in fields
    assert reference.overview not in fields


def test_schema_blocks_render_with_broken_config_beside_static_migration_teaching() -> None:
    response = render_guide(
        ("concept-migration", "vm-template", "vm-platform/wsl2"),
        GuideMode.AGENT,
        load_config_fn=_broken_config,
    )

    assert response.exit_code == 1
    assert "Preserve the migration evidence" in response.markdown
    assert "Reference target: `vm-template`" in response.markdown
    assert "```yaml" in response.markdown
    assert "Reference target: `vm-platform/wsl2`" in response.markdown
    assert response.markdown.count("Configuration error: retired resource sections remain") == 1


def test_capability_topics_never_contribute_samples() -> None:
    kind = _dynamic_topic(None, "vm-platform")
    implementation = _dynamic_topic(None, "vm-platform/wsl2")

    assert any(isinstance(block, FieldReference) for block in kind.blocks)
    assert any(isinstance(block, FieldReference) for block in implementation.blocks)
    assert not any(isinstance(block, Sample) for block in (*kind.blocks, *implementation.blocks))

    invalid = {
        "topic": "vm-platform",
        "title": "Platforms",
        "summary": "Capability kind.",
        "anchor": {"type": "kind", "kind": "vm-platform"},
        "blocks": [{"type": "sample", "id": "sample"}],
    }
    with pytest.raises(GuideContributionError, match="sample block requires a declarable kind"):
        _build_guide_catalog(
            (("core", invalid),),
            strict_trusted_taxonomy=True,
        )


@pytest.mark.parametrize(
    "anchor",
    [
        {"type": "concept", "name": "concept-invalid"},
        {"type": "resource", "kind": "vm-template", "name": "demo"},
    ],
)
@pytest.mark.parametrize("block_type", ["field-reference", "sample"])
def test_concept_and_resource_anchors_reject_schema_blocks(anchor: dict[str, str], block_type: str) -> None:
    topic = anchor["name"] if anchor["type"] == "concept" else f"{anchor['kind']}/{anchor['name']}"
    with pytest.raises(InvalidBlockError):
        parse_topic_contribution(
            {
                "topic": topic,
                "title": "Invalid",
                "summary": "Invalid schema anchor.",
                "anchor": anchor,
                "blocks": [{"type": block_type, "id": "schema"}],
            },
            "plugin:test",
        )


def test_invalid_field_section_is_a_scoped_content_issue() -> None:
    topic = TopicContribution(
        TopicSlug("secret"),
        "Secret",
        "Secret schema.",
        KindAnchor("secret"),
        (FieldReference(BlockId("fields"), ("spec", "absent")),),
    )

    rendered = render_topic(topic, None, GuideMode.HUMAN)

    assert rendered.issues == (
        "schema content for secret/fields is unavailable: field-reference section 'spec.absent' is unavailable",
    )
    assert "Schema content unavailable" in rendered.markdown


def test_field_and_sample_renderers_read_live_services(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.guide.render as guide_render

    original = reference_for("secret")
    first = original.spec[0]
    changed_doc = replace(first.doc, description="Fixture declaration changed.")
    changed = replace(original, spec=(replace(first, doc=changed_doc), *original.spec[1:]))
    monkeypatch.setattr(guide_render, "reference_for", lambda target: changed)
    monkeypatch.setattr(guide_render, "sample_text", lambda kind: "# live fixture sample")
    topic = TopicContribution(
        TopicSlug("secret"),
        "Secret",
        "Secret schema.",
        KindAnchor("secret"),
        (FieldReference(BlockId("fields")), Sample(BlockId("sample"))),
    )

    rendered = render_topic(topic, None, GuideMode.AGENT)

    assert "Fixture declaration changed." in rendered.markdown
    assert "# live fixture sample" in rendered.markdown


def test_schema_block_payloads_are_identical_between_modes() -> None:
    topic = _dynamic_topic(None, "secret")
    human = render_topic(topic, None, GuideMode.HUMAN, unavailable="registry unavailable")
    agent = render_topic(topic, None, GuideMode.AGENT, unavailable="registry unavailable")

    assert [(block.key, block.source_payload) for block in human.blocks] == [
        (block.key, block.source_payload) for block in agent.blocks
    ]


def test_explicit_resource_request_still_degrades_under_broken_config() -> None:
    response = render_guide(("vm-template/demo",), GuideMode.AGENT, load_config_fn=_broken_config)

    assert response.exit_code == 1
    assert "# vm-template/demo" in response.markdown
    assert response.markdown.count("Live facts unavailable: see the system failure below") == 3
    assert "`vm-template`" in response.markdown
