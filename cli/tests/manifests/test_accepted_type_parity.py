"""The field stream offers every arm the emitted schema does.

Emitted JSON Schema and the field-reference stream are SIBLING
derivations of one authored model (``manifests/emit.py``'s module
docstring, ``schema/fields.py``'s). Neither reads the other, which is what
keeps them from being one generator pretending to be two; the price is
that nothing structural stops them disagreeing, and a disagreement is
always the same defect wearing a different hat: the schema says a value
may be written two ways and the stream documents one of them, so
``describe-kind`` (which ``docs/guides/resources.md`` calls the authority
on what a spec accepts) tells an operator to write the long form of
something the loader takes short.

This module is the structural stop. It reads what pydantic emits for a
model and what :attr:`~agentworks.schema.FieldDoc.annotation` says an
operator may write, and fails when either offers a type the other does
not, at every depth a collection reaches. The two directions are two
different lies (the stream under-documenting what loads, and the stream
offering a spelling the schema rejects), so each is its own test.

**Nothing here derives its expectation from the walker under test.** The
schema side is pydantic's own ``model_json_schema``; the annotation side
is read with ``typing`` primitives in :func:`_annotation_types` rather
than with ``agentworks.schema``'s helpers, deliberately: a guard that
asked the classifier what a field accepts would agree with the classifier
about a field it classified wrongly, which is the recorded way this
effort's tests have passed for the wrong reason.

One subtraction, and it is dodged rather than subtracted: the YAML 1.1
spelling correction is avoided by reading the DEFAULT schema generator
instead of ``manifests/emit._ManifestJsonSchema``. That correction is a
property of an editor's parser, not of the model, so it is not something
the stream should be documenting.

``null`` used to be subtracted here too, on the grounds that an
owner-templated field is emitted nullable on purpose
(``schema/base.py``'s ``_with_marker_corrections``) and that optionality
is a fact the stream carries separately as ``FieldDoc.required``. The
second half of that was true and the first half was the defect: the
emitted schema accepts ``token: null`` as a VALUE spelling, the loader
reads it as the instruction to use the owner template, and
``describe-kind`` rendered a bare "string". The subtraction is what let
the two derivations disagree in the one place this module exists to
watch, so it is gone and ``accepted_annotation`` states the widening
instead.
"""

from __future__ import annotations

import types
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from agentworks.capabilities.config import capability_config_model, registered_implementations
from agentworks.capabilities.descriptor import capability_descriptors
from agentworks.manifests.spec_model import declarable_kinds, metadata_model, spec_model
from agentworks.schema import iter_field_docs, model_is_complete
from tests.schema import _fixture_models

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Every JSON type, for the annotations that accept anything (``object``
#: is Python's word for "any value", and it reaches an operator page
#: through a secret's ``backend_mappings``).
_ANY: Final = frozenset({"string", "integer", "number", "boolean", "object", "array", "null"})

#: What one Python scalar accepts, in JSON Schema's vocabulary. ``date``
#: and ``datetime`` are strings because that is what a DOCUMENT carries;
#: the model reaches them through a before-validator.
_SCALAR_TYPES: Final[dict[object, str]] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    type(None): "null",
    date: "string",
    datetime: "string",
}


def _annotation_types(annotation: object) -> frozenset[str]:
    """The JSON types a Python annotation accepts.

    Read with ``typing`` primitives rather than with the classifier, so
    this is an independent answer to the question the classifier answers.
    """
    if getattr(annotation, "__metadata__", None) is not None:
        return _annotation_types(get_args(annotation)[0])
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return frozenset[str]().union(*(_annotation_types(arg) for arg in get_args(annotation)))
    if origin is Literal:
        return frozenset(_value_type(value) for value in get_args(annotation))
    # Asked as "what does this origin BEHAVE like" rather than against a
    # list of concrete classes, so an author who spells a field with the
    # ABC (``Sequence[str]``) gets the same answer as one who spells it
    # ``list[str]``, which is the answer pydantic gives. Mapping first:
    # every mapping is also a ``Collection``, and it serializes as an
    # object rather than an array.
    if isinstance(origin, type):
        if issubclass(origin, Mapping):
            return frozenset({"object"})
        if issubclass(origin, (Sequence, AbstractSet)) and origin is not str:
            return frozenset({"array"})
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return frozenset(_value_type(member.value) for member in annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return frozenset({"object"})
    if annotation is object:
        return _ANY
    return frozenset({_SCALAR_TYPES[annotation]})


def _value_type(value: object) -> str:
    """One ``Literal`` or ``Enum`` value's JSON type. Booleans first:
    ``bool`` is a subclass of ``int``."""
    if isinstance(value, bool):
        return "boolean"
    return _SCALAR_TYPES[type(value)]


def _schema_types(node: dict[str, Any], defs: dict[str, Any]) -> frozenset[str]:
    """The JSON types an emitted subschema accepts."""
    node = _dereferenced(node, defs)
    branches = node.get("anyOf") or node.get("oneOf")
    if branches:
        return frozenset[str]().union(*(_schema_types(branch, defs) for branch in branches))
    declared = node.get("type")
    if declared is None:
        return _ANY
    return frozenset(declared) if isinstance(declared, list) else frozenset({declared})


def _dereferenced(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    return defs[ref.rsplit("/", 1)[-1]] if isinstance(ref, str) else node


def _element_nodes(node: dict[str, Any], defs: dict[str, Any], keyword: str) -> Iterator[dict[str, Any]]:
    """Every subschema under ``keyword``, across a combinator's branches.

    A branch rather than the node itself, because the shape this module
    exists for is exactly a node that is a combinator: the object arm of
    ``{anyOf: [string, object]}`` is where ``additionalProperties`` lives.
    """
    node = _dereferenced(node, defs)
    for branch in node.get("anyOf") or node.get("oneOf") or [node]:
        found = _dereferenced(branch, defs).get(keyword)
        if isinstance(found, dict):
            yield found


def _disagreement(
    annotation: object,
    node: dict[str, Any],
    defs: dict[str, Any],
    where: str,
    *,
    reverse: bool = False,
) -> str | None:
    """Where one derivation offers a type the other does not, at this
    location or inside the collection it holds: the emitted schema beyond
    the annotation normally, the annotation beyond the schema under
    ``reverse``.

    Recurses into a collection's elements rather than into a nested
    model's fields: a model is visited in its own right (every model the
    stream reaches is a subject here), while an element type is spelled
    only inside the annotation that holds it, which is where the shipped
    defect was.
    """
    schema_types = _schema_types(node, defs)
    stream_types = _annotation_types(annotation)
    extra = stream_types - schema_types if reverse else schema_types - stream_types
    if extra:
        offerer = "the field stream" if reverse else "emitted schema"
        refuser = "emitted schema" if reverse else "the field stream"
        return f"{where} accepts {sorted(extra)} in {offerer} and not in {refuser}; the stream says {annotation!r}"
    inner, keyword = _element_of(annotation)
    if inner is None:
        return None
    for element in _element_nodes(node, defs, keyword):
        found = _disagreement(inner, element, defs, f"{where}[]", reverse=reverse)
        if found is not None:
            return found
    return None


def _element_of(annotation: object) -> tuple[object | None, str]:
    """What ONE element of a collection annotation holds, and the JSON
    Schema keyword that holds its schema."""
    if getattr(annotation, "__metadata__", None) is not None:
        return _element_of(get_args(annotation)[0])
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        # A collection wrapped in an optional or in a union of scalars:
        # the collection is the one member that is one.
        for arg in args:
            inner, keyword = _element_of(arg)
            if inner is not None:
                return inner, keyword
        return None, ""
    if origin is dict and len(args) == 2:
        return args[1], "additionalProperties"
    if origin is tuple:
        return (args[0], "items") if len(args) == 2 and args[1] is Ellipsis else (None, "")
    if origin in (list, set, frozenset) and args:
        return args[0], "items"
    return None, ""


def _reachable(root: type[BaseModel]) -> set[type[BaseModel]]:
    """``root`` and every model the field stream reaches from it.

    Enumeration only. What each model ACCEPTS is answered by pydantic and
    by ``typing`` below; this just says which models get asked.
    """
    found: set[type[BaseModel]] = set()
    pending = [root]
    while pending:
        model = pending.pop()
        if model in found or not model_is_complete(model):
            continue
        found.add(model)
        for doc in iter_field_docs(model):
            arms = (arm.doc.model for arm in (*doc.union_arms, *doc.item_union_arms))
            pending.extend(child for child in (doc.nested_model, doc.item_model, *arms) if child is not None)
    return found


def _shipped_roots() -> Iterator[type[BaseModel]]:
    """Every model an operator writes against on this host: both blocks of
    every declarable kind's document, and every registered capability
    implementation's config."""
    for kind in declarable_kinds():
        yield metadata_model(kind)
        yield spec_model(kind)
    for descriptor in capability_descriptors():
        for name in registered_implementations(descriptor.kind):
            model = capability_config_model(descriptor.kind, name)
            if model is not None:
                yield model


def _subjects() -> list[type[BaseModel]]:
    """The shipped surface plus the walker fixtures, so a spelling the app
    does not ship yet is guarded the day someone writes one."""
    fixtures = (model for model in _fixture_models.ALL_FIXTURES if model_is_complete(model))
    return sorted(
        {model for root in (*_shipped_roots(), *fixtures) for model in _reachable(root)},
        key=lambda model: (model.__module__, model.__qualname__),
    )


def test_the_field_stream_accepts_every_type_the_emitted_schema_does() -> None:
    """Every buildable model, both derivations, one question.

    The shipped failure this pins: ``EnvEntry`` accepts a bare string
    through a before-validator, emitted schema said so, and the stream
    documented the table form alone, so ``agw resource describe-kind
    vm-template`` told operators to rewrite every plaintext env value into
    a table.

    One loop over the eighty-odd subjects rather than one parametrized
    case per model, because the failure mode is a SHARED base or a shared
    classifier rule: those disagree on many models at once, and a sweep
    reports that as eighty tracebacks you read one at a time where the
    loop reports it as one list of every field that drifted.
    ``_disagreement`` names its subject (``Model.field``), so nothing about
    which model broke is lost.
    """
    drifted = [
        reason
        for model in _subjects()
        for reason in _disagreements_of(model)  # noqa: PD011  (not a pandas frame)
    ]
    assert not drifted, "\n".join(drifted)


def _disagreements_of(model: type[BaseModel], *, reverse: bool = False) -> Iterator[str]:
    """Every field of ``model`` where one derivation offers a type the
    other does not, at the depth this model owns; ``reverse`` picks the
    direction (see :func:`_disagreement`)."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    properties = _dereferenced(schema, defs).get("properties", {})
    for doc in iter_field_docs(model):
        if len(doc.path) > 1:
            # A nested block's field, which is compared when its own model
            # comes up: this model's schema says only ``$ref``.
            continue
        node = properties.get(doc.path[0])
        if node is None:
            # A ROOT model has one field and no properties, and a field
            # pydantic drops (``SkipJsonSchema``) the stream drops too.
            node = schema if doc.path == ("root",) else None
        if node is None:
            continue
        where = f"{model.__qualname__}.{'.'.join(doc.path)}"
        reason = _disagreement(doc.annotation, node, defs, where, reverse=reverse)
        if reason is not None:
            yield reason


def test_the_emitted_schema_accepts_every_type_the_field_stream_offers() -> None:
    """The reverse direction, same subjects, same walk.

    A type the stream offers and the schema refuses is ``describe-kind``
    lying PERMISSIVELY: the surface the resources guide calls the
    authority tells an operator a spelling the loader's own schema
    rejects, and they find out from a red underline or a load failure
    after writing it. No shipped or fixture model disagrees this way
    today, so this is insurance at the price of one loop, kept because
    the forward guard alone would let the two derivations drift apart in
    exactly one direction.
    """
    drifted = [
        reason
        for model in _subjects()
        for reason in _disagreements_of(model, reverse=True)  # noqa: PD011  (not a pandas frame)
    ]
    assert not drifted, "\n".join(drifted)


def test_the_shipped_surface_is_actually_being_walked() -> None:
    """The subject list above is computed, so an empty or truncated one
    would make the loop pass by having nothing to check. These three are
    the ones the defect and its siblings live in."""
    subjects = _subjects()
    from agentworks.env.entry import EnvEntry
    from agentworks.plugins.onepassword.backend import OnePasswordSourceConfig

    assert EnvEntry in subjects
    assert OnePasswordSourceConfig in subjects
    assert _fixture_models.StringOrTableRoot in subjects
    assert len(subjects) > 50, f"only {len(subjects)} models walked, which is fewer than this app ships"
