"""The framework-wide model vocabulary: bases, field markers, walkers.

Everything an agentworks surface needs to be DECLARED rather than
hand-validated lives here:

- :class:`AgwModel` / :class:`AgwRootModel`, the shared strict, frozen,
  closed-world bases every spec and capability config model extends.
- :class:`SecretRef` / :class:`ResourceRef`, the ``Annotated`` markers
  that say what a field means about another Resource, and their
  ``x-agw-ref`` JSON Schema encoding.
- :class:`CapabilityBlock`, the tagged table a hosting kind's spec field
  holds: the capability's name plus that capability's own config.
- :func:`extract_references`, the total, never-raising reference
  extractor that reads a raw blob through a model's markers.
- :func:`iter_field_docs`, the ordered field-reference stream every human
  presentation of a model derives from, and :func:`render_type` beside
  it for the presenters that want our type rendering.
- :func:`config_error_from`, the bridge from a pydantic
  ``ValidationError`` to the owner-framed, located text an operator
  reads.

**This package is a LEAF, and that is load-bearing rather than tidy.** It
imports ``agentworks.errors`` and ``agentworks.source_location``, both
top-level leaves themselves, and nothing else of ours. In particular it
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

from agentworks.schema._shape import marker_of, model_is_complete
from agentworks.schema.base import (
    AgwModel,
    AgwRootModel,
    NonEmptyStr,
    PositiveInt,
    validation_context,
)
from agentworks.schema.block import CapabilityBlock
from agentworks.schema.errors import (
    MAX_ERROR_LINES,
    config_error_from,
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
from agentworks.schema.markers import (
    REF_SCHEMA_KEY,
    RefMarker,
    RefOwner,
    ResourceRef,
    SecretRef,
)

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
    "NonEmptyStr",
    "PositiveInt",
    "RefMarker",
    "RefOwner",
    "ResourceRef",
    "SecretRef",
    "UnionArm",
    "config_error_from",
    "extract_references",
    "iter_field_docs",
    "marker_of",
    "model_doc",
    "model_is_complete",
    "render_type",
    "validation_context",
]
