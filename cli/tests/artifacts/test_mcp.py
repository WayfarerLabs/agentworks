"""Canonical MCP schema, acquisition and persistence boundaries."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agentworks.artifacts.bundle import ArtifactBundle, resolve_bundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.declarations import ArtifactsConfig, MCPServerArtifactSpec, RuleArtifactSpec
from agentworks.artifacts.mcp import content_from_definition, parse_definition
from agentworks.artifacts.model import ArtifactGroup, ArtifactOrigin, ArtifactType
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.state import capture_owner, declaration_digest, read_captures, write_capture
from agentworks.origin import Origin
from agentworks.package_sources import CaptureLimits, PackageCapture
from agentworks.sources import SourceRefError
from tests.artifacts._fixtures import group
from tests.artifacts.test_type_maps import registry_with

STDIO = {"type": "stdio", "command": "inventory-mcp", "env": {"TOKEN": "${TOKEN}"}}
HTTP = {"type": "http", "url": "https://example.test/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}}
OWNER = ArtifactOrigin("agent", "agent", "worker")


def capture(spec: MCPServerArtifactSpec, *, limits: CaptureLimits | None = None) -> ArtifactGroup:
    bundle = ArtifactBundle(name="team", mcp_servers={"inventory": spec})
    return capture_artifacts([("team", bundle)], OWNER, limits=limits)


def inline(value: object = STDIO) -> MCPServerArtifactSpec:
    return MCPServerArtifactSpec.model_validate({"definition": value})


@pytest.mark.parametrize("definition", [STDIO, HTTP])
def test_inline_capture_is_normalized_unresolved_metadata(definition, monkeypatch):
    monkeypatch.setenv("TOKEN", "must-not-enter-artifacts")
    inputs = capture(inline(definition))
    item = inputs.mcp_servers["inventory"]
    assert item.content.type is ArtifactType.MCP_SERVER
    assert item.content.members == () and item.content.text == ""
    assert item.content.native_options == {}
    assert "must-not-enter-artifacts" not in json.dumps(encode_inputs(inputs))
    expected = {"args": [], **STDIO} if definition == STDIO else HTTP
    assert item.content.metadata == expected
    mutable = item.content.metadata
    mutable["type"] = "changed"
    assert item.content.metadata == expected
    assert decode_inputs(encode_inputs(inputs)) == (4, inputs)


@pytest.mark.parametrize(
    "definition",
    [
        {},
        {"type": "sse", "url": "https://example.test"},
        {**STDIO, "command": " "},
        {**STDIO, "command": ["server"]},
        {**STDIO, "command": "${COMMAND}"},
        {**STDIO, "args": [3]},
        {**STDIO, "args": ["${ARG}"]},
        {**STDIO, "args": ["$(whoami)"]},
        {**STDIO, "env": {"TOKEN": "${OTHER}"}},
        {**STDIO, "env": {"TOKEN": "${TOKEN:-fallback}"}},
        {**STDIO, "env": {"TOKEN": {"secret": "token"}}},
        {**STDIO, "env": {"BAD-NAME": "public"}},
        {**STDIO, "env": {"TOKEN": "prefix-${TOKEN}"}},
        {**STDIO, "url": "https://example.test"},
        {**STDIO, "headers": {}},
        {**HTTP, "command": "server"},
        {**HTTP, "env": {}},
        {**HTTP, "url": "/relative"},
        {**HTTP, "url": "https://user:password@example.test/mcp"},
        {**HTTP, "url": "https://${HOST}/mcp"},
        {**HTTP, "url": "https://example.test:99999/mcp"},
        {**HTTP, "headers": {"Authorization": "Bearer ${TOKEN}", "authorization": "public"}},
        {**HTTP, "headers": {"Bad Name": "public"}},
        {**HTTP, "headers": {"X-Token": "Bearer ${TOKEN}"}},
        {**HTTP, "headers": {"X-Token": "${TOKEN} suffix"}},
        {**HTTP, "headers": {"X-Token": "header\r\ninjection"}},
        {**STDIO, "description": ""},
        {**STDIO, "description": "bad\x00description"},
        {**STDIO, "args": ["bad\x7fargument"]},
    ]
    + [
        {**STDIO, field: {}}
        for field in ("oauth", "auth", "tools", "allowedTools", "cwd", "disabled", "native_options")
    ],
)
def test_definition_rejects_unsupported_structure_at_both_boundaries(definition):
    with pytest.raises(ValidationError):
        inline(definition)
    with pytest.raises(SourceRefError):
        parse_definition(definition)


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"definition": STDIO, "source": "mcp.json"},
        {"definition": STDIO, "preserve_bytes": []},
        {"source": "git::https://user:private-token@example.test/repo.git//mcp.json"},
    ],
)
def test_wrapper_requires_exactly_one_safe_input(entry):
    with pytest.raises(ValidationError) as error:
        MCPServerArtifactSpec.model_validate(entry)
    assert "private-token" not in str(error.value)


def test_rejected_discriminator_values_do_not_escape_in_declaration_or_source_errors(tmp_path):
    rejected = {"type": "private-token", "command": "server"}
    with pytest.raises(ValidationError) as declaration_error:
        inline(rejected)
    assert "private-token" not in str(declaration_error.value)
    source = tmp_path / "mcp.json"
    source.write_text(json.dumps({"mcpServers": {"inventory": rejected}}))
    with pytest.raises(SourceRefError) as source_error:
        capture(MCPServerArtifactSpec(source=str(source)))
    assert "private-token" not in str(source_error.value)


def test_public_literals_and_all_three_reference_forms_are_preserved():
    stdio = parse_definition(
        {**STDIO, "args": ["", "--read", "a path"], "env": {"PUBLIC": "value", "TOKEN": "${TOKEN}"}}
    )
    http = parse_definition(
        {**HTTP, "headers": {"X-Token": "${OTHER}", "Authorization": "Bearer ${TOKEN}", "X-Public": "ok"}}
    )
    assert stdio.model_dump()["env"] == {"PUBLIC": "value", "TOKEN": "${TOKEN}"}
    assert http.model_dump()["headers"] == {"X-Token": "${OTHER}", "Authorization": "Bearer ${TOKEN}", "X-Public": "ok"}


@pytest.mark.windows
def test_jsonc_selects_only_same_named_entry_and_source_noise_does_not_change_identity(tmp_path):
    source = tmp_path / "mcp.jsonc"
    source.write_bytes(
        b'{\r\n// note\r\n"$schema":"https://schema.invalid/not-fetched",\r\n"mcpServers": {'
        b'"inventory":{"type":"stdio","command":"inventory-mcp","env":{"TOKEN":"${TOKEN}"}},'
        b'"unselected":{"unknown":"private-unused-body", "oauth": true},},}\r\n'
    )
    inputs = capture(MCPServerArtifactSpec(source=str(source)))
    item = inputs.mcp_servers["inventory"]
    assert item.content == capture(inline()).mcp_servers["inventory"].content
    assert item.content.members == ()
    assert "private-unused-body" not in json.dumps(encode_inputs(inputs))
    source.write_text(json.dumps({"mcpServers": {"inventory": STDIO, "unselected": 42}}))
    repeated = capture(MCPServerArtifactSpec(source=str(source)))
    assert repeated.mcp_servers["inventory"].identity == item.identity
    source.unlink()
    assert decode_inputs(encode_inputs(inputs)) == (4, inputs)


@pytest.mark.parametrize(
    "document",
    [
        "{invalid",
        "[]",
        '{"mcpServers": []}',
        '{"mcpServers": {}, "extra": true}',
        '{"mcpServers": {}, "$schema": 1}',
        '{"mcpServers": {"other": {}}}',
        '{"mcpServers": {}, "mcpServers": {}}',
        '{"mcpServers": {"other": {"x": 1, "x": 2}}}',
        '{"mcpServers": {"other": {"x": 1, "\\u0078": 2}}}',
        '{"mcpServers": {"other": 1/**/2}}',
        '{"mcpServers": {"inventory": {"type":"stdio", "command":"server", "oauth": {}}}}',
    ],
)
def test_source_rejects_syntax_root_duplicates_and_selected_semantics(tmp_path, document):
    source = tmp_path / "mcp.jsonc"
    source.write_text(document)
    with pytest.raises(SourceRefError):
        capture(MCPServerArtifactSpec(source=str(source)))


@pytest.mark.windows
def test_source_file_is_distinguished_from_one_file_directory_and_bounds_apply(tmp_path):
    directory = tmp_path / "package"
    directory.mkdir()
    source = directory / "mcp.json"
    source.write_text(json.dumps({"mcpServers": {"inventory": STDIO}}))
    with PackageCapture() as operation:
        assert operation.capture(str(source)).is_directory is False
        assert operation.capture(str(directory)).is_directory is True
    assert capture(MCPServerArtifactSpec(source=str(source))).mcp_servers
    with pytest.raises(SourceRefError):
        capture(MCPServerArtifactSpec(source=str(directory)))
    with pytest.raises(SourceRefError):
        capture(MCPServerArtifactSpec(source=str(source)), limits=CaptureLimits(member_bytes=10))
    with pytest.raises(SourceRefError):
        capture(inline(), limits=CaptureLimits(member_bytes=10))


def test_source_normalized_definition_obeys_the_same_member_limit(tmp_path):
    source = tmp_path / "mcp.json"
    definition = {"type": "stdio", "command": "server-" + "\u00e9" * 100}
    source.write_text(json.dumps({"mcpServers": {"inventory": definition}}, ensure_ascii=False), encoding="utf-8")
    source_size = source.stat().st_size
    assert len(content_from_definition("inventory", parse_definition(definition)).metadata_json.encode()) > source_size
    with pytest.raises(SourceRefError):
        capture(MCPServerArtifactSpec(source=str(source)), limits=CaptureLimits(member_bytes=source_size))


def test_inheritance_replaces_the_whole_connection_and_never_opens_discarded_source(tmp_path):
    parent = ArtifactBundle(
        name="parent", mcp_servers={"inventory": MCPServerArtifactSpec(source=str(tmp_path / "missing"))}
    )
    child = ArtifactBundle(name="child", inherits=["parent"], mcp_servers={"inventory": inline(HTTP)})
    registry = registry_with(parent, child)
    registry.finalize()
    resolved = resolve_bundle(registry, "child").value
    assert resolved.mcp_servers["inventory"].source is None
    assert resolved.mcp_servers["inventory"].definition is not None
    result = capture_owner(registry, "agent", "worker", "agent", ArtifactsConfig(bundles=["child"]))
    assert result.inputs.mcp_servers["inventory"].content.metadata == HTTP


def test_native_name_uses_full_original_owner_and_key_but_not_content_or_bundle():
    item = capture(inline()).mcp_servers["inventory"]
    changed = replace(item, content=content_from_definition("inventory", parse_definition(HTTP)))
    assert mcp_native_name(changed) == mcp_native_name(item)
    assert mcp_native_name(replace(item, origin=replace(item.origin, bundle="elsewhere"))) == mcp_native_name(item)
    assert mcp_native_name(replace(item, origin=replace(item.origin, component="workspace"))) != mcp_native_name(item)
    prefix = "a" * 23 + "-"
    first = replace(item, content=replace(item.content, name=prefix + "one"))
    second = replace(item, content=replace(item.content, name=prefix + "two"))
    assert mcp_native_name(first).startswith("agentworks-" + "a" * 23 + "-")
    assert mcp_native_name(first) != mcp_native_name(second)
    assert len(mcp_native_name(first)) <= 56


@pytest.mark.parametrize("version", [2, 3])
def test_legacy_captures_retain_four_maps_and_hashes_and_reject_mcp(version):
    bundle = ArtifactBundle(name="team", rules={"baseline": RuleArtifactSpec(text="Always test.")})
    inputs = capture_artifacts([("team", bundle)], OWNER)
    payload = encode_inputs(inputs, version=version)
    assert "mcp_servers" not in payload
    assert decode_inputs(payload) == (version, inputs)
    assert encode_inputs(decode_inputs(payload)[1], version=version) == payload
    assert inputs.rules["baseline"].content.digest == "fd4cb6ca57ea31de694e85b08dbfca9936020a2a5f1eb52a0c542bff13c06a4b"
    with pytest.raises(SourceRefError):
        encode_inputs(capture(inline()), version=version)
    payload["mcp_servers"] = {}
    with pytest.raises(SourceRefError):
        decode_inputs(payload)


def test_empty_mcp_map_does_not_stale_legacy_declaration_digest():
    bundle = ArtifactBundle(name="team", rules={"baseline": RuleArtifactSpec(text="Always test.")})
    registry = registry_with(bundle)
    expected = declaration_digest(registry, ArtifactsConfig(bundles=["team"]))
    assert expected == "a861d51d2f535f78e5baf4b2bad909dd3d32bd1cf047bff57c9f2a09e3d8c6e8"
    registry.add(
        "artifact-bundle",
        "mcp",
        ArtifactBundle(name="mcp", mcp_servers={"inventory": inline()}),
        Origin.built_in(source="mcp-test"),
    )
    assert declaration_digest(registry, ArtifactsConfig(bundles=["team", "mcp"])) != expected


def test_semantic_forgery_with_matching_hashes_is_rejected():
    inputs = capture(inline())
    item = inputs.mcp_servers["inventory"]
    forged = replace(
        item, content=replace(item.content, metadata_json='{"command":"server","cwd":"/tmp","type":"stdio"}')
    )
    payload = encode_inputs(inputs)
    record = cast("dict[str, Any]", payload["mcp_servers"])["inventory"]
    record["content"]["metadata_json"] = forged.content.metadata_json
    record["content"]["digest"] = forged.content.digest
    record["identity"] = forged.identity
    with pytest.raises(SourceRefError):
        decode_inputs(payload)
    with pytest.raises(SourceRefError):
        encode_inputs(group(forged))


def test_mcp_database_capture_round_trip_uses_v4(db):
    db.insert_vm("box", site="local", hostname="box")
    bundle = ArtifactBundle(name="team", mcp_servers={"inventory": inline()})
    registry = registry_with(bundle)
    snapshot = capture_owner(registry, "vm", "box", "vm", ArtifactsConfig(bundles=["team"]))
    write_capture(db, "vm", "box", "vm", snapshot, operation="fixture")
    assert read_captures(db, "vm", "box") == {"vm": snapshot}
    assert snapshot.codec_version == 4


@pytest.mark.windows
def test_git_file_capture_uses_committed_selected_entry_and_tracks_revision(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repository), *args], text=True).strip()

    git("init", "--quiet")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "MCP fixture")
    (repository / "package").mkdir()
    source = repository / "package" / "mcp.json"
    source.write_text(json.dumps({"mcpServers": {"inventory": HTTP, "other": {"unknown": "unused"}}}))
    git("add", ".")
    git("commit", "--quiet", "-m", "fixture")
    commit = git("rev-parse", "HEAD")
    original = subprocess.Popen

    def local_transport(command, *args, **kwargs):
        command = [str(repository) if value == "https://fixture.invalid/repo.git" else value for value in command]
        command = ["protocol.file.allow=always" if value == "protocol.file.allow=never" else value for value in command]
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", local_transport)
    reference = "git::https://fixture.invalid/repo.git//package/mcp.json"
    inputs = capture(MCPServerArtifactSpec(source=reference))
    item = inputs.mcp_servers["inventory"]
    assert item.provenance.commit == commit and item.provenance.selected_path == "package/mcp.json"
    assert item.content.metadata == HTTP
    assert decode_inputs(encode_inputs(inputs)) == (4, inputs)
    with pytest.raises(SourceRefError):
        capture(MCPServerArtifactSpec(source="git::https://fixture.invalid/repo.git//package"))
