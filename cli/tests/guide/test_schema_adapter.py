from __future__ import annotations

from dataclasses import replace
from enum import Enum
from pathlib import Path

import pytest

from agentworks.errors import ConfigError, ValidationError
from agentworks.guide import (
    BlockId,
    FieldReference,
    GuideContributionError,
    GuideMode,
    ImplementationAnchor,
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
from agentworks.guide.service import (
    _build_schema_catalog,
    _dynamic_names,
    _dynamic_topic,
    _EmptyInventory,
    render_guide,
)
from agentworks.manifests.field_tree import Alternative, FieldEntry
from agentworks.manifests.reference import SchemaReference, describable_targets, reference_for
from agentworks.manifests.yaml_value import render_value


def _broken_config() -> object:
    raise ConfigError("retired resource sections remain", hint="Rewrite the named sections as manifests.")


def test_config_free_names_include_every_schema_target_without_a_registry() -> None:
    schema = _build_schema_catalog(strict=True)
    names = _dynamic_names(None, schema)

    assert schema.issues == ()
    assert schema.names() == describable_targets()
    assert "vm-template" in names
    assert "vm-platform" in names
    assert "vm-platform/wsl2" in names


def _reject_vm_template_title(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid_reference(target: str):
        reference = reference_for(target)
        if target == "vm-template":
            return replace(reference, title="Untrusted ${SCHEMA_PAYLOAD}")
        return reference

    monkeypatch.setattr("agentworks.guide.service.reference_for", invalid_reference)


def test_strict_schema_catalog_rejects_one_invalid_target_with_scoped_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject_vm_template_title(monkeypatch)

    with pytest.raises(GuideContributionError) as raised:
        _build_schema_catalog(strict=True)

    assert raised.value.source == "schema:vm-template"
    assert raised.value.topic == "vm-template"
    assert raised.value.field_path == "title"
    assert "SCHEMA_PAYLOAD" not in str(raised.value)


def test_schema_service_error_is_strict_for_ci_and_isolated_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_error = ValidationError("SCHEMA_SERVICE_PAYLOAD must not be rendered")

    def unavailable_reference(target: str):
        if target == "vm-template":
            raise service_error
        return reference_for(target)

    monkeypatch.setattr("agentworks.guide.service.reference_for", unavailable_reference)

    with pytest.raises(ValidationError) as raised:
        _build_schema_catalog(strict=True)
    assert raised.value is service_error

    response = render_guide(
        ("vm-template", "agent-template", "vm-platform/azure-vm"),
        GuideMode.AGENT,
        load_config_fn=lambda: object(),  # type: ignore[arg-type,return-value]
        load_registry_fn=lambda _config: None,  # type: ignore[arg-type,return-value]
    )

    assert response.exit_code == 1
    assert "# vm-template\n\nThis guide topic is unavailable." in response.markdown
    assert "Reference target: `agent-template`" in response.markdown
    assert "`config.auth.secret`" in response.markdown
    assert "invalid guide contribution from schema:vm-template: reference is unavailable" in response.markdown
    assert "vm-template" not in response.names
    assert "SCHEMA_SERVICE_PAYLOAD" not in response.markdown


def test_runtime_isolates_invalid_schema_target_in_explicit_multi_topic_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject_vm_template_title(monkeypatch)

    response = render_guide(
        ("vm-template", "agent-template"),
        GuideMode.AGENT,
        load_config_fn=lambda: object(),  # type: ignore[arg-type,return-value]
        load_registry_fn=lambda _config: None,  # type: ignore[arg-type,return-value]
    )

    assert response.exit_code == 1
    assert "# vm-template\n\nThis guide topic is unavailable." in response.markdown
    assert "Reference target: `agent-template`" in response.markdown
    assert "schema:vm-template" in response.markdown
    assert "title contains an expression delimiter" in response.markdown
    assert "SCHEMA_PAYLOAD" not in response.markdown


def _implementation_field_topic(target: str, section: tuple[str, ...] = ()) -> TopicContribution:
    kind, name = target.split("/", 1)
    return TopicContribution(
        TopicSlug(target),
        target,
        "Implementation schema.",
        ImplementationAnchor(kind, name),
        (FieldReference(BlockId("fields"), section),),
    )


def _kind_field_topic(kind: str) -> TopicContribution:
    return TopicContribution(
        TopicSlug(kind),
        kind,
        "Declarable schema.",
        KindAnchor(kind),
        (FieldReference(BlockId("fields")),),
    )


@pytest.mark.parametrize(
    ("target", "default", "arms", "fields"),
    [
        (
            "vm-platform/azure-vm",
            "default `{mode: ambient}`",
            ("ambient", "service-principal"),
            ("config.auth.tenant_id", "config.auth.client_id", "config.auth.secret"),
        ),
        (
            "vm-platform/aws-ec2",
            "default `{mode: ambient}`",
            ("ambient", "access-key"),
            ("config.auth.access_key_id", "config.auth.access_key_secret", "config.auth.assume_role_arn"),
        ),
        (
            "vm-platform/gcp-gce",
            "default `{mode: ambient}`",
            ("ambient", "service-account"),
            ("config.auth.secret",),
        ),
        (
            "vm-platform/lima",
            "default `{mode: local}`",
            ("local", "ssh"),
            ("config.placement.host",),
        ),
    ],
)
def test_schema_fields_render_every_nested_union_arm_from_the_live_reference(
    target: str,
    default: str,
    arms: tuple[str, ...],
    fields: tuple[str, ...],
) -> None:
    rendered = render_topic(_implementation_field_topic(target), None, GuideMode.AGENT)
    payload = rendered.blocks[0].source_payload

    assert payload is not None
    assert default in payload
    for arm in arms:
        assert f"Alternative `{arm}`" in payload
    for field in fields:
        assert f"`{field}`" in payload


@pytest.mark.parametrize(
    ("topic", "token_path", "mode_path", "secret_path"),
    [
        pytest.param(
            _kind_field_topic("git-credential"),
            "spec.provider.token",
            "spec.provider.token.mode",
            "spec.provider.token.secret",
            id="declarable-git-credential",
        ),
        pytest.param(
            _implementation_field_topic("git-credential-provider/github"),
            "config.token",
            "config.token.mode",
            "config.token.secret",
            id="github-provider",
        ),
        pytest.param(
            _implementation_field_topic("git-credential-provider/azdo"),
            "config.token",
            "config.token.mode",
            "config.token.secret",
            id="azdo-provider",
        ),
    ],
)
def test_git_token_structural_union_renders_without_dropping_untagged_arm_fields(
    topic: TopicContribution,
    token_path: str,
    mode_path: str,
    secret_path: str,
) -> None:
    rendered = render_topic(topic, None, GuideMode.AGENT)
    payload = rendered.blocks[0].source_payload

    assert rendered.issues == ()
    assert payload is not None
    assert f"`{token_path}`: optional; `string or table`; default `{{mode: secret}}`" in payload
    assert payload.count("Alternative `secret`") == 1
    assert f"`{mode_path}`: required; `one of: secret`; choices `secret`" in payload
    assert f"`{secret_path}`: optional; `string or null`; owner default `git-token-<name>`" in payload
    assert "Alternative `minted`" not in payload


@pytest.mark.parametrize(
    ("target", "mode_path"),
    [
        ("vm-platform/azure-vm", "config.auth.mode"),
        ("vm-platform/aws-ec2", "config.auth.mode"),
        ("vm-platform/gcp-gce", "config.auth.mode"),
        ("vm-platform/lima", "config.placement.mode"),
    ],
)
def test_each_live_auth_or_placement_arm_keeps_its_own_mode_row(
    target: str,
    mode_path: str,
) -> None:
    rendered = render_topic(_implementation_field_topic(target), None, GuideMode.AGENT)
    payload = rendered.blocks[0].source_payload

    assert payload is not None
    assert payload.count(f"`{mode_path}`") == 2


@pytest.mark.parametrize(
    ("target", "section", "rendered_path"),
    [
        ("vm-platform/azure-vm", ("auth", "secret"), "config.auth.secret"),
        ("vm-platform/aws-ec2", ("auth", "access_key_id"), "config.auth.access_key_id"),
        ("vm-platform/gcp-gce", ("auth", "secret"), "config.auth.secret"),
        ("vm-platform/lima", ("placement", "host"), "config.placement.host"),
    ],
)
def test_field_section_selects_a_field_unique_to_one_union_arm(
    target: str,
    section: tuple[str, ...],
    rendered_path: str,
) -> None:
    rendered = render_topic(_implementation_field_topic(target, section), None, GuideMode.AGENT)
    payload = rendered.blocks[0].source_payload

    assert rendered.issues == ()
    assert payload is not None
    assert f"`{rendered_path}`" in payload


def test_field_section_refuses_a_name_repeated_across_union_arms() -> None:
    rendered = render_topic(
        _implementation_field_topic("vm-platform/azure-vm", ("auth", "mode")),
        None,
        GuideMode.AGENT,
    )

    assert rendered.issues == (
        "schema content for vm-platform/azure-vm/fields is unavailable: "
        "field-reference section 'auth.mode' is unavailable",
    )
    assert "Schema content unavailable" in rendered.markdown


def _shared_intermediate_reference(*, duplicate_leaf: bool) -> SchemaReference:
    original = reference_for("secret")
    template = original.spec[0]

    def entry(path: tuple[str, ...], *, children: tuple[FieldEntry, ...] = ()) -> FieldEntry:
        return replace(
            template,
            doc=replace(template.doc, path=path),
            children=children,
            alternatives=(),
        )

    first_credentials = entry(
        ("auth", "credentials"),
        children=(entry(("auth", "credentials", "secret")),),
    )
    second_leaf = "secret" if duplicate_leaf else "profile"
    second_credentials = entry(
        ("auth", "credentials"),
        children=(entry(("auth", "credentials", second_leaf)),),
    )
    auth = replace(
        template,
        doc=replace(template.doc, path=("auth",)),
        children=(),
        alternatives=(
            Alternative(name="first", summary=None, target=None, fields=(first_credentials,)),
            Alternative(name="second", summary=None, target=None, fields=(second_credentials,)),
        ),
    )
    return replace(original, metadata=(), spec=(auth,))


def test_field_section_carries_shared_intermediate_candidates_to_a_unique_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.guide.render as guide_render

    monkeypatch.setattr(
        guide_render,
        "reference_for",
        lambda _target: _shared_intermediate_reference(duplicate_leaf=False),
    )
    topic = TopicContribution(
        TopicSlug("secret"),
        "Secret",
        "Secret schema.",
        KindAnchor("secret"),
        (FieldReference(BlockId("fields"), ("auth", "credentials", "secret")),),
    )

    rendered = render_topic(topic, None, GuideMode.AGENT)
    payload = rendered.blocks[0].source_payload

    assert rendered.issues == ()
    assert payload is not None
    assert "`spec.auth.credentials.secret`" in payload


def test_field_section_refuses_a_shared_intermediate_with_duplicate_full_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.guide.render as guide_render

    monkeypatch.setattr(
        guide_render,
        "reference_for",
        lambda _target: _shared_intermediate_reference(duplicate_leaf=True),
    )
    topic = TopicContribution(
        TopicSlug("secret"),
        "Secret",
        "Secret schema.",
        KindAnchor("secret"),
        (FieldReference(BlockId("fields"), ("auth", "credentials", "secret")),),
    )

    rendered = render_topic(topic, None, GuideMode.AGENT)

    assert rendered.issues == (
        "schema content for secret/fields is unavailable: "
        "field-reference section 'auth.credentials.secret' is unavailable",
    )
    assert "Schema content unavailable" in rendered.markdown


def test_union_arms_explain_recursion_and_point_to_addressable_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.guide.render as guide_render

    original = reference_for("secret")
    entry = original.spec[0]
    union = replace(
        entry,
        children=(),
        alternatives=(
            Alternative(name="recursive", summary="Repeats itself.", target=None, recurring=True),
            Alternative(
                name="external",
                summary="Documented elsewhere.",
                target="vm-platform/wsl2",
            ),
        ),
    )
    monkeypatch.setattr(guide_render, "reference_for", lambda _target: replace(original, spec=(union,)))
    topic = TopicContribution(
        TopicSlug("secret"),
        "Secret",
        "Secret schema.",
        KindAnchor("secret"),
        (FieldReference(BlockId("fields")),),
    )

    rendered = render_topic(topic, None, GuideMode.AGENT)
    payload = rendered.blocks[0].source_payload

    assert payload is not None
    assert "Recurring arm: this alternative repeats the block already shown above." in payload
    assert "Full reference: `agw guide vm-platform/wsl2`" in payload
    assert "`agw resource describe-kind vm-platform/wsl2`" in payload


def test_runtime_index_retains_other_topics_when_one_schema_target_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject_vm_template_title(monkeypatch)

    response = render_guide(
        (),
        GuideMode.AGENT,
        load_config_fn=lambda: object(),  # type: ignore[arg-type,return-value]
        load_registry_fn=lambda _config: None,  # type: ignore[arg-type,return-value]
    )

    assert response.exit_code == 1
    assert "agent-template" in response.names
    assert "vm-template" not in response.names
    assert "`agent-template`" in response.markdown
    assert "schema:vm-template" in response.markdown
    assert "SCHEMA_PAYLOAD" not in response.markdown


def test_name_discovery_omits_invalid_schema_target_without_echoing_its_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reject_vm_template_title(monkeypatch)

    response = render_guide(
        (),
        GuideMode.AGENT,
        names_only=True,
        load_config_fn=lambda: object(),  # type: ignore[arg-type,return-value]
        load_registry_fn=lambda _config: None,  # type: ignore[arg-type,return-value]
    )

    assert response.exit_code == 0
    assert "agent-template" in response.names
    assert "vm-template" not in response.names
    assert "SCHEMA_PAYLOAD" not in response.markdown
    assert "Guide content unavailable" not in response.markdown


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


class _LiteralChoice(Enum):
    BACKTICKS = "a``b"


def test_schema_scalar_facts_render_authoritative_yaml_without_prose_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.guide.render as guide_render

    original = reference_for("secret")
    entry = original.spec[0]
    literal_doc = replace(
        entry.doc,
        default=True,
        default_template=None,
        description=None,
        choices=(_LiteralChoice.BACKTICKS, "", " "),
        constraints={"allowed": [True, "a``b"], "pattern": "a``b"},
        examples=(
            {"enabled": True},
            ["a``b", False],
            "",
            " ",
            "top\n\nbottom",
            "carriage\rreturn",
            "tab\tvalue",
            r"slash\ntext",
        ),
    )
    literal_entry = replace(entry, doc=literal_doc, children=(), alternatives=())
    reference = replace(original, metadata=(), spec=(literal_entry,), alternatives=(), root_value=None)
    monkeypatch.setattr(guide_render, "reference_for", lambda target: reference)
    monkeypatch.setattr(guide_render, "plain_text", lambda value: pytest.fail(f"plain_text called for {value!r}"))
    topic = TopicContribution(
        TopicSlug("secret"),
        "Secret",
        "Secret schema.",
        KindAnchor("secret"),
        (FieldReference(BlockId("fields")),),
    )

    rendered = render_topic(topic, None, GuideMode.AGENT)

    assert "default `true`" in rendered.markdown
    assert f"choices ```{render_value(_LiteralChoice.BACKTICKS)}```" in rendered.markdown
    assert f"`{render_value('')}`" in rendered.markdown
    assert f"`{render_value(' ')}`" in rendered.markdown
    assert f"allowed ```{render_value([True, 'a``b'])}```" in rendered.markdown
    assert f"pattern ```{render_value('a``b')}```" in rendered.markdown
    assert f"`{render_value({'enabled': True})}`" in rendered.markdown
    assert f"```{render_value(['a``b', False])}```" in rendered.markdown
    fields = rendered.blocks[0].source_payload
    assert fields is not None
    assert fields.count("\n") == 2
    assert r"\n\n" in fields
    assert r"\\r" in fields
    assert r"\\t" in fields
    assert r"\\n" in fields


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a``b", "```a``b```"),
        ("`leading", "`` `leading ``"),
        ("trailing`", "`` trailing` ``"),
        (" edge ", "`  edge  `"),
    ],
)
def test_schema_code_spans_use_longer_delimiters_and_needed_edge_padding(value: str, expected: str) -> None:
    from agentworks.guide.render import _code

    assert _code(value) == expected


def test_schema_value_turns_blank_line_yaml_into_one_lossless_display_line() -> None:
    from agentworks.guide.render import _schema_value

    yaml_value = render_value("top\n\nbottom")
    rendered = _schema_value("top\n\nbottom")

    assert "\n\n" in yaml_value
    assert not any(control in rendered for control in ("\r", "\n", "\t"))
    assert r"\n\n\n  bottom" in rendered


def test_schema_value_distinguishes_controls_from_literal_backslash_sequences() -> None:
    from agentworks.guide.render import _schema_value

    displays = {
        "lf": _schema_value("line\nbreak"),
        "slash-n": _schema_value(r"line\nbreak"),
        "cr": _schema_value("carriage\rreturn"),
        "slash-r": _schema_value(r"carriage\rreturn"),
        "tab": _schema_value("tab\tvalue"),
        "slash-t": _schema_value(r"tab\tvalue"),
    }

    assert len(set(displays.values())) == len(displays)
    assert all(not any(control in display for control in ("\r", "\n", "\t")) for display in displays.values())
    assert r"\n" in displays["lf"]
    assert r"\\n" in displays["slash-n"]
    assert r"\\r" in displays["cr"]
    assert r"\\t" in displays["tab"]


def test_schema_value_is_single_line_and_commonmark_safe_with_backticks() -> None:
    from agentworks.guide.render import _schema_value

    rendered = _schema_value("a``b\n\npath\\name\tvalue")

    assert rendered.startswith("```") and rendered.endswith("```")
    assert not any(control in rendered for control in ("\r", "\n", "\t"))
    assert "a``b" in rendered
    assert r"\\n\\n" in rendered
    assert r"\\" in rendered
    assert r"\\t" in rendered


def test_schema_blocks_render_with_broken_config_beside_static_migration_teaching() -> None:
    response = render_guide(
        ("concept-migration", "vm-template", "vm-platform/wsl2"),
        GuideMode.AGENT,
        load_config_fn=_broken_config,
    )

    assert response.exit_code == 1
    assert "Inventory and preserve the migration evidence" in response.markdown
    assert "Reference target: `vm-template`" in response.markdown
    assert "```yaml" in response.markdown
    assert "Reference target: `vm-platform/wsl2`" in response.markdown
    assert response.markdown.count("Configuration error: retired resource sections remain") == 1


def test_disabled_proxmox_renders_disabled_state_and_config_free_schema(tmp_path: Path) -> None:
    from agentworks.bootstrap import load_guide_registry
    from agentworks.config import load_config

    private_key = tmp_path / "id"
    public_key = tmp_path / "id.pub"
    private_key.write_text("private")
    public_key.write_text("public")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'[operator]\nssh_public_key = "{public_key}"\nssh_private_key = "{private_key}"\n')
    config = load_config(config_path, warn_issues=False, warn_deprecations=False)
    assert config.enabled_system_plugins == ()

    response = render_guide(
        ("vm-platform/proxmox",),
        GuideMode.AGENT,
        load_config_fn=lambda: config,
        load_registry_fn=load_guide_registry,
        db=_EmptyInventory(),  # type: ignore[arg-type]
    )

    assert response.exit_code == 0
    assert "`vm-platform/proxmox` (disabled)" in response.markdown
    assert "Reference target: `vm-platform/proxmox`" in response.markdown
    assert "`config.api_url`" in response.markdown


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
