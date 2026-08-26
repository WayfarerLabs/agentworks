"""The framework-wide model vocabulary: bases, field markers, walkers.

Everything an agentworks surface needs to be DECLARED rather than
hand-validated lives here:

- :class:`AgwModel` / :class:`AgwRootModel`, the shared strict, frozen,
  closed-world bases every spec and capability config model extends.
- :class:`ScalarShorthand`, the model-level declaration that a table may
  also be written as one bare scalar, from which validation, emitted
  schema, and the field-reference stream all derive.
- :class:`UnionScalarShorthand`, the explicit field-level declaration
  that a discriminated union dispatches a scalar spelling to one arm.
- :class:`SecretRef` / :class:`ResourceRef`, the ``Annotated`` markers
  that say what a field means about another Resource, and their
  ``x-agw-ref`` JSON Schema encoding. :func:`reference_marker_error` is
  the placement rule the three consumers of a marker all assume, checked
  at registration so a marker nothing could honor is never declared.
- :class:`CapabilityBlock`, the tagged table a hosting kind's spec field
  holds: the capability's name plus that capability's own config.
- :func:`filled_defaults`, the boundary fill that renders a marker's
  owner-templated default into a raw blob, so validation and extraction
  both read the one filled blob and neither needs an owner of its own.
- :func:`extract_references`, the total, never-raising reference
  extractor that reads a raw blob through a model's markers.
- :func:`iter_field_docs`, the ordered field-reference stream every human
  presentation of a model derives from, and :func:`render_type` beside
  it for the presenters that want our type rendering.
- :class:`StructuralUnion`, the declaration that an untagged union of
  closed model arms is addressed by each arm's required and allowed keys.
- :func:`config_error_from`, the bridge from a pydantic
  ``ValidationError`` to the owner-framed, located text an operator
  reads.

**This package is a LEAF, and that is load-bearing rather than tidy.** It
imports ``agentworks.errors``, ``agentworks.source_location`` and
``agentworks.path_rendering``, all top-level leaves themselves, and
nothing else of ours. In particular it
imports nothing under ``agentworks.resources``, because importing any
module of that package runs its ``__init__``, which loads every kind
module, which loads every capability package. Capability modules declare
their config models at class-definition time and so must import this
package at MODULE level; if that import dragged in the kind registry, a
capability module could not be imported on its own at all.

That is why the model layer's own reference records (``RefRelationship``
and ``ConfigReference``) live here in ``schema/reference.py`` and are
re-exported by ``resources/reference.py`` rather than the other way
round, and why this package sits at top level rather than under
``resources/``, which is where the first draft put it. It is the same
constraint ``declared_resource.py`` and ``source_location.py`` already
sit at top level for.
"""

from __future__ import annotations

from agentworks.schema._shape import (
    element_annotation,
    marker_of,
    model_is_complete,
    structural_union_error,
    union_scalar_shorthand_error,
)
from agentworks.schema.base import (
    AgwModel,
    AgwRootModel,
    NonBlankStr,
    NonEmptyStr,
    PositiveInt,
    reference_marker_error,
)
from agentworks.schema.block import CapabilityBlock
from agentworks.schema.errors import (
    MAX_ERROR_LINES,
    config_error_from,
    located,
    location_text,
)
from agentworks.schema.extract import extract_references
from agentworks.schema.fields import (
    MAPPING_KEY,
    SEQUENCE_ELEMENT,
    UNSET,
    FieldDoc,
    ModelDoc,
    UnionArm,
    iter_field_docs,
    model_doc,
    render_type,
)
from agentworks.schema.fill import filled_defaults
from agentworks.schema.markers import (
    REF_SCHEMA_KEY,
    RefMarker,
    RefOwner,
    ResourceRef,
    SecretRef,
)
from agentworks.schema.shorthand import ScalarShorthand, UnionScalarShorthand
from agentworks.schema.structural import StructuralUnion

__all__ = [
    "MAPPING_KEY",
    "MAX_ERROR_LINES",
    "REF_SCHEMA_KEY",
    "SEQUENCE_ELEMENT",
    "UNSET",
    "AgwModel",
    "AgwRootModel",
    "CapabilityBlock",
    "FieldDoc",
    "ModelDoc",
    "NonBlankStr",
    "NonEmptyStr",
    "PositiveInt",
    "RefMarker",
    "RefOwner",
    "ResourceRef",
    "ScalarShorthand",
    "SecretRef",
    "StructuralUnion",
    "UnionScalarShorthand",
    "UnionArm",
    "config_error_from",
    "element_annotation",
    "extract_references",
    "filled_defaults",
    "iter_field_docs",
    "located",
    "location_text",
    "marker_of",
    "model_doc",
    "model_is_complete",
    "reference_marker_error",
    "render_type",
    "structural_union_error",
    "union_scalar_shorthand_error",
]
