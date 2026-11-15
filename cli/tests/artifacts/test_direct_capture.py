"""Capture, identity, and metadata inspection for direct artifact definitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from agentworks.artifacts.bundle import ArtifactBundle, resolve_bundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.declarations import ArtifactDefinitions, ArtifactsConfig, HintArtifactSpec, RuleArtifactSpec
from agentworks.artifacts.inspection import inspect_artifacts, inspection_data, render_artifacts
from agentworks.artifacts.model import ArtifactOrigin, ArtifactType
from agentworks.artifacts.native.shell import filesystem_artifacts
from agentworks.artifacts.routing import inspect_owner_artifacts
from agentworks.artifacts.state import capture_owner, declaration_digest, read_captures, write_capture
from agentworks.db import AppliedStateKey
from agentworks.errors import StateError
from agentworks.origin import Origin
from agentworks.package_sources import CaptureLimits, PackageCapture
from agentworks.sources import SourceRefError
from tests.artifacts._fixtures import received
from tests.artifacts.test_routing import graph
from tests.artifacts.test_type_maps import registry_with


def test_direct_overrides_ordered_bundles_by_type_and_key_with_complete_replacement(tmp_path):
    source = tmp_path / "direct.md"
    source.write_bytes(b"direct\r\nnormalized\r")
    first = ArtifactBundle(name="first", hints={"same": HintArtifactSpec(text="first")})
    second = ArtifactBundle(
        name="second",
        hints={"same": HintArtifactSpec(text="second"), "kept": HintArtifactSpec(text="kept")},
        rules={"same": RuleArtifactSpec(text="rule")},
    )
    direct = ArtifactDefinitions.model_validate({"hints": {"same": {"source": str(source)}, "last": {"text": "last"}}})
    origin = ArtifactOrigin("agent", "agent", "worker")
    captured = capture_artifacts([(row.name, row) for row in (first, second)], origin, declare=direct)
    assert list(captured.hints) == ["same", "kept", "last"]
    assert captured.hints["same"].content.text == "direct\nnormalized\n"
    assert captured.rules["same"].content.text == "rule"
    item = captured.hints["same"]
    assert item.origin.bundle == "" and item.origin.entry == "same"
    assert item.provenance.source == str(source)
    assert [previous.origin.bundle for previous in item.replacements] == ["first", "second"]
    assert decode_inputs(json.loads(json.dumps(encode_inputs(captured)))) == (4, captured)


def test_bundle_to_direct_preserves_input_identity_and_native_claim_address():
    bundle = ArtifactBundle(name="team", hints={"same": HintArtifactSpec(text="same")})
    origin = ArtifactOrigin("session", "session", "run")
    bundled = capture_artifacts([("team", bundle)], origin)
    direct = capture_artifacts([], origin, declare=ArtifactDefinitions(hints=bundle.hints))
    before, after = bundled.hints["same"], direct.hints["same"]
    assert before.identity == after.identity and before.origin_identity == after.origin_identity
    assert before.content == after.content and before.provenance == after.provenance
    before_files = filesystem_artifacts(received(before), "/native", session=True).files
    after_files = filesystem_artifacts(received(after), "/native", session=True).files
    assert [(file.path, file.origins, file.native_identity) for file in before_files] == [
        (file.path, file.origins, file.native_identity) for file in after_files
    ]
    # Type and original owner remain independent even with equal keys and text.
    another = capture_artifacts(
        [], replace(origin, component="agent", resource_kind="agent"), declare=ArtifactDefinitions(hints=bundle.hints)
    )
    assert next(another.items()).identity != after.identity


@pytest.mark.parametrize("limits", [CaptureLimits(total_bytes=1001), CaptureLimits(members=1)])
@pytest.mark.parametrize("source_kind", ["file", "text", "mcp"])
def test_bundle_and_direct_share_content_and_member_budgets(tmp_path, limits, source_kind):
    bundle = ArtifactBundle(name="team", hints={"one": HintArtifactSpec(text="1" * 1000)})
    source = tmp_path / "note.md"
    source.write_text("5678")
    definitions = {
        "file": {"hints": {"two": {"source": str(source)}}},
        "text": {"hints": {"two": {"text": "5678"}}},
        "mcp": {"mcp_servers": {"two": {"definition": {"type": "stdio", "command": "fixture"}}}},
    }
    with PackageCapture(limits) as operation:
        staging = operation.root
        with pytest.raises(SourceRefError):
            capture_artifacts(
                [("team", bundle)],
                ArtifactOrigin("agent", "agent", "worker"),
                declare=ArtifactDefinitions.model_validate(definitions[source_kind]),
                operation=operation,
            )
    assert not staging.exists()


def test_direct_all_types_validate_and_normalize_on_same_capture_path(tmp_path):
    skill = tmp_path / "review"
    skill.mkdir()
    (skill / "SKILL.md").write_bytes(b"---\r\nname: review\r\ndescription: Review\r\n---\r\nReview.\r\n")
    persona = tmp_path / "reviewer.md"
    persona.write_bytes(b"---\r\nname: reviewer\r\ndescription: Review\r\n---\r\nReview.\r\n")
    mcp = tmp_path / "mcp.jsonc"
    mcp.write_text('{"mcpServers":{"tools":{"type":"stdio","command":"fixture"}}}')
    direct = ArtifactDefinitions.model_validate(
        {
            "hints": {"note": {"text": "context\r\n"}},
            "rules": {"style": {"text": "---\r\ndescription: Style\r\n---\r\nRule.\r\n"}},
            "skills": {"review": {"source": str(skill)}},
            "agents": {"reviewer": {"source": str(persona)}},
            "mcp_servers": {"tools": {"source": str(mcp)}},
        }
    )
    captured = capture_artifacts([], ArtifactOrigin("agent", "agent", "worker"), declare=direct)
    assert [item.content.type for item in captured.items()] == list(ArtifactType)
    assert all(item.origin.bundle == "" for item in captured.items())
    assert all("\r" not in item.content.text for item in captured.items())
    assert captured.hints["note"].provenance.source == "inline"
    assert captured.skills["review"].provenance.source == str(skill)
    assert captured.mcp_servers["tools"].provenance.source == str(mcp)
    assert decode_inputs(encode_inputs(captured)) == (4, captured)


def test_direct_capture_failure_does_not_echo_content_or_source(tmp_path):
    source = tmp_path / "private-source-value.md"
    source.write_bytes(b"private-content\x00")
    config = ArtifactsConfig.model_validate({"declare": {"rules": {"style": {"source": str(source)}}}})
    with pytest.raises(StateError) as caught:
        capture_owner(registry_with(), "agent", "worker", "agent", config)
    assert "private-source-value" not in str(caught.value) and "private-content" not in str(caught.value)


def test_bundle_digest_bytes_remain_compatible_when_direct_definitions_are_empty():
    bundle = ArtifactBundle(name="team", hints={"note": HintArtifactSpec(text="context")})
    registry = registry_with(bundle)
    resolved = resolve_bundle(registry, "team").value
    legacy = [
        (
            "team",
            {
                kind.map_name: {name: spec.model_dump(mode="json") for name, spec in entries.items()}
                for kind, entries in resolved.type_maps()
                if kind != ArtifactType.MCP_SERVER or entries
            },
        )
    ]
    expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    for declaration in (None, ArtifactDefinitions()):
        config = ArtifactsConfig(bundles=["team"], declare=declaration)
        assert declaration_digest(registry, config) == expected
    config = config.model_copy(update={"declare": ArtifactDefinitions(hints={"note": HintArtifactSpec(text="direct")})})
    assert declaration_digest(registry, config) != expected
    first = declaration_digest(registry, config)
    assert config.declare is not None
    config.declare.hints["note"] = HintArtifactSpec(text="changed")
    assert declaration_digest(registry, config) != first


@pytest.mark.parametrize("component", ["vm", "admin", "agent", "workspace"])
@pytest.mark.parametrize("declare", [None, ArtifactDefinitions()])
def test_empty_direct_surface_does_not_stale_existing_bundle_setup(db, component, declare):
    fixture = graph(db, active=(component,))
    fixture.save(component)
    owner = fixture.owners[component]
    owner = replace(owner, artifacts=owner.artifacts.model_copy(update={"declare": declare}))
    view = inspect_owner_artifacts(db, fixture.registry, owner, "shell")
    assert view.capture_status == "current" and view.status == "current"


@pytest.mark.parametrize("stage", ["missing", "stale", "current", "withdrawn"])
def test_inspection_distinguishes_direct_selection_from_captured_origin_without_payloads(
    db, monkeypatch, capsys, stage
):
    fixture = graph(db)
    template = fixture.registry.lookup("agent-template", "agent")
    direct = ArtifactDefinitions.model_validate(
        {
            "hints": {"agent-0": {"text": "PRIVATE-BODY"}, "new": {"text": "PRIVATE-NEW-BODY"}},
            "mcp_servers": {
                "tools": {
                    "definition": {
                        "type": "http",
                        "url": "https://fixture.invalid/PRIVATE-ENDPOINT",
                        "headers": {"Authorization": "PRIVATE-CREDENTIAL"},
                    }
                }
            },
        }
    )
    template = template.model_copy(update={"artifacts": template.artifacts.model_copy(update={"declare": direct})})
    fixture.registry.add("agent-template", "agent", template, Origin.built_in(source="direct-inspection"))
    if stage == "missing":
        db.instance_state.clear_applied_slice("agent", "agent", AppliedStateKey.ARTIFACT_INPUTS)
    elif stage in ("current", "withdrawn"):
        capture = capture_owner(fixture.registry, "agent", "agent", "agent", template.artifacts)
        write_capture(db, "agent", "agent", "agent", capture, operation="fixture")
        assert read_captures(db, "agent", "agent")["agent"] == capture
        if stage == "withdrawn":
            template = template.model_copy(
                update={"artifacts": template.artifacts.model_copy(update={"declare": None})}
            )
            fixture.registry.add("agent-template", "agent", template, Origin.built_in(source="direct-inspection"))

    def forbid(*args, **kwargs):
        raise AssertionError("inspection must not acquire sources")

    monkeypatch.setattr(PackageCapture, "capture", forbid)
    result = inspect_artifacts(db, fixture.registry, agent_name="agent")
    owner = result.owners[-1]
    assert owner.capture_status == ("stale" if stage == "withdrawn" else stage)
    rows = {item.entry: item for item in owner.artifacts}
    assert rows["agent-0"].declared_bundles == ("agent-bundle",)
    assert rows["agent-0"].declared_direct is (stage != "withdrawn")
    assert rows["agent-0"].selected_bundle == ("agent-bundle" if stage == "withdrawn" else None)
    assert rows["agent-0"].bundle == ("agent-bundle" if stage == "stale" else "")
    assert rows["tools"].native_name == "tools"
    assert rows["tools"].declared is (stage != "withdrawn")
    assert bool(rows["agent-0"].replacements) is (stage in ("current", "withdrawn"))
    data = inspection_data(result)
    render_artifacts(result)
    rendered = json.dumps(data) + capsys.readouterr().out
    for private in ("PRIVATE-BODY", "PRIVATE-NEW-BODY", "PRIVATE-ENDPOINT", "PRIVATE-CREDENTIAL"):
        assert private not in rendered


def test_direct_only_missing_capture_is_not_current(db):
    fixture = graph(db)
    owner = fixture.owners["agent"]
    owner = replace(
        owner, artifacts=ArtifactsConfig(declare=ArtifactDefinitions(hints={"note": HintArtifactSpec(text="context")}))
    )
    db.instance_state.clear_applied_slice("agent", "agent", AppliedStateKey.ARTIFACT_INPUTS)
    assert inspect_owner_artifacts(db, fixture.registry, owner).capture_status == "missing"
