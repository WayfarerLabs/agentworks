"""The declaration that an untagged model union is selected by table shape.

``StructuralUnion`` is metadata on an ``Annotated`` union. Validation normally
uses Pydantic's ordinary untagged-union validation unchanged. A compatibility
option canonicalizes one legacy spelling: a uniquely selected arm beside
another arm's explicit ``null`` field. The declaration tells the schema
walkers that a raw table may address one of the closed model arms by its
required and allowed keys, through that same boundary. It also emits the
alternatives as ``oneOf``: the shapes are mutually exclusive by declaration,
and ``anyOf`` would hide that operator-facing fact from editors.

An arm may retain a marker-free scalar shorthand, as the plaintext env arm
does. Walkers select structural arms from raw TABLE keys, not from scalar
types, so registration refuses a shorthand-bearing arm that contains a
reference marker. Registration also refuses validation aliases: raw-key
selection and the emitted schema must name the same keys without a second
alias vocabulary.

The declaration is selector-free. Registration refuses a field or collection
element that combines it with any discriminator spelling rather than silently
letting tagged dispatch override structural selection.

Required and allowed keys remain facts of the arm models, so changing an arm
changes validation, traversal, and emitted schema without updating a parallel
selector table. The one option declares compatibility behavior rather than
duplicating any arm shape.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args

from pydantic import BaseModel
from pydantic_core import core_schema

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema


@dataclass(frozen=True)
class StructuralUnion:
    """Mark an untagged union of closed models as shape-addressable."""

    canonicalize_null_companions: bool = False
    """Accept another arm's fields only when they are explicitly ``null``.

    This is a compatibility boundary for a union replacing an older combined
    model whose persisted dump included every nullable field. Leave it false
    for a newly declared structural union.
    """

    def __get_pydantic_core_schema__(
        self,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Canonicalize a uniquely selected arm before union validation."""
        schema = handler(source_type)
        if not self.canonicalize_null_companions:
            return schema
        arms = tuple(arm for arg in get_args(source_type) if (arm := _model_arm(arg)) is not None)

        def canonicalize(value: object) -> object:
            # Imported here to keep the metadata declaration independent of
            # the classifier module that imports it.
            from agentworks.schema._shape import structural_arm_and_value

            _arm, canonical = structural_arm_and_value(
                arms,
                value,
                canonicalize_null_companions=True,
            )
            return canonical

        return core_schema.no_info_before_validator_function(canonicalize, schema)

    def __get_pydantic_json_schema__(
        self,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Render Pydantic's untagged alternatives as a plain ``oneOf``."""
        schema = handler(core_schema)
        alternatives = schema.pop("anyOf", None)
        if alternatives is not None:
            schema["oneOf"] = (
                _with_nullable_companions(alternatives, handler) if self.canonicalize_null_companions else alternatives
            )
        return schema


def _with_nullable_companions(
    alternatives: list[JsonSchemaValue],
    handler: GetJsonSchemaHandler,
) -> list[JsonSchemaValue]:
    """Widen each table arm by only the other arms' ``null`` properties."""
    widened = [deepcopy(handler.resolve_ref_schema(alternative)) for alternative in alternatives]
    objects = [_root_object_schema(alternative) for alternative in widened]
    known = {
        name for object_schema in objects if object_schema is not None for name in object_schema.get("properties", {})
    }
    for object_schema in objects:
        if object_schema is None:
            continue
        properties = object_schema.get("properties")
        if not isinstance(properties, dict):
            continue
        for name in known - properties.keys():
            properties[name] = {"type": "null"}
    return widened


def _model_arm(annotation: object) -> type[BaseModel] | None:
    """A model union arm after peeling its local ``Annotated`` wrappers."""
    while getattr(annotation, "__metadata__", None) is not None:
        annotation = get_args(annotation)[0]
    return annotation if isinstance(annotation, type) and issubclass(annotation, BaseModel) else None


def _root_object_schema(schema: JsonSchemaValue) -> JsonSchemaValue | None:
    """The root table branch in one model arm, including a scalar shorthand."""
    if schema.get("type") == "object":
        return schema
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        objects = [branch for branch in branches if isinstance(branch, dict) and branch.get("type") == "object"]
        if len(objects) == 1:
            return objects[0]
    return None
