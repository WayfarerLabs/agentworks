"""Native settings semantics, independent of transport and applied state."""

from __future__ import annotations

import json
import tomllib
import traceback
from pathlib import Path

import pytest
import tomli_w
from pydantic import ValidationError

from agentworks.capabilities.harness_integration.settings import (
    PreparedSettings,
    SettingsFormat,
    SettingsMapping,
    SettingsStrategy,
    parse_settings,
    prepare_settings,
)
from agentworks.errors import ConfigError
from agentworks.sources import SourceRefError


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"source": "settings.json"},
        {"strategy": "replace"},
        {"source": "settings.json", "strategy": "invented"},
        {"source": "settings.json", "strategy": "replace", "destination": "/etc/settings"},
        {"source": "git::https://example.com/settings.git", "strategy": "replace"},
        {"source": "git::invalid-value", "strategy": "replace"},
        {"source": "https::example.com/settings", "strategy": "replace"},
        {"source": "file::", "strategy": "replace"},
        {"source": "", "strategy": "replace"},
        {"source": 42, "strategy": "replace"},
    ],
)
def test_mapping_requires_explicit_policy_and_local_source(config: dict) -> None:
    with pytest.raises(ValidationError):
        SettingsMapping.model_validate(config)


@pytest.mark.parametrize("format", ["json", "toml"])
@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_nested_merge_arrays_and_type_collisions(format: SettingsFormat, strategy: SettingsStrategy) -> None:
    old = {
        "ui": {"theme": "dark", "old": True},
        "extra": True,
        "array": [{"old": True}],
        "scalar_to_object": 1,
        "object_to_scalar": {"old": True},
    }
    new = {
        "ui": {"theme": "light", "bell": True},
        "array": [{"new": True}],
        "scalar_to_object": {"new": True},
        "object_to_scalar": 2,
    }

    def encode(value: dict) -> bytes:
        return (json.dumps(value) if format == "json" else tomli_w.dumps(value)).encode()

    source = encode(new)
    destination = encode(old)
    prepared = PreparedSettings.from_bytes(source, format=format, strategy=strategy)
    result = prepared.apply(destination)
    actual = parse_settings(result.content, format=format)
    if strategy == "replace":
        assert actual == new
    elif strategy == "merge-overwrite":
        assert actual == {**old, **new, "ui": {"theme": "light", "old": True, "bell": True}}
    elif strategy == "merge-preserve":
        assert actual == {**new, **old, "ui": {"theme": "dark", "old": True, "bell": True}}
    else:
        assert actual == old
        assert result.content == destination
    assert result.skipped is (strategy == "skip-existing")
    # The same prepared source remains reusable and does not absorb merged keys.
    assert parse_settings(prepared.apply(None).content, format=format) == new
    assert prepared.apply(result.content).content == result.content


@pytest.mark.parametrize("format,source", [("json", b'{"enabled": true}'), ("toml", b"enabled = true\n")])
@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_absent_destination_created_with_every_policy(
    format: SettingsFormat, source: bytes, strategy: SettingsStrategy
) -> None:
    result = PreparedSettings.from_bytes(source, format=format, strategy=strategy).apply(None)
    assert parse_settings(result.content, format=format) == {"enabled": True}
    assert not result.skipped


@pytest.mark.parametrize("format,source", [("json", b"{}"), ("toml", b"a = 1\n")])
@pytest.mark.parametrize("destination", [b"", b"invalid document \xff"])
@pytest.mark.parametrize("strategy", ["replace", "skip-existing"])
def test_nonmerge_does_not_parse_existing_file(
    format: SettingsFormat, source: bytes, destination: bytes, strategy: SettingsStrategy
) -> None:
    result = PreparedSettings.from_bytes(source, format=format, strategy=strategy).apply(destination)
    if strategy == "skip-existing":
        assert result.content == destination
        assert result.skipped
    else:
        assert parse_settings(result.content, format=format) == parse_settings(source, format=format)


@pytest.mark.parametrize("strategy", ["merge-overwrite", "merge-preserve"])
@pytest.mark.parametrize("format,source", [("json", b"{}"), ("toml", b"a = 1")])
def test_merge_requires_valid_destination(strategy: SettingsStrategy, format: SettingsFormat, source: bytes) -> None:
    prepared = PreparedSettings.from_bytes(source, format=format, strategy=strategy)
    with pytest.raises(ConfigError):
        prepared.apply(b"invalid document")


@pytest.mark.parametrize(
    "source",
    [
        b"[]",
        b"null",
        b"false",
        b"1",
        b"",
        b'{"a": 1, "a": 2}',
        b'{"nested": [{"a": 1, "a": 2}]}',
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": -Infinity}',
        b'{"a": 1e999}',
        b'{"a": -1e999}',
        b'{"a": "\xff"}',
    ],
)
@pytest.mark.parametrize("strategy", ["replace", "skip-existing"])
def test_invalid_json_source_always_rejected(source: bytes, strategy: SettingsStrategy) -> None:
    with pytest.raises(ConfigError):
        PreparedSettings.from_bytes(source, format="json", strategy=strategy)


@pytest.mark.parametrize("source", [b"a = 1\na = 2", b"[a]\n[a]\n", b"a = {b=1, b=2}", b"a=\xff"])
def test_invalid_toml_source_rejected(source: bytes) -> None:
    with pytest.raises(ConfigError):
        PreparedSettings.from_bytes(source, format="toml", strategy="skip-existing")


def test_toml_native_values_survive_semantic_serialization() -> None:
    source = b"""day = 2026-09-07
instant = 2026-09-07T10:30:00Z
clock = 10:30:00
ratio = inf
[[servers]]
name = "one"
"""
    result = PreparedSettings.from_bytes(source, format="toml", strategy="replace").apply(None)
    assert tomllib.loads(result.content.decode()) == tomllib.loads(source.decode())


@pytest.mark.parametrize(
    "format,source", [("json", b'{"external-sensitive-input":'), ("toml", b'"external-sensitive-input" =')]
)
def test_parse_error_does_not_echo_input(format: SettingsFormat, source: bytes) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_settings(source, format=format)
    assert "external-sensitive-input" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.windows
def test_prepared_workstation_source_is_stable_after_change_and_deletion(tmp_path: Path) -> None:
    source = tmp_path / "settings.json"
    source.write_bytes(b'{"original": true}')
    mapping = SettingsMapping(source=f"file::{source}", strategy="merge-overwrite")
    prepared = prepare_settings(mapping, format="json")
    source.write_bytes(b'{"replacement": true}')
    assert json.loads(prepared.apply(b"{}").content) == {"original": True}
    source.unlink()
    assert json.loads(prepared.apply(None).content) == {"original": True}
    with pytest.raises(SourceRefError):
        prepare_settings(mapping, format="json")


@pytest.mark.parametrize("strategy", ["merge-overwrite", "merge-preserve"])
@pytest.mark.parametrize(
    "format,source,destination",
    [("json", b"{}", b'{"nested": {"key": 1, "key": 2}}'), ("toml", b"", b"[nested]\nkey=1\nkey=2")],
)
def test_merge_rejects_duplicate_destination_keys(
    strategy: SettingsStrategy, format: SettingsFormat, source: bytes, destination: bytes
) -> None:
    with pytest.raises(ConfigError):
        PreparedSettings.from_bytes(source, format=format, strategy=strategy).apply(destination)
