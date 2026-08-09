"""The declaration that an untagged model union is selected by table shape.

``StructuralUnion`` is metadata on an ``Annotated`` union. Validation uses
Pydantic's ordinary untagged-union validation after canonicalizing one legacy
spelling: a uniquely selected arm beside another arm's explicit ``null``
field. The declaration tells the schema walkers that a raw table may address
one of the closed model arms by its required and allowed keys, through that
same canonicalization boundary. It also emits the alternatives as ``oneOf``:
the shapes are mutually exclusive by declaration, and ``anyOf`` would hide
that operator-facing fact from editors.

An arm may retain a marker-free scalar shorthand, as the plaintext env arm
does. Walkers select structural arms from raw TABLE keys, not from scalar
types, so registration refuses a shorthand-bearing arm that contains a
reference marker. Registration also refuses validation aliases: raw-key
selection and the emitted schema must name the same keys without a second
alias vocabulary.

The declaration is selector-free. Registration refuses a field or collection
element that combines it with any discriminator spelling rather than silently
letting tagged dispatch override structural selection.

The metadata is intentionally content-free. Required and allowed keys remain
facts of the arm models, so changing an arm changes validation, traversal,
and emitted schema without updating a parallel selector table.
"""

from __future__ import annotations

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

    def __get_pydantic_core_schema__(
        self,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Canonicalize a uniquely selected arm before union validation."""
        schema = handler(source_type)
        arms = tuple(arg for arg in get_args(source_type) if isinstance(arg, type) and issubclass(arg, BaseModel))

        def canonicalize(value: object) -> object:
            # Imported here to keep the metadata declaration independent of
            # the classifier module that imports it.
            from agentworks.schema._shape import structural_arm_and_value

            _arm, canonical = structural_arm_and_value(arms, value)
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
            schema["oneOf"] = alternatives
        return schema
