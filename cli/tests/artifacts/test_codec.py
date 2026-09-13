"""Persisted artifact semantics are validated independently of content hashes."""

from __future__ import annotations

import base64
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest

from agentworks.artifacts.bundle import ArtifactBundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.declarations import AgentArtifactSpec, SkillArtifactSpec
from agentworks.artifacts.model import ArtifactInput, ArtifactMember, ArtifactOrigin, ArtifactType
from agentworks.sources import SourceRefError
from tests.artifacts._fixtures import group


def captured_skill(tmp_path: Path) -> ArtifactInput:
    root = tmp_path / "review"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: review\ndescription: Review code\n---\nRead it.\n")
    return capture_artifacts(
        [("team", ArtifactBundle(name="team", skills={"review": SkillArtifactSpec(source=str(root))}))],
        ArtifactOrigin("agent", "agent", "worker"),
    ).skills["review"]


def altered_payload(original: ArtifactInput, altered: ArtifactInput) -> dict[str, object]:
    """Tamper with a readable wire record, retaining hashes valid for the altered values."""
    payload = encode_inputs(group(original))
    row = cast("dict[str, dict[str, object]]", payload[original.content.type.value + "s"])[original.content.name]
    row["content"] = {
        **asdict(altered.content),
        "digest": altered.content.digest,
        "members": [
            {**asdict(member), "data": base64.b64encode(member.data).decode("ascii")}
            for member in altered.content.members
        ],
    }
    row["provenance"] = asdict(altered.provenance)
    row["identity"] = altered.identity
    return payload


@pytest.mark.parametrize("field", ["hooks", "mcpServers", "mcp_servers"])
def test_decode_rejects_execution_metadata_with_valid_hashes(tmp_path: Path, field: str) -> None:
    item = captured_skill(tmp_path)
    member = item.content.members[0]
    data = member.data.replace(b"description:", f"{field}: {{command: external-command}}\ndescription:".encode())
    altered = replace(item, content=replace(item.content, members=(replace(member, data=data),)))
    with pytest.raises(SourceRefError):
        decode_inputs(altered_payload(item, altered))


@pytest.mark.parametrize("field", ["name", "description", "text", "metadata_json", "native_options_json"])
def test_decode_rejects_fields_inconsistent_with_entrypoint(tmp_path: Path, field: str) -> None:
    item = captured_skill(tmp_path)
    value = '{"unexpected":true}' if field.endswith("_json") else "different"
    altered = replace(item, content=replace(item.content, **{field: value}))
    with pytest.raises(SourceRefError):
        decode_inputs(altered_payload(item, altered))


@pytest.mark.parametrize("change", ["missing", "renamed", "binary", "unnormalized"])
def test_decode_requires_normalized_skill_entrypoint(tmp_path: Path, change: str) -> None:
    item = captured_skill(tmp_path)
    member = item.content.members[0]
    members: tuple[ArtifactMember, ...]
    if change == "missing":
        members = ()
    elif change == "renamed":
        members = (replace(member, path="OTHER.md"),)
    elif change == "binary":
        members = (replace(member, text=False),)
    else:
        members = (replace(member, data=member.data.replace(b"\n", b"\r\n"), text=False),)
    altered = replace(item, content=replace(item.content, members=members))
    with pytest.raises(SourceRefError):
        decode_inputs(altered_payload(item, altered))


@pytest.mark.parametrize(
    "source",
    [
        "https://user:private-token@example.com/repo.git",
        "https://example.com/repo.git?access_token=private-token",
        "https://example.com/repo.git#private-token",
        "git::https://user:private-token@example.com/repo.git",
    ],
)
def test_decode_rejects_credential_bearing_provenance(tmp_path: Path, source: str) -> None:
    item = captured_skill(tmp_path)
    altered = replace(item, provenance=replace(item.provenance, source=source))
    assert altered.identity == item.identity
    with pytest.raises(SourceRefError) as error:
        decode_inputs(altered_payload(item, altered))
    assert "private-token" not in str(error.value)


def test_decode_accepts_git_provenance_without_source_access(tmp_path: Path) -> None:
    item = captured_skill(tmp_path)
    altered = replace(
        item,
        provenance=replace(
            item.provenance,
            source="https://example.invalid/team/repo.git",
            selected_path="skills/review",
            requested_ref="main",
            commit="a" * 40,
        ),
    )
    assert decode_inputs(encode_inputs(group(altered))) == group(altered)


@pytest.mark.parametrize("field", ["hooks", "mcpServers", "mcp_servers"])
def test_agent_capture_and_decode_reject_nested_execution_metadata(tmp_path: Path, field: str) -> None:
    source = tmp_path / "review.md"
    safe = "---\nname: review\ndescription: Review code\n---\nRead it.\n"
    source.write_text(safe)
    item = capture_artifacts(
        [("team", ArtifactBundle(name="team", agents={"review": AgentArtifactSpec(source=str(source))}))],
        ArtifactOrigin("agent", "agent", "worker"),
    ).agents["review"]
    assert item.content.type == ArtifactType.AGENT
    unsafe = safe.replace(
        "description:", f"metadata:\n  nested:\n    {field}: {{command: external-command}}\ndescription:"
    )
    source.write_text(unsafe)
    with pytest.raises(SourceRefError):
        capture_artifacts(
            [("team", ArtifactBundle(name="team", agents={"review": AgentArtifactSpec(source=str(source))}))],
            ArtifactOrigin("agent", "agent", "worker"),
        )
    altered = replace(
        item, content=replace(item.content, members=(replace(item.content.members[0], data=unsafe.encode()),))
    )
    with pytest.raises(SourceRefError):
        decode_inputs(altered_payload(item, altered))
