"""``extract_references`` is total: no input makes it raise.

This is the property the whole dependency graph rests on. The registry
builds edges before it validates anything, so a config with both a
malformed blob and a cycle must still report the cycle.

The property is quantified over BOTH inputs, not just the blob. The blob
axis is an explicit adversarial corpus plus a seeded generator; the model
axis is the fixture set, which is why it carries the shapes that are easy
to get wrong (every union spelling, collections of models, a model that
cannot be built) rather than only well-formed ones.

The kind oracle is deliberately the OTHER derivation mechanism: the
expected kinds come from emitted JSON Schema (the marker's schema hook),
not from the walker under test, so a walker that invented an edge could
not also excuse itself. The generator's own vocabulary is read from the
same schema, so each model's blobs mostly land on ITS field names and ITS
discriminator values.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from pydantic.errors import PydanticSchemaGenerationError, PydanticUndefinedAnnotation, PydanticUserError

from agentworks.resources.schema import REF_SCHEMA_KEY, RefOwner, extract_references

from ._fixture_models import ALL_FIXTURES

if TYPE_CHECKING:
    from collections.abc import Iterator

OWNER = RefOwner(kind="vm-site", name="lab")

#: Keys and values every generated blob draws from on top of the ones
#: read off the model under test: junk names, and scalars of every wrong
#: type.
_JUNK_KEYS = ("bogus", "", "0")

_JUNK_VALUES: tuple[object, ...] = (
    None,
    0,
    1,
    -1,
    3.5,
    True,
    False,
    "",
    "x",
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


def _schema_of(model_cls: type[BaseModel]) -> dict[str, object]:
    """The model's emitted JSON Schema, or nothing when it cannot be
    built. An unbuildable model declares nothing, which makes the
    assertions on it the strongest available: it must produce no edges at
    all."""
    try:
        return model_cls.model_json_schema()
    except (PydanticSchemaGenerationError, PydanticUndefinedAnnotation, PydanticUserError):
        return {}


def _kinds_declared_by(model_cls: type[BaseModel]) -> set[str]:
    """Every marker kind reachable in the model's emitted JSON Schema."""
    kinds: set[str] = set()
    for node in _schema_nodes(_schema_of(model_cls)):
        extension = node.get(REF_SCHEMA_KEY)
        if isinstance(extension, dict):
            kinds.add(str(extension["kind"]))
    return kinds


def _schema_nodes(node: object) -> Iterator[dict[str, object]]:
    """Every object node in a JSON Schema document, the root included."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _schema_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schema_nodes(item)


@dataclass(frozen=True)
class _Vocabulary:
    """What a generated blob for one model is built out of.

    Read off the model's emitted JSON Schema rather than hand-listed, so
    every model's generated blobs mostly land on ITS field names and ITS
    discriminator values. A generator whose keys rarely match anything
    tests only the ignore-it path.
    """

    keys: tuple[str, ...]
    values: tuple[object, ...]
    tags: tuple[str, ...]
    """The model's own discriminator values, drawn half the time so a
    tagged union is actually SELECTED rather than only mis-tagged."""


def _vocabulary_of(model_cls: type[BaseModel]) -> _Vocabulary:
    keys: set[str] = set(_JUNK_KEYS)
    tags: set[str] = set()
    for node in _schema_nodes(_schema_of(model_cls)):
        properties = node.get("properties")
        if isinstance(properties, dict):
            keys.update(str(key) for key in properties)
        for declared in (node.get("enum"), node.get("const")):
            for tag in declared if isinstance(declared, list) else [declared]:
                if isinstance(tag, str):
                    tags.add(tag)
    return _Vocabulary(
        keys=tuple(sorted(keys)),
        values=(*_JUNK_VALUES, *sorted(tags)),
        tags=tuple(sorted(tags)),
    )


def _random_blob(rng: random.Random, vocabulary: _Vocabulary, depth: int = 0) -> object:
    roll = rng.random()
    if depth > 3 or roll < 0.25:
        if vocabulary.tags and rng.random() < 0.5:
            return rng.choice(vocabulary.tags)
        return rng.choice(vocabulary.values)
    if roll < 0.45:
        return [_random_blob(rng, vocabulary, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rng.choice(vocabulary.keys): _random_blob(rng, vocabulary, depth + 1) for _ in range(rng.randint(1, 4))}


def _generated_blobs(model_cls: type[BaseModel], count: int = 800) -> list[object]:
    # Seeded, so a failure reproduces exactly. Hypothesis would say this
    # more directly; adding a test dependency is a decision outside this
    # step, and a seeded generator is reproducible and sufficient.
    rng = random.Random(20260806)  # noqa: S311
    vocabulary = _vocabulary_of(model_cls)
    return [_random_blob(rng, vocabulary) for _ in range(count)]


def _assert_sane(model_cls: type[BaseModel], blob: object, declared: set[str]) -> None:
    for ref in extract_references(model_cls, blob, OWNER):
        assert isinstance(ref.name, str) and ref.name, f"{model_cls.__name__} produced an unnamed edge from {blob!r}"
        assert ref.kind in declared, f"{model_cls.__name__} produced an undeclared {ref.kind} edge from {blob!r}"


@pytest.mark.parametrize("model_cls", ALL_FIXTURES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("blob", _ADVERSARIAL, ids=range(len(_ADVERSARIAL)))
def test_the_adversarial_corpus_never_raises(model_cls: type[BaseModel], blob: object) -> None:
    _assert_sane(model_cls, blob, _kinds_declared_by(model_cls))


@pytest.mark.parametrize("model_cls", ALL_FIXTURES, ids=lambda cls: cls.__name__)
def test_generated_blobs_never_raise(model_cls: type[BaseModel]) -> None:
    declared = _kinds_declared_by(model_cls)
    for blob in _generated_blobs(model_cls):
        _assert_sane(model_cls, blob, declared)


#: Why each fixture the corpus reaches NO edge on cannot have one. A
#: whole-suite "somebody produced an edge" check would pass on one
#: fixture out of twenty, so the expectation is stated per model and both
#: directions are asserted.
_EDGELESS_BY_DESIGN = {
    "CatalogEntryLike": "nothing in a size catalog entry names a Resource",
    "LimaArm": "the arm that names nothing",
    "UnmarkedLike": "no marked field at any depth",
    "StringRoot": "a bare scalar root names nothing",
    "UndiscriminatedSite": "no discriminator, so no arm is addressable",
    "NumericallyTaggedSite": "tagged by something other than a name",
    "NeverResolved": "the model cannot be built",
    "ResolvesToUnbuildable": "the model cannot be built",
}


@pytest.mark.parametrize("model_cls", ALL_FIXTURES, ids=lambda cls: cls.__name__)
def test_the_inputs_reach_an_edge_on_every_model_that_has_one(model_cls: type[BaseModel]) -> None:
    # Per model, not per suite: the assertions in the two tests above are
    # vacuous for any model whose inputs never reach an edge, and a
    # whole-suite check would be satisfied by one fixture out of twenty.
    blobs = [*_ADVERSARIAL, *_generated_blobs(model_cls)]
    produced = [ref for blob in blobs for ref in extract_references(model_cls, blob, OWNER)]
    reason = _EDGELESS_BY_DESIGN.get(model_cls.__name__)
    if reason is None:
        assert produced, f"no input ever reached an edge on {model_cls.__name__}; its assertions are vacuous"
    else:
        assert not produced, f"{model_cls.__name__} was expected to be edgeless ({reason}) and was not"


def test_the_kind_oracle_is_not_vacuous() -> None:
    from ._fixture_models import TemplateLike

    assert _kinds_declared_by(TemplateLike) == {"vm-template"}
