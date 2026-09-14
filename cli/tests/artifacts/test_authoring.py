"""Authoring schemas preserve rule bodies and refuse metadata that cannot take effect."""

from dataclasses import replace

import pytest

from agentworks.artifacts.capture import content_from_members, text_content
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.declarations import AgentArtifactSpec, HintArtifactSpec, RuleArtifactSpec
from agentworks.artifacts.model import ArtifactInput, ArtifactMember, ArtifactOrigin, ArtifactProvenance, ArtifactType
from agentworks.artifacts.state import CapturedArtifacts, canonicalize_captures, read_captures, write_capture
from agentworks.plugins.claude import artifacts as claude
from agentworks.plugins.codex import artifacts as codex
from agentworks.plugins.grok import artifacts as grok
from agentworks.sources import SourceRefError
from tests.artifacts._fixtures import group, received
from tests.artifacts.test_capture import capture
from tests.artifacts.test_native_delivery import context


@pytest.mark.parametrize("file_source", [False, True])
@pytest.mark.parametrize("header", ["", "---\r\ndescription: Repository conventions\r\n---\r\n"])
def test_rule_capture_roundtrip_and_native_body(tmp_path, file_source, header):
    source = header + "Run relevant checks.\r\n"
    if file_source:
        path = tmp_path / "conventions.md"
        path.write_bytes(source.encode())
        spec = RuleArtifactSpec(source=str(path))
    else:
        spec = RuleArtifactSpec(text=source)
    inputs = capture(spec)
    item = inputs.rules["review"]
    assert item.content.text == "Run relevant checks.\n"
    assert item.content.description == ("Repository conventions" if header else "")
    assert item.content.metadata == ({"description": "Repository conventions"} if header else {})
    assert decode_inputs(encode_inputs(inputs)) == inputs
    for render in (claude.outer_artifacts, grok.outer_artifacts):
        files = render(received(item), "/home/user/.native").files
        assert len(files) == 1
        assert item.content.text.encode() in files[0].data
        assert b"description:" not in files[0].data
    native = codex.session_artifacts(context(item), configured=None, extra_args=())
    assert any("Run relevant checks." in arg for arg in native.argv)
    assert all("Repository conventions" not in arg for arg in native.argv)


@pytest.mark.parametrize(
    "header",
    [
        "description: []",
        "description: null",
        "description: ' '",
        "description: " + "x" * 1025,
        "globs: ['*.py']",
        "targets: ['codex']",
        "root: true",
        "localRoot: true",
        "native_options: {}",
        "descripton: typo",
        "description: ok\ndescription: replaced",
        "- description",
        "42: value",
        "description: &x value",
        "description: {nested: value}",
    ],
)
@pytest.mark.parametrize("file_source", [False, True])
def test_rule_rejects_unexpected_frontmatter(tmp_path, header, file_source):
    source = f"---\n{header}\n---\nprivate-body\n"
    path = tmp_path / "rule.md"
    path.write_text(source)
    spec = RuleArtifactSpec(source=str(path)) if file_source else RuleArtifactSpec(text=source)
    with pytest.raises(SourceRefError) as error:
        capture(spec)
    assert "private-body" not in str(error.value)


@pytest.mark.parametrize("source", ["---\ndescription: unfinished", "---\ndescription: valid\n---\n "])
def test_rule_requires_closed_frontmatter_and_nonempty_body(source):
    with pytest.raises(SourceRefError):
        capture(RuleArtifactSpec(text=source))


def test_hints_remain_plaintext_even_when_they_look_like_frontmatter():
    source = "---\nglobs: ['*.py']\n---\nA setup fact.\n"
    item = capture(HintArtifactSpec(text=source)).hints["review"]
    assert item.content.text == source and item.content.metadata == {}
    assert decode_inputs(encode_inputs(group(item))) == group(item)


@pytest.mark.parametrize(
    "extra",
    [
        "model: example",
        "claudecode: {model: example}",
        "targets: ['*']",
        "metadata: {}",
        "name: replacement",
        "native_options: []",
        "native_options: {codex: example}",
        "native_options: {codex: {model: first, model: second}}",
        "native_options: {codex: {1: value}}",
    ],
)
def test_agents_reject_unexpected_or_ambiguous_structure(tmp_path, extra):
    source = tmp_path / "review.md"
    source.write_text(f"---\nname: review\ndescription: Review code\n{extra}\n---\nprivate-body\n")
    with pytest.raises(SourceRefError) as error:
        capture(AgentArtifactSpec(source=str(source)))
    assert "private-body" not in str(error.value)


@pytest.mark.parametrize("artifact_type", [ArtifactType.RULE, ArtifactType.AGENT])
def test_existing_capture_keeps_its_interpretation_until_owner_refresh(db, tmp_path, artifact_type):
    db.insert_vm("box", site="local", hostname="box")
    source = tmp_path / "review.md"
    text = (
        "---\ndescription: Old literal text\n---\nBody.\n"
        if artifact_type == ArtifactType.RULE
        else "---\nname: review\ndescription: Review\nmodel: formerly-ignored\n---\nBody.\n"
    )
    source.write_text(text)
    member = ArtifactMember(source.name, text.encode(), text=True)
    old_content = content_from_members(artifact_type, "review", (member,), str(source), legacy=True)
    item = ArtifactInput(
        old_content,
        ArtifactProvenance(source=str(source)),
        ArtifactOrigin("vm", "vm", "box", bundle="team", entry="review"),
    )
    snapshot = CapturedArtifacts("a" * 64, group(item), codec_version=2)
    write_capture(db, "vm", "box", "vm", snapshot, operation="fixture")
    source.unlink()  # Reading, rewriting a sibling, and backup must not recapture.
    assert read_captures(db, "vm", "box")["vm"] == snapshot
    admin = CapturedArtifacts("b" * 64, group(owner=replace(item.origin.owner, component="admin")))
    write_capture(db, "vm", "box", "admin", admin, operation="fixture")
    record = db.instance_state.get_applied_slices("vm", "box")[0]
    assert canonicalize_captures(record) == record.payload
    assert read_captures(db, "vm", "box") == {"vm": snapshot, "admin": admin}
    assert decode_inputs(encode_inputs(snapshot.inputs, version=2)) == snapshot.inputs
    with pytest.raises(SourceRefError):
        encode_inputs(snapshot.inputs)  # Old bytes cannot masquerade as a new capture.
    if artifact_type == ArtifactType.RULE:
        fresh = replace(item, content=content_from_members(artifact_type, "review", (member,), str(source)))
        write_capture(db, "vm", "box", "vm", CapturedArtifacts("a" * 64, group(fresh)), operation="refresh")
        assert read_captures(db, "vm", "box")["vm"].inputs.rules["review"].content.text == "Body.\n"
    else:
        with pytest.raises(SourceRefError):
            content_from_members(artifact_type, "review", (member,), str(source))


def test_inline_rule_persisted_metadata_is_validated_even_with_matching_hashes():
    item = ArtifactInput(
        text_content(ArtifactType.RULE, "review", "Run checks."),
        ArtifactProvenance(),
        ArtifactOrigin("agent", "agent", "user", bundle="team", entry="review"),
    )
    for content in (
        replace(item.content, metadata_json='{"targets":["codex"]}'),
        replace(item.content, description="Invented description"),
        replace(item.content, metadata_json='{"description":[]}'),
    ):
        with pytest.raises(SourceRefError):
            encode_inputs(group(replace(item, content=content)))


def test_inspection_exposes_captured_description_without_body_or_source_access(db, monkeypatch, capsys):
    from agentworks.artifacts.inspection import inspect_artifacts, inspection_data, render_artifacts
    from tests.artifacts.test_routing import graph

    fixture = graph(db)
    previous = fixture.captures["agent"]
    seed = next(previous.inputs.items())
    item = replace(
        seed,
        content=text_content(
            ArtifactType.RULE, seed.content.name, "---\ndescription: Review conventions\n---\nPrivate instructions.\n"
        ),
    )
    write_capture(db, "agent", "agent", "agent", replace(previous, inputs=group(item)), operation="fixture")

    def forbid(*args, **kwargs):
        raise AssertionError("inspection must not fetch sources")

    monkeypatch.setattr("agentworks.artifacts.state.capture_owner", forbid)
    result = inspect_artifacts(db, fixture.registry, agent_name="agent")
    assert result.owners[-1].artifacts[0].description == "Review conventions"
    data = inspection_data(result)
    assert "Review conventions" in str(data)
    assert "Private instructions." not in str(data)
    render_artifacts(result)
    rendered = capsys.readouterr().out
    assert "Review conventions" in rendered and "Private instructions." not in rendered
