"""Whole-entry replacement and original-owner groups at the artifact boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agentworks.artifacts.bundle import ArtifactBundle, resolve_bundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.declarations import ArtifactsConfig, HintArtifactSpec, RuleArtifactSpec, SkillArtifactSpec
from agentworks.artifacts.inspection import inspect_artifacts, inspection_data
from agentworks.artifacts.model import ArtifactGroup, ArtifactInput, ArtifactOrigin, ArtifactType
from agentworks.artifacts.native.shell import shell_artifacts
from agentworks.artifacts.state import capture_owner, write_capture
from agentworks.errors import ConfigError
from agentworks.manifests.emit import document_schema
from agentworks.origin import Origin
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.grok import artifacts as grok
from agentworks.resources.registry import Registry
from agentworks.sources import SourceRefError
from tests.artifacts._fixtures import received
from tests.artifacts.test_native_delivery import artifact, context, owned
from tests.artifacts.test_routing import graph
from tests.conftest import registry_with_shell


def registry_with(*bundles: ArtifactBundle) -> Registry:
    registry = registry_with_shell()
    for bundle in bundles:
        registry.add("artifact-bundle", bundle.name, bundle, Origin.built_in(source="type-maps-test"))
    return registry


def test_bundle_inheritance_replaces_whole_entry_and_never_acquires_discarded_source(tmp_path):
    source = tmp_path / "rule.md"
    source.write_text("child file")
    parent = ArtifactBundle(
        name="parent",
        hints={"discarded": HintArtifactSpec(source=str(tmp_path / "missing.md"))},
        rules={"same": RuleArtifactSpec(text="parent inline"), "kept": RuleArtifactSpec(text="kept")},
    )
    child = ArtifactBundle(
        name="child",
        inherits=["parent"],
        hints={"discarded": HintArtifactSpec(text="child inline")},
        rules={"same": RuleArtifactSpec(source=str(source))},
    )
    registry = registry_with(parent, child)
    registry.finalize()
    resolved = resolve_bundle(registry, "child")
    assert list(resolved.value.rules) == ["same", "kept"]
    assert resolved.value.rules["same"].text is None
    assert resolved.value.rules["same"].source == str(source)
    assert resolved.value.hints["discarded"].source is None
    captured = capture_owner(registry, "session", "run", "session", ArtifactsConfig(bundles=["child"]))
    assert captured.inputs.rules["same"].content.text == "child file"
    assert captured.inputs.hints["discarded"].content.text == "child inline"
    assert not captured.inputs.hints["discarded"].replacements


def test_later_bundle_replaces_only_its_type_and_key_and_retains_order_and_provenance(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.capture.output.warn", warnings.append)
    first = ArtifactBundle(
        name="first",
        hints={"same": HintArtifactSpec(text="old"), "kept": HintArtifactSpec(text="kept")},
        rules={"same": RuleArtifactSpec(text="rule")},
    )
    second = ArtifactBundle(
        name="second", hints={"same": HintArtifactSpec(text="new"), "last": HintArtifactSpec(text="last")}
    )
    snapshot = capture_artifacts(
        [(first.name, first), (second.name, second)], ArtifactOrigin("agent", "agent", "alice")
    )
    assert list(snapshot.hints) == ["same", "kept", "last"]
    assert snapshot.hints["same"].content.text == "new"
    assert snapshot.rules["same"].content.text == "rule"
    assert [item.content.type for item in snapshot.items()] == [ArtifactType.HINT] * 3 + [ArtifactType.RULE]
    assert snapshot.hints["same"].origin.bundle == "second"
    assert snapshot.hints["same"].replacements[0].origin.bundle == "first"
    assert len(warnings) == 1
    assert decode_inputs(encode_inputs(snapshot)) == (3, snapshot)
    with pytest.raises(TypeError):
        cast(dict[str, ArtifactInput], snapshot.hints)["other"] = snapshot.hints["same"]


def test_identical_override_is_quiet_and_bundle_provenance_is_not_identity(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.artifacts.capture.output.warn", warnings.append)
    first = ArtifactBundle(name="first", hints={"same": HintArtifactSpec(text="unchanged")})
    second = ArtifactBundle(name="second", hints=first.hints)
    origin = ArtifactOrigin("session", "session", "run")
    original = capture_artifacts([(first.name, first)], origin)
    replaced = capture_artifacts([(first.name, first), (second.name, second)], origin)
    assert replaced.hints["same"].identity == original.hints["same"].identity
    assert replaced.hints["same"].origin_identity == original.hints["same"].origin_identity
    assert replaced.hints["same"].origin.bundle == "second"
    assert len(replaced.hints["same"].replacements) == 1
    assert warnings == []


def test_replaced_skill_keeps_only_the_later_complete_directory(tmp_path):
    bundles = []
    for label in ("first", "second"):
        root = tmp_path / label / "review"
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text("---\nname: review\ndescription: Review\n---\nReview\n")
        (root / f"{label}.txt").write_text(label)
        bundles.append((label, ArtifactBundle(name=label, skills={"review": SkillArtifactSpec(source=str(root))})))
    snapshot = capture_artifacts(bundles, ArtifactOrigin("session", "session", "run"))
    assert {member.path for member in snapshot.skills["review"].content.members} == {"SKILL.md", "second.txt"}


@pytest.mark.parametrize("kind", ["skills", "agents"])
def test_canonical_map_key_must_match_source_frontmatter(tmp_path, kind):
    path = tmp_path / "review"
    path.mkdir()
    source = path / "SKILL.md" if kind == "skills" else path / "review.md"
    source.write_text("---\nname: review\ndescription: Review\n---\nReview\n")
    bundle = ArtifactBundle.model_validate(
        {"name": "team", kind: {"different": {"source": str(path if kind == "skills" else source)}}}
    )
    with pytest.raises(SourceRefError):
        capture_artifacts([("team", bundle)], ArtifactOrigin("session", "session", "run"))


def test_wire_map_owner_type_and_key_must_match_content():
    captured = capture_artifacts(
        [("team", ArtifactBundle(name="team", hints={"note": HintArtifactSpec(text="note")}))],
        ArtifactOrigin("session", "session", "run"),
    )
    for change in ("key", "type", "owner"):
        payload = cast(dict[str, Any], encode_inputs(captured))
        if change == "key":
            payload["hints"]["renamed"] = payload["hints"].pop("note")
        elif change == "type":
            payload["rules"] = payload["hints"]
            payload["hints"] = {}
        else:
            payload["owner"]["resource_name"] = "another"
        with pytest.raises(SourceRefError):
            decode_inputs(payload)


def test_group_boundary_rejects_mismatched_identity():
    item = artifact(ArtifactType.HINT)
    for owner, maps in (
        (replace(item.origin.owner, resource_name="other"), {"hints": {"review": item}}),
        (item.origin.owner, {"rules": {"review": item}}),
        (item.origin.owner, {"hints": {"wrong": item}}),
    ):
        with pytest.raises(ValueError):
            ArtifactGroup(owner, **maps)


def test_same_key_from_three_scopes_survives_vm_user_session_routing(db):
    fixture = graph(db, active=("agent",))
    for component in ("vm", "agent", "workspace"):
        bundle = fixture.registry.lookup("artifact-bundle", f"{component}-bundle")
        bundle.hints.clear()
        bundle.hints["shared"] = HintArtifactSpec(text=component)
        owner = fixture.owners[component]
        captured = capture_owner(fixture.registry, owner.kind, owner.name, component, owner.artifacts)
        fixture.captures[component] = captured
        write_capture(db, owner.kind, owner.name, component, captured, operation="fixture")
    fixture.save("agent", inherited=fixture.captures["vm"].inputs, routes={"shared": "session"})
    routed = fixture.route().inputs
    assert [group.owner.component for group in routed.deferred.values()] == ["vm", "agent", "workspace"]
    assert [group.hints["shared"].content.text for group in routed.deferred.values()] == ["vm", "agent", "workspace"]
    assert len({item.identity for item in routed.items()}) == 3
    application = shell_artifacts(routed, "/private", session=True)
    assert len({file.path for file in application.files}) == 4
    index = json.loads(application.files[-1].data)
    assert [entry["owner"]["component"] for entry in index["groups"]] == ["vm", "agent", "workspace"]


@pytest.mark.parametrize("adapter", [claude, grok])
def test_native_rule_and_hint_aggregation_preserves_same_keys_from_different_groups(adapter):
    items = [artifact(kind, owner=owner) for owner in ("one", "two") for kind in (ArtifactType.HINT, ArtifactType.RULE)]
    application = adapter.outer_artifacts(received(*items), "/native")
    assert len(application.files) == 2
    for file in application.files:
        assert len(file.origins) == 2
        assert file.data.count(items[0].content.text.encode()) == 2


@pytest.mark.parametrize("adapter", [claude, codex, grok])
@pytest.mark.parametrize("kind", [ArtifactType.SKILL, ArtifactType.AGENT])
def test_native_ancestor_name_conflict_is_refused_with_no_session_inputs(adapter, kind):
    user = artifact(kind, owner="user")
    project = artifact(kind, owner="workspace")
    if adapter is codex:
        user_files = adapter.outer_artifacts(
            received(user),
            skills_root="/home/.agents/skills",
            agents_root="/home/.codex/agents",
            instructions_path="/home/.codex/AGENTS.md",
        ).files
        project_files = adapter.outer_artifacts(
            received(project),
            skills_root="/project/.agents/skills",
            agents_root="/project/.codex/agents",
            instructions_path="/project/AGENTS.md",
        ).files
    else:
        user_files = adapter.outer_artifacts(received(user), "/home/.native").files
        project_files = adapter.outer_artifacts(received(project), "/project/.native").files
    with pytest.raises(ConfigError):
        adapter.session_artifacts(
            context(ancestors=tuple(owned(file) for file in (*user_files, *project_files))),
            configured=None,
            extra_args=[],
        )


def test_inspection_retains_compact_replacement_evidence(db):
    fixture = graph(db)
    second = ArtifactBundle(name="later", hints={"agent-0": HintArtifactSpec(text="private replacement content")})
    fixture.registry.add("artifact-bundle", "later", second, Origin.built_in(source="test"))
    template = fixture.registry.lookup("agent-template", "agent")
    template.artifacts.bundles.append("later")
    snapshot = capture_owner(fixture.registry, "agent", "agent", "agent", template.artifacts)
    write_capture(db, "agent", "agent", "agent", snapshot, operation="fixture")
    metadata = json.loads(json.dumps(inspection_data(inspect_artifacts(db, fixture.registry, agent_name="agent"))))
    row = metadata["owners"][-1]["artifacts"][0]
    assert row["bundle"] == "later"
    assert row["replacements"][0]["bundle"] == "agent-bundle"
    assert "private replacement content" not in json.dumps(metadata)


def test_generated_bundle_schema_accepts_maps_and_rejects_old_flat_entries():
    schema = Draft202012Validator(document_schema("artifact-bundle"))
    document = {
        "apiVersion": "agentworks/v1",
        "kind": "artifact-bundle",
        "metadata": {"name": "team"},
        "spec": {"hints": {"note": {"text": "context"}}},
    }
    schema.validate(document)
    document["spec"] = {"artifacts": {"note": {"type": "hint", "text": "context"}}}
    assert list(schema.iter_errors(document))
    with pytest.raises(ValidationError):
        ArtifactBundle.model_validate({"name": "team", "hints": {"note": {"type": "hint", "text": "context"}}})


@pytest.mark.parametrize("name", ["/", "Upper", "", "trailing/", "a_b", "a--b", "x" * 65])
def test_persisted_type_map_names_are_canonical_even_when_key_and_content_agree(name):
    captured = capture_artifacts(
        [("team", ArtifactBundle(name="team", hints={"note": HintArtifactSpec(text="note")}))],
        ArtifactOrigin("session", "session", "run"),
    )
    payload = cast(dict[str, Any], encode_inputs(captured))
    record = payload["hints"].pop("note")
    record["content"]["name"] = record["origin"]["entry"] = name
    payload["hints"][name] = record
    with pytest.raises(SourceRefError):
        decode_inputs(payload)


@pytest.mark.parametrize("stage", ["before-capture", "stale-capture", "current-capture"])
def test_inspection_separates_current_bundle_selection_from_captured_replacements(db, monkeypatch, capsys, stage):
    from agentworks.artifacts.inspection import render_artifacts
    from agentworks.db import AppliedStateKey

    fixture = graph(db)
    second = ArtifactBundle(name="selected-bundle", hints={"agent-0": HintArtifactSpec(text="uncaptured private body")})
    fixture.registry.add("artifact-bundle", second.name, second, Origin.built_in(source="test"))
    template = fixture.registry.lookup("agent-template", "agent")
    template.artifacts.bundles.append(second.name)
    if stage == "before-capture":
        db.instance_state.clear_applied_slice("agent", "agent", AppliedStateKey.ARTIFACT_INPUTS)
    elif stage == "current-capture":
        snapshot = capture_owner(fixture.registry, "agent", "agent", "agent", template.artifacts)
        write_capture(db, "agent", "agent", "agent", snapshot, operation="fixture")

    def forbid(*args, **kwargs):
        raise AssertionError("inspection must not acquire sources")

    monkeypatch.setattr("agentworks.artifacts.capture.capture_artifacts", forbid)
    monkeypatch.setattr("agentworks.package_sources.PackageCapture.capture", forbid)
    result = inspect_artifacts(db, fixture.registry, agent_name="agent")
    rows = result.owners[-1].artifacts
    assert len(rows) == 1
    assert rows[0].declared_bundles == ("agent-bundle", "selected-bundle")
    assert rows[0].bundle == ("agent-bundle" if stage == "stale-capture" else "selected-bundle")
    assert bool(rows[0].replacements) == (stage == "current-capture")
    assert (rows[0].input_id is None) == (stage == "before-capture")
    data = inspection_data(result)
    encoded = json.dumps(data)
    assert json.loads(encoded)["owners"][-1]["artifacts"][0]["declared_bundles"] == ["agent-bundle", "selected-bundle"]
    render_artifacts(result)
    human = capsys.readouterr().out
    assert "agent-bundle" in human and "selected-bundle" in human
    assert "uncaptured private body" not in human + encoded
