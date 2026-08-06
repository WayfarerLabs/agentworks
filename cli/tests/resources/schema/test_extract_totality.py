"""``extract_references`` is total: no input makes it raise.

This is the property the whole dependency graph rests on. The registry
builds edges before it validates anything, so a config with both a
malformed blob and a cycle must still report the cycle. Two layers here:
an explicit adversarial corpus, and a seeded generator over the fixture
models.

The kind oracle is deliberately the OTHER derivation mechanism: the
expected kinds come from emitted JSON Schema (the marker's schema hook),
not from the walker under test, so a walker that invented an edge could
not also excuse itself.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from agentworks.resources.schema import REF_SCHEMA_KEY, RefOwner, extract_references

from ._fixture_models import ALL_FIXTURES

if TYPE_CHECKING:
    from collections.abc import Iterator

OWNER = RefOwner(kind="vm-site", name="lab")

#: Keys the generator draws from: the fixtures' real field names, some
#: discriminator values, and junk.
_KEYS = (
    "token",
    "secret",
    "name",
    "platform",
    "inherits",
    "image",
    "service_principal",
    "credentials",
    "access_key_secret",
    "token_secret",
    "primary",
    "fallback",
    "child",
    "region",
    "root",
    "bogus",
    "",
)

_SCALARS: tuple[object, ...] = (
    None,
    0,
    1,
    -1,
    3.5,
    True,
    False,
    "",
    "x",
    "lima",
    "proxmox",
    "unregistered",
    b"bytes",
)

_ADVERSARIAL: tuple[object, ...] = (
    None,
    0,
    "",
    False,
    [],
    {},
    "a bare string",
    ["a", "list", "where", "a", "table", "belongs"],
    {"token": ["a", "list", "where", "a", "string", "belongs"]},
    {"token": {"nested": "table"}},
    {"service_principal": []},
    {"service_principal": {"secret": {"deeper": ["garbage", {"still": "garbage"}]}}},
    {"platform": {"name": 8}},
    {"platform": {"name": "no-such-arm", "token_secret": "x"}},
    {"platform": ["not", "a", "table"]},
    {"inherits": {"not": "a list"}},
    {"inherits": [[], {}, None, 0, ""]},
    {1: "a non-string key", (2, 3): "an unhashable-looking key"},
    {"child": {"child": {"child": {"child": {"secret": "deep"}}}}},
    {"primary": {"secret": None}, "fallback": 8},
    {"root": "x"},
)


def _kinds_declared_by(model_cls: type[BaseModel]) -> set[str]:
    """Every marker kind reachable in the model's emitted JSON Schema."""
    return set(_walk_schema(model_cls.model_json_schema()))


def _walk_schema(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == REF_SCHEMA_KEY and isinstance(value, dict):
                yield str(value["kind"])
            else:
                yield from _walk_schema(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_schema(item)


def _random_blob(rng: random.Random, depth: int = 0) -> object:
    roll = rng.random()
    if depth > 3 or roll < 0.45:
        return rng.choice(_SCALARS)
    if roll < 0.7:
        return [_random_blob(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(_KEYS): _random_blob(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def _assert_sane(model_cls: type[BaseModel], blob: object) -> None:
    refs = extract_references(model_cls, blob, OWNER)
    declared = _kinds_declared_by(model_cls)
    for ref in refs:
        assert isinstance(ref.name, str) and ref.name, f"{model_cls.__name__} produced an unnamed edge from {blob!r}"
        assert ref.kind in declared, f"{model_cls.__name__} produced an undeclared {ref.kind} edge from {blob!r}"


@pytest.mark.parametrize("model_cls", ALL_FIXTURES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("blob", _ADVERSARIAL, ids=range(len(_ADVERSARIAL)))
def test_the_adversarial_corpus_never_raises(model_cls: type[BaseModel], blob: object) -> None:
    _assert_sane(model_cls, blob)


@pytest.mark.parametrize("model_cls", ALL_FIXTURES, ids=lambda cls: cls.__name__)
def test_generated_blobs_never_raise(model_cls: type[BaseModel]) -> None:
    # Seeded, so a failure reproduces exactly. Hypothesis would say this
    # more directly; adding a test dependency is a decision outside this
    # step, and a seeded generator is reproducible and sufficient.
    rng = random.Random(20260806)  # noqa: S311
    for _ in range(250):
        _assert_sane(model_cls, _random_blob(rng))


def test_the_corpus_is_not_vacuous() -> None:
    # A corpus that produced no edges at all would pass every assertion
    # above while exercising nothing.
    found = [
        ref for model_cls in ALL_FIXTURES for blob in _ADVERSARIAL for ref in extract_references(model_cls, blob, OWNER)
    ]
    assert found, "the adversarial corpus never reached an edge; the assertions above are vacuous"


def test_the_kind_oracle_is_not_vacuous() -> None:
    from ._fixture_models import TemplateLike

    assert _kinds_declared_by(TemplateLike) == {"vm-template"}
