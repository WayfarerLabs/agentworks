"""Public OAuth intent at declaration, selected source and persisted-state boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agentworks.artifacts.bundle import ArtifactBundle, resolve_bundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.declarations import ArtifactsConfig, MCPServerArtifactSpec
from agentworks.artifacts.mcp import MCPHTTPDefinition, content_from_definition, content_from_source, parse_definition
from agentworks.artifacts.model import ArtifactGroup, ArtifactOrigin
from agentworks.artifacts.state import (
    CapturedArtifacts,
    UnsupportedArtifactCaptureVersionError,
    canonicalize_captures,
    capture_owner,
    declaration_digest,
    encode_captures,
    read_captures,
    write_capture,
)
from agentworks.db import AppliedStateKey, Database, VersionedPayload
from agentworks.db.instance_state import JsonObject
from agentworks.origin import Origin
from agentworks.resources.registry import Registry
from agentworks.sources import SourceRefError
from tests.artifacts._fixtures import group

HTTP: dict[str, object] = {"type": "http", "url": "https://example.test/mcp"}
OPTIONS: dict[str, object] = {"client_id": "public-client", "callback_port": 8180, "scopes": ["z:read", "a:write"]}
OWNER = ArtifactOrigin("agent", "agent", "worker")


def spec(definition: object) -> MCPServerArtifactSpec:
    return MCPServerArtifactSpec.model_validate({"definition": definition})


def registry_for(*bundles: ArtifactBundle) -> Registry:
    registry = Registry.empty()
    for bundle in bundles:
        registry.add("artifact-bundle", bundle.name, bundle, Origin.built_in(source="oauth-test"))
    return registry


def captured(definition: object) -> ArtifactGroup:
    return capture_artifacts(
        [("team", ArtifactBundle(name="team", mcp_servers={"inventory": spec(definition)}))], OWNER
    )


@pytest.mark.parametrize(
    "oauth", [{}, OPTIONS, {"callback_port": 1}, {"callback_port": 65535}, {"client_id": " public id "}]
)
def test_public_options_remain_typed_and_ordered(oauth: dict[str, object]) -> None:
    definition = parse_definition({**HTTP, "oauth": oauth})
    assert isinstance(definition, MCPHTTPDefinition)
    assert definition.oauth is not None
    assert definition.oauth.model_dump(mode="json") == oauth
    assert content_from_definition("inventory", definition).metadata == {**HTTP, "headers": {}, "oauth": oauth}


@pytest.mark.parametrize(
    "oauth",
    [
        True,
        False,
        [],
        "public",
        0,
        {"clientId": "public"},
        {"callbackPort": 8180},
        {"client_secret": "rejected-private-value"},
        {"tokens": {}},
        {"authorization_code": "rejected-private-value"},
        {"client_id": True},
        {"client_id": 1},
        {"client_id": []},
        {"client_id": ""},
        {"client_id": " \u00a0"},
        {"client_id": "bad\x00id"},
        {"client_id": "bad\nid"},
        {"client_id": "bad\x7fid"},
        {"client_id": "${CLIENT_ID}"},
        {"client_id": "$(client-id)"},
        {"client_id": "`client-id`"},
        {"callback_port": True},
        {"callback_port": False},
        {"callback_port": "8180"},
        {"callback_port": 8180.0},
        {"callback_port": 0},
        {"callback_port": -1},
        {"callback_port": 65536},
        {"callback_port": []},
        {"scopes": []},
        {"scopes": "read"},
        {"scopes": ("read",)},
        {"scopes": [1]},
        {"scopes": [True]},
        {"scopes": [None]},
        {"scopes": ["read", "read"]},
        {"scopes": [""]},
        {"scopes": ["read write"]},
        {"scopes": ["read\twrite"]},
        {"scopes": ["read\nwrite"]},
        {"scopes": ['read"write']},
        {"scopes": ["read\\write"]},
        {"scopes": ["read\x00write"]},
        {"scopes": ["read\x7fwrite"]},
        {"scopes": ["r\u00e9ad"]},
    ],
)
def test_invalid_auth_shape_is_rejected_at_both_declaration_boundaries(oauth: object) -> None:
    definition = {**HTTP, "oauth": oauth}
    with pytest.raises(ValidationError):
        spec(definition)
    with pytest.raises(SourceRefError):
        parse_definition(definition)


def test_scope_tokens_accept_the_full_rfc_ascii_range_without_reordering() -> None:
    token = "".join(chr(value) for value in range(0x21, 0x7F) if value not in (0x22, 0x5C))
    definition = parse_definition({**HTTP, "oauth": {"scopes": [token, "read"]}})
    assert content_from_definition("inventory", definition).metadata["oauth"] == {"scopes": [token, "read"]}


@pytest.mark.parametrize("oauth", [None, {}, OPTIONS])
def test_stdio_rejects_any_oauth_field(oauth: object) -> None:
    with pytest.raises(ValidationError):
        spec({"type": "stdio", "command": "server", "oauth": oauth})
    with pytest.raises(SourceRefError):
        parse_definition({"type": "stdio", "command": "server", "oauth": oauth})


@pytest.mark.parametrize("header", ["Authorization", "authorization", "AUTHORIZATION", "aUtHoRiZaTiOn"])
@pytest.mark.parametrize("oauth", [{}, OPTIONS, {"scopes": None}])
def test_explicit_oauth_conflicts_with_every_authorization_header_case(header: str, oauth: object) -> None:
    definition = {**HTTP, "headers": {header: "Bearer ${TOKEN}"}, "oauth": oauth}
    with pytest.raises(ValidationError):
        spec(definition)
    with pytest.raises(SourceRefError):
        parse_definition(definition)


def test_other_header_grammar_and_absent_oauth_authorization_remain_supported() -> None:
    headers = {"X-Proxy": "${PROXY_TOKEN}", "X-Public": "public"}
    assert (
        captured({**HTTP, "headers": headers, "oauth": {}}).mcp_servers["inventory"].content.metadata["headers"]
        == headers
    )
    assert (
        "oauth"
        not in captured({**HTTP, "headers": {"Authorization": "Bearer ${TOKEN}"}, "oauth": None})
        .mcp_servers["inventory"]
        .content.metadata
    )
    with pytest.raises(SourceRefError):
        parse_definition({**HTTP, "headers": {"X-Proxy": "Bearer ${TOKEN}"}, "oauth": {}})


@pytest.mark.parametrize("key", ["private\nfield", "\x1b[31mprivate-field", "private-field" * 1000])
def test_nested_rejections_keep_errors_bounded_and_omit_values(key: str) -> None:
    definition = {**HTTP, "oauth": {key: "rejected-private-value"}}
    with pytest.raises(ValidationError) as inline_error:
        spec(definition)
    with pytest.raises(SourceRefError) as source_error:
        content_from_source("inventory", json.dumps({"mcpServers": {"inventory": definition}}))
    for error in (inline_error.value, source_error.value):
        message = str(error)
        assert key not in message and "rejected-private-value" not in message
        assert len(message) < 512
    assert str(source_error.value).isprintable()


def test_nullable_fields_normalize_without_losing_explicit_default_intent() -> None:
    absent = spec(HTTP)
    null = spec({**HTTP, "oauth": None})
    defaults = spec({**HTTP, "oauth": {}})
    null_options = spec({**HTTP, "oauth": {"client_id": None, "callback_port": None, "scopes": None}})
    assert absent.model_dump(mode="json") == null.model_dump(mode="json")
    assert defaults.model_dump(mode="json") == null_options.model_dump(mode="json")
    assert defaults.model_dump(mode="json")["definition"]["oauth"] == {}
    assert spec({**HTTP, "oauth": {**OPTIONS, "callback_port": None}}).model_dump(mode="json")["definition"][
        "oauth"
    ] == {"client_id": "public-client", "scopes": ["z:read", "a:write"]}
    old = captured(HTTP).mcp_servers["inventory"]
    assert old == captured({**HTTP, "oauth": None}).mcp_servers["inventory"]
    assert old.content.digest != captured({**HTTP, "oauth": {}}).mcp_servers["inventory"].content.digest
    assert "oauth" not in old.content.metadata


@pytest.mark.windows
@pytest.mark.parametrize("oauth", [None, {}, OPTIONS, {"client_id": None, "scopes": None}])
def test_selected_jsonc_and_inline_use_identical_canonical_options(tmp_path: Path, oauth: object) -> None:
    definition = {**HTTP, "oauth": oauth}
    source = tmp_path / "mcp.jsonc"
    source.write_text(
        "// source comment\n" + json.dumps({"mcpServers": {"inventory": definition, "unselected": {"oauth": True}}})
    )
    inputs = capture_artifacts(
        [("team", ArtifactBundle(name="team", mcp_servers={"inventory": MCPServerArtifactSpec(source=str(source))}))],
        OWNER,
    )
    item = inputs.mcp_servers["inventory"]
    assert item.content == captured(definition).mcp_servers["inventory"].content
    assert item.identity == captured(definition).mcp_servers["inventory"].identity
    source.unlink()
    assert decode_inputs(encode_inputs(inputs)) == (5, inputs)


def test_oauth_overrides_replace_the_entire_entry() -> None:
    parent = ArtifactBundle(name="parent", mcp_servers={"inventory": spec({**HTTP, "oauth": OPTIONS})})
    child = ArtifactBundle(name="child", inherits=["parent"], mcp_servers={"inventory": spec({**HTTP, "oauth": {}})})
    resolved = resolve_bundle(registry_for(parent, child), "child").value
    definition = resolved.mcp_servers["inventory"].definition
    assert isinstance(definition, MCPHTTPDefinition) and definition.oauth is not None
    assert definition.oauth.model_dump(mode="json") == {}


def test_phase_one_declaration_and_content_identity_are_preserved() -> None:
    # Measured on the phase-1 model, including its description/source null defaults.
    bundle = ArtifactBundle(name="team", mcp_servers={"inventory": spec(HTTP)})
    assert (
        declaration_digest(registry_for(bundle), ArtifactsConfig(bundles=["team"]))
        == "500bceab81167f0ada78963a6d72c0ce15b52d3c90f5d786bc8920e8e5d623d7"
    )
    assert spec(HTTP).model_dump(mode="json") == {
        "definition": {**HTTP, "description": None, "headers": {}},
        "source": None,
    }
    item = captured(HTTP).mcp_servers["inventory"]
    assert item.content.metadata_json == '{"headers":{},"type":"http","url":"https://example.test/mcp"}'
    assert item.content.digest == "9e9024e1abbc4d1b2869425988a85ed07ccc5b32f2cd068a2dd4b0708d38ad86"
    assert item.identity == "914b9ded25ac9d3f3849b0dd8dbd9de4a69d06c5164aa6e04fb33edbe0cf81c5"
    legacy = encode_inputs(captured(HTTP), version=4)
    assert decode_inputs(legacy) == (4, captured(HTTP))
    assert encode_inputs(decode_inputs(legacy)[1], version=4) == legacy


def test_auth_intent_and_each_option_change_declaration_and_content_hashes() -> None:
    declarations = set()
    digests = set()
    for oauth in (
        None,
        {},
        {"client_id": "public"},
        {"callback_port": 8180},
        {"scopes": ["read", "write"]},
        {"scopes": ["write", "read"]},
    ):
        definition = {**HTTP, "oauth": oauth}
        bundle = ArtifactBundle(name="team", mcp_servers={"inventory": spec(definition)})
        declarations.add(declaration_digest(registry_for(bundle), ArtifactsConfig(bundles=["team"])))
        digests.add(captured(definition).mcp_servers["inventory"].content.digest)
    assert len(declarations) == len(digests) == 6


@pytest.mark.parametrize("oauth", [None, {}, OPTIONS, {"client_id": None}])
def test_v4_rejects_any_auth_field_even_with_matching_hashes(oauth: object) -> None:
    inputs = captured(HTTP)
    item = inputs.mcp_servers["inventory"]
    metadata = {**item.content.metadata, "oauth": oauth}
    forged = replace(
        item, content=replace(item.content, metadata_json=json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    )
    payload = encode_inputs(inputs, version=4)
    row = cast("dict[str, Any]", payload["mcp_servers"])["inventory"]
    row["content"]["metadata_json"] = forged.content.metadata_json
    row["content"]["digest"] = forged.content.digest
    row["identity"] = forged.identity
    with pytest.raises(SourceRefError):
        decode_inputs(payload)
    with pytest.raises(SourceRefError):
        encode_inputs(group(forged), version=4)


@pytest.mark.parametrize("oauth", [{}, OPTIONS])
def test_current_capture_round_trips_public_auth_and_rejects_v4_write(oauth: object) -> None:
    inputs = captured({**HTTP, "oauth": oauth})
    payload = encode_inputs(inputs)
    assert decode_inputs(payload) == (5, inputs)
    assert encode_inputs(decode_inputs(payload)[1]) == payload
    with pytest.raises(SourceRefError):
        encode_inputs(inputs, version=4)


@pytest.mark.parametrize(
    "oauth",
    [{"callback_port": True}, {"scopes": []}, {"client_secret": "rejected-private-value"}],
)
def test_current_capture_rejects_invalid_auth_even_with_matching_hashes(oauth: object) -> None:
    inputs = captured(HTTP)
    item = inputs.mcp_servers["inventory"]
    metadata = {**item.content.metadata, "oauth": oauth}
    forged = replace(
        item,
        content=replace(item.content, metadata_json=json.dumps(metadata, sort_keys=True, separators=(",", ":"))),
    )
    payload = encode_inputs(inputs)
    row = cast("dict[str, Any]", payload["mcp_servers"])["inventory"]
    row["content"]["metadata_json"] = forged.content.metadata_json
    row["content"]["digest"] = forged.content.digest
    row["identity"] = forged.identity
    with pytest.raises(SourceRefError) as error:
        decode_inputs(payload)
    assert "rejected-private-value" not in str(error.value)


@pytest.mark.windows
def test_current_state_writes_v5_and_preserves_retained_v4(db: Database) -> None:
    db.insert_vm("box", site="local", hostname="box")
    bundle = ArtifactBundle(name="team", mcp_servers={"inventory": spec({**HTTP, "oauth": OPTIONS})})
    current = capture_owner(registry_for(bundle), "vm", "box", "admin", ArtifactsConfig(bundles=["team"]))
    old_bundle = ArtifactBundle(name="team", mcp_servers={"inventory": spec(HTTP)})
    old_inputs = capture_artifacts([("team", old_bundle)], ArtifactOrigin("vm", "vm", "box"))
    retained = CapturedArtifacts("a" * 64, old_inputs, 4)
    write_capture(db, "vm", "box", "vm", retained, operation="fixture")
    before = db.instance_state.get_applied_slices("vm", "box")[0].payload
    write_capture(db, "vm", "box", "admin", current, operation="fixture")
    assert current.codec_version == 5
    assert read_captures(db, "vm", "box") == {"vm": retained, "admin": current}
    record = db.instance_state.get_applied_slices("vm", "box")[0]
    assert record.payload.payload_version == 5
    assert canonicalize_captures(record) == record.payload
    components = cast("dict[str, Any]", record.payload.value["components"])
    old_components = cast("dict[str, Any]", before.value["components"])
    assert components["vm"] == old_components["vm"]
    assert encode_captures({}).payload_version == 5


@pytest.mark.windows
@pytest.mark.parametrize("future", [6, 99])
def test_future_capture_refuses_read_and_overwrite_and_preserves_backup(db: Database, future: int) -> None:
    db.insert_vm("box", site="local", hostname="box")
    inputs = capture_artifacts(
        [("team", ArtifactBundle(name="team", mcp_servers={"inventory": spec(HTTP)}))],
        ArtifactOrigin("vm", "vm", "box"),
    )
    capture = CapturedArtifacts("a" * 64, inputs)
    content = encode_inputs(inputs)
    content["version"] = future
    with pytest.raises(SourceRefError):
        decode_inputs(content)
    payload = VersionedPayload(future, cast(JsonObject, {"uninterpreted": content}))
    db.instance_state.replace_applied_slices("vm", "box", "fixture", {AppliedStateKey.ARTIFACT_INPUTS: payload})
    before = db.instance_state.get_applied_slices("vm", "box")
    with pytest.raises(UnsupportedArtifactCaptureVersionError):
        read_captures(db, "vm", "box")
    with pytest.raises(UnsupportedArtifactCaptureVersionError):
        write_capture(db, "vm", "box", "vm", capture, operation="replace")
    assert db.instance_state.get_applied_slices("vm", "box") == before
    assert canonicalize_captures(before[0]) == payload
    assert db.snapshot_vm_backup_data("box")[-1][0].payload == payload
