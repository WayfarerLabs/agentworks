"""The framework-wide model vocabulary: bases, field markers, walkers.

Everything an agentworks surface needs to be DECLARED rather than
hand-validated lives here:

- :class:`AgwModel` / :class:`AgwRootModel`, the shared strict, frozen,
  closed-world bases every spec and capability config model extends.
- :class:`SecretRef` / :class:`ResourceRef`, the ``Annotated`` markers
  that say what a field means about another Resource, and their
  ``x-agw-ref`` JSON Schema encoding.
- :func:`extract_references`, the total, never-raising reference
  extractor that reads a raw blob through a model's markers.
- :func:`iter_field_docs`, the ordered field-reference stream every human
  presentation of a model derives from, and :func:`render_type` beside
  it for the presenters that want our type rendering.
- :func:`config_error_from` and :func:`render_validation_error`, the
  bridge from a pydantic ``ValidationError`` to the owner-framed,
  located text an operator reads.

This package sits BELOW the domains that use it. It may import
``resources/reference.py`` (the reference records it produces) and
nothing else of ours; in particular nothing here imports
``capabilities/``, ``manifests/``, ``plugins/``, or the kind registry,
which is what lets all of those import this package freely. The one edge
this package creates runs in a single direction: ``resources/schema/``
imports ``resources/reference.py``, never the reverse.
"""

from __future__ import annotations

from agentworks.resources.schema._shape import model_is_complete
from agentworks.resources.schema.base import (
    AgwModel,
    AgwRootModel,
    NonEmptyStr,
    PositiveInt,
    validation_context,
)
from agentworks.resources.schema.errors import (
    MAX_ERROR_LINES,
    FramedConfigError,
    config_error_from,
    render_validation_error,
)
from agentworks.resources.schema.extract import extract_references
from agentworks.resources.schema.fields import (
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
from agentworks.resources.schema.markers import (
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
    "FieldDoc",
    "FramedConfigError",
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
    "model_doc",
    "model_is_complete",
    "render_type",
    "render_validation_error",
    "validation_context",
]
