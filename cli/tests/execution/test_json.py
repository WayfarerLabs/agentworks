"""Behavioral coverage for the private bounded JSON transformation."""

from __future__ import annotations

import json
import traceback
from typing import Any, Literal

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution._json import transform_json

_LIMITS = {"max_bytes": 16_384, "max_depth": 64}
type JsonStrategy = Literal["replace", "merge-overwrite", "merge-preserve", "skip-existing"]


def _transform(
    source: bytes,
    existing: bytes | None,
    strategy: JsonStrategy,
    *,
    create: bool = True,
    max_bytes: int = 16_384,
    max_depth: int = 64,
) -> bytes | None:
    return transform_json(
        source,
        existing,
        strategy=strategy,
        create=create,
        max_bytes=max_bytes,
        max_depth=max_depth,
    )


def _decoded(content: bytes | None) -> dict[str, Any]:
    assert content is not None
    value = json.loads(content)
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (
            "replace",
            {
                "ui": {"theme": "light", "bell": True},
                "array": [{"new": True}],
                "scalar_to_object": {"new": True},
                "object_to_scalar": None,
            },
        ),
        (
            "merge-overwrite",
            {
                "ui": {"theme": "light", "old": True, "bell": True},
                "extra": True,
                "array": [{"new": True}],
                "scalar_to_object": {"new": True},
                "object_to_scalar": None,
            },
        ),
        (
            "merge-preserve",
            {
                "ui": {"theme": "dark", "old": True, "bell": True},
                "extra": True,
                "array": [{"old": True}],
                "scalar_to_object": 1,
                "object_to_scalar": {"old": True},
            },
        ),
    ],
)
def test_transform_strategies_recurse_only_into_objects(strategy: JsonStrategy, expected: dict[str, Any]) -> None:
    existing = json.dumps(
        {
            "ui": {"theme": "dark", "old": True},
            "extra": True,
            "array": [{"old": True}],
            "scalar_to_object": 1,
            "object_to_scalar": {"old": True},
        }
    ).encode()
    source = json.dumps(
        {
            "ui": {"theme": "light", "bell": True},
            "array": [{"new": True}],
            "scalar_to_object": {"new": True},
            "object_to_scalar": None,
        }
    ).encode()

    assert _decoded(_transform(source, existing, strategy)) == expected


def test_skip_existing_returns_only_the_skip_sentinel() -> None:
    assert _transform(b'{"valid": true}', b'{"private": "destination"}', "skip-existing") is None


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_absent_destination_requires_creation(strategy: JsonStrategy) -> None:
    with pytest.raises(StateError):
        _transform(b'{"enabled": true}', None, strategy, create=False)

    assert _decoded(_transform(b'{"enabled": true}', None, strategy)) == {"enabled": True}


@pytest.mark.parametrize("strategy", ["replace", "merge-overwrite", "merge-preserve", "skip-existing"])
def test_creation_selection_does_not_change_existing_destination_behavior(strategy: JsonStrategy) -> None:
    result = _transform(b'{"source": true}', b'{"existing": true}', strategy, create=False)
    if strategy == "skip-existing":
        assert result is None
    else:
        assert result is not None


@pytest.mark.parametrize("strategy", ["replace", "skip-existing"])
@pytest.mark.parametrize("existing", [b"", b"malformed \xff", b"x" * 32_768])
def test_nonmerge_strategies_do_not_parse_or_bound_existing_bytes(strategy: JsonStrategy, existing: bytes) -> None:
    result = _transform(b"{}", existing, strategy)
    if strategy == "skip-existing":
        assert result is None
    else:
        assert _decoded(result) == {}


@pytest.mark.parametrize("strategy", ["merge-overwrite", "merge-preserve"])
@pytest.mark.parametrize(
    "existing",
    [
        b"",
        b"[]",
        b'{"a": 1, "a": 2}',
        b'{"nested": {"a": 1, "a": 2}}',
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'{"value": 1e999}',
        b'{"value": "\xff"}',
    ],
)
def test_merge_requires_a_strict_existing_object(strategy: JsonStrategy, existing: bytes) -> None:
    with pytest.raises(ValidationError):
        _transform(b"{}", existing, strategy)


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"[]",
        b"null",
        b"false",
        b"1",
        b'{"a": 1, "a": 2}',
        b'{"nested": [{"a": 1, "a": 2}]}',
        b'{"value": NaN}',
        b'{"value": Infinity}',
        b'{"value": -Infinity}',
        b'{"value": 1e999}',
        b'{"value": -1e999}',
        b'{"value": "\xff"}',
    ],
)
def test_source_is_always_a_strict_finite_utf8_object(source: bytes) -> None:
    with pytest.raises(ValidationError):
        _transform(source, b"existing bytes are skipped", "skip-existing")


def test_invalid_source_wins_over_absent_and_skip_decisions() -> None:
    for existing, create in ((None, False), (b"private destination", True)):
        with pytest.raises(ValidationError):
            _transform(b'{"duplicate": 1, "duplicate": 2}', existing, "skip-existing", create=create)


def test_literal_null_is_a_value_in_both_merge_directions() -> None:
    assert _decoded(_transform(b'{"value": null}', b'{"value": 1}', "merge-overwrite")) == {"value": None}
    assert _decoded(_transform(b'{"value": null}', b'{"value": 1}', "merge-preserve")) == {"value": 1}
    assert _decoded(_transform(b'{"value": 1}', b'{"value": null}', "merge-preserve")) == {"value": None}


def test_serialization_is_semantic_stable_and_does_not_reuse_source_bytes() -> None:
    first_source = b'{"z":"\xc3\xa9","a":1}'
    second_source = rb'{ "a" : 1, "z" : "\u00e9" }'
    first = _transform(first_source, b"ignored", "replace")
    second = _transform(second_source, b"ignored", "replace")

    assert first == second == b'{\n  "a": 1,\n  "z": "\\u00e9"\n}\n'
    assert first != first_source


def test_source_byte_bound_has_no_smaller_implicit_cap() -> None:
    source = b'{"value":"' + (b"x" * 100_000) + b'"}'
    assert _transform(source, b"exists", "skip-existing", max_bytes=len(source)) is None
    with pytest.raises(ValidationError):
        _transform(source, b"exists", "skip-existing", max_bytes=len(source) - 1)


def test_merge_bounds_existing_input_and_serialized_result() -> None:
    with pytest.raises(ValidationError):
        _transform(b"{}", b'{"value":"too long for the bound"}', "merge-overwrite", max_bytes=16)

    source = b'{"source":"1234567890"}'
    existing = b'{"existing":"1234567890"}'
    assert len(source) < 48 and len(existing) < 48
    with pytest.raises(ValidationError):
        _transform(source, existing, "merge-overwrite", max_bytes=48)


def test_replace_result_is_independently_byte_bounded() -> None:
    with pytest.raises(ValidationError):
        _transform(b"{}", b"ignored", "replace", max_bytes=2)


def test_container_depth_counts_root_and_nested_containers_but_not_scalars() -> None:
    assert _decoded(_transform(b'{"scalar": 1}', None, "replace", max_depth=1)) == {"scalar": 1}
    assert _decoded(_transform(rb'{"scalar": "[\"{{"}', None, "replace", max_depth=1)) == {"scalar": '["{{'}
    assert _decoded(_transform(b'{"array": [1]}', None, "replace", max_depth=2)) == {"array": [1]}
    with pytest.raises(ValidationError):
        _transform(b'{"array": [1]}', None, "replace", max_depth=1)
    with pytest.raises(ValidationError):
        _transform(b"{}", b'{"nested": {}}', "merge-overwrite", max_depth=1)


def test_deep_valid_bytes_are_checked_without_a_recursive_depth_walk() -> None:
    source = b'{"value":' + (b"[" * 63) + b"0" + (b"]" * 63) + b"}"
    assert _decoded(_transform(source, None, "replace", max_bytes=100_000, max_depth=64))
    with pytest.raises(ValidationError):
        _transform(source, None, "replace", max_bytes=100_000, max_depth=63)


@pytest.mark.parametrize("bound_name", ["max_bytes", "max_depth"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_bounds_require_positive_plain_integers(bound_name: str, value: object) -> None:
    limits: dict[str, object] = dict(_LIMITS)
    limits[bound_name] = value
    with pytest.raises(ValidationError):
        transform_json(b"{}", None, strategy="replace", create=True, **limits)  # type: ignore[arg-type]


@pytest.mark.parametrize("stage", ["parse", "serialize"])
@pytest.mark.parametrize("failure_type", [RecursionError, ValueError])
def test_runtime_capacity_refusal_retains_no_payload_or_exception_chain(
    monkeypatch: pytest.MonkeyPatch, stage: str, failure_type: type[Exception]
) -> None:
    def refuse(*args: object, **kwargs: object) -> Any:
        raise failure_type("private-json-marker")

    if stage == "parse":
        monkeypatch.setattr(json, "loads", refuse)
    else:
        monkeypatch.setattr(json.JSONEncoder, "iterencode", refuse)

    with pytest.raises(ValidationError) as caught:
        _transform(b"{}", None, "replace")

    assert "private-json-marker" not in repr(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("source", "existing", "strategy"),
    [
        (b'{"private-json-marker":', None, "replace"),
        (b"{}", b'{"private-json-marker":', "merge-overwrite"),
    ],
)
def test_rejected_payload_is_absent_from_error_and_exception_chain(
    source: bytes, existing: bytes | None, strategy: JsonStrategy
) -> None:
    marker = "private-json-marker"
    with pytest.raises(ValidationError) as caught:
        _transform(source, existing, strategy)

    failure = caught.value
    assert marker not in repr(failure)
    assert marker not in "".join(traceback.format_exception_only(failure))
    assert failure.__context__ is None
    assert failure.__cause__ is None
