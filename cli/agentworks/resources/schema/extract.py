"""``extract_references``: the reference edges a raw blob implies.

The dependency graph is built before anything is validated, so this
walker reads RAW values off the blob and never runs validation. What it
reads is the model's reference markers, so renaming a marked field, or
adding a second one, changes the extracted edges with no other edit
anywhere.

**Total by contract: this never raises, for any inputs whatsoever.** A
blob it cannot make sense of contributes no edges. The registry builds
edges in pass 1 and validates in pass 7, so a config with both a
malformed blob and a cycle must still report the cycle. Totality is a
property of the code, not of a guard: the walk performs only membership
tests, ``isinstance`` checks, ``Mapping.get``, and a substitution over a
placeholder vocabulary that was validated when the marker was
constructed. There is deliberately NO blanket ``except Exception``,
because that would turn a bug in this walker into silently missing graph
edges, and a graph that builds while quietly omitting a secret is worse
than a traceback.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import BaseModel, RootModel

from agentworks.resources.reference import ConfigReference
from agentworks.resources.schema._shape import Collection, model_fields_of, shape_of

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

    from agentworks.resources.schema._shape import FieldShape
    from agentworks.resources.schema.markers import RefMarker, RefOwner


def extract_references(
    model_cls: type[BaseModel],
    blob: object,
    owner: RefOwner,
) -> tuple[ConfigReference, ...]:
    """Every Resource reference ``blob`` implies under ``model_cls``, read
    structurally from the model's reference-marked fields.

    ``blob`` is ``object`` rather than a mapping type on purpose: a
    modeled surface need not be mapping-shaped (a secret backend's
    mapping is a bare string), so the extractor answers honestly rather
    than making the caller pre-screen. ``model_cls`` is widened to
    ``BaseModel`` for the same reason: a root model passes through and
    contributes nothing.

    Never raises; see the module docstring for why that is load-bearing.

    The parameters are the model, the blob, and the owner, and that is
    the whole input. This function is deliberately not given the
    registry, the config, the graph, a source location, or a
    declared-versus-effective flag: a blob validated against the same
    model extracts identically no matter where it came from, because
    this walker cannot tell the difference. Two-stage extraction
    (structural edges off the declared blob, secret edges off the
    effective one) is therefore two CALLS, never a parameter here.
    """
    found: list[ConfigReference] = []
    _walk(model_cls, blob, owner, found, ())
    return tuple(found)


def _walk(
    model_cls: type[BaseModel],
    blob: object,
    owner: RefOwner,
    found: list[ConfigReference],
    visiting: tuple[type[BaseModel], ...],
) -> None:
    """Collect ``model_cls``'s references from ``blob`` into ``found``.

    ``visiting`` is the current PATH, not an accumulating visited set:
    each recursion extends it and the extension goes out of scope on
    return. A model reachable from itself terminates; two sibling fields
    of the SAME nested model type both walk, which an accumulating set
    would silently reduce to the first one.
    """
    if model_cls in visiting:
        return
    fields = model_fields_of(model_cls)
    if fields is None:
        # An unresolvable model contributes nothing rather than raising.
        # Registration-time conformance is what keeps this unreachable
        # in practice.
        return

    visiting = (*visiting, model_cls)
    if issubclass(model_cls, RootModel):
        # A root model has one field, ``root``, whose value IS the blob:
        # there is no key to read it out of. A bare-scalar root then
        # names nothing, which is every shipped backend mapping, while a
        # model-rooted one carries whatever its root model carries.
        root = fields.get("root")
        if root is not None:
            _walk_field(root, present=True, value=blob, owner=owner, found=found, visiting=visiting)
        return

    if not isinstance(blob, Mapping):
        return
    for name, field in fields.items():
        _walk_field(field, present=name in blob, value=blob.get(name), owner=owner, found=found, visiting=visiting)


def _walk_field(
    field: FieldInfo,
    *,
    present: bool,
    value: object,
    owner: RefOwner,
    found: list[ConfigReference],
    visiting: tuple[type[BaseModel], ...],
) -> None:
    """Collect one field's references from its raw ``value``."""
    shape = shape_of(field)
    if shape.marker is not None:
        _emit_scalar(shape.marker, present=present, value=value, owner=owner, found=found)
    elif shape.collection is not None:
        _walk_collection(shape, value, owner, found, visiting)
    elif shape.nested_model is not None:
        _walk(shape.nested_model, value, owner, found, visiting)
    elif shape.arms and shape.discriminator is not None:
        _walk_union(shape, value, owner, found, visiting)


def _emit_scalar(
    marker: RefMarker,
    *,
    present: bool,
    value: object,
    owner: RefOwner,
    found: list[ConfigReference],
) -> None:
    """A marked scalar field: the operator's name, else the marker's
    owner-templated default, else nothing.

    A value that is present but is not a non-empty string destroys the
    edge's identity, so the edge is omitted rather than guessed at. That
    reproduces what every hand-rolled ``dependencies`` does today.
    """
    if isinstance(value, str) and value:
        found.append(_reference(marker, value))
        return
    if present and value is not None:
        return
    default = marker.render_default(owner)
    if default:
        found.append(_reference(marker, default))


def _walk_collection(
    shape: FieldShape,
    value: object,
    owner: RefOwner,
    found: list[ConfigReference],
    visiting: tuple[type[BaseModel], ...],
) -> None:
    """A field holding many values: every element contributes.

    Elements are either marked scalars (one edge per element that names
    something) or models (walked like a nested block). No template
    default at any element: a collection has no single default identity,
    and there is no element to default when the collection is absent.
    """
    elements = _elements_of(shape.collection, value)
    for element in elements:
        if shape.item_marker is not None:
            if isinstance(element, str) and element:
                found.append(_reference(shape.item_marker, element))
        elif shape.item_model is not None:
            _walk(shape.item_model, element, owner, found, visiting)


def _elements_of(collection: Collection | None, value: object) -> tuple[object, ...]:
    """``value`` read as the collection the field declares, or nothing.

    A sequence is a list or a tuple (what a YAML or TOML frontend
    produces), never a bare string, which is iterable and would otherwise
    decompose into characters.
    """
    if collection is Collection.MAPPING and isinstance(value, Mapping):
        return tuple(value.values())
    if collection is Collection.SEQUENCE and isinstance(value, list | tuple):
        return tuple(value)
    return ()


def _walk_union(
    shape: FieldShape,
    value: object,
    owner: RefOwner,
    found: list[ConfigReference],
    visiting: tuple[type[BaseModel], ...],
) -> None:
    """A discriminated union field: recurse into the arm the RAW tag
    names. An absent or unrecognized tag contributes nothing; the arm is
    selected from the blob and the union type alone, never from a
    registry, which is what keeps this walk a pure function of the
    model."""
    if not isinstance(value, Mapping) or shape.discriminator is None:
        return
    tag = value.get(shape.discriminator)
    if not isinstance(tag, str):
        return
    for arm in shape.arms:
        if arm.tag == tag:
            _walk(arm.model, value, owner, found, visiting)
            return


def _reference(marker: RefMarker, name: str) -> ConfigReference:
    return ConfigReference(
        kind=marker.kind,
        name=name,
        usage=marker.usage,
        relationship=marker.relationship,
    )
