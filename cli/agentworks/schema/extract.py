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

The blob is the operator's, so its depth is theirs to choose: the walk
descends through :mod:`agentworks.traversal` rather than through Python's
stack, and ``RecursionError`` on a deeply nested document would break the
same contract a raised exception would.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, RootModel

from agentworks.schema._shape import Collection, model_fields_of, shape_of
from agentworks.schema.reference import ConfigReference
from agentworks.traversal import iter_descendants

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterator

    from agentworks.schema._shape import FieldShape, UnionArmType
    from agentworks.schema.markers import RefMarker, RefOwner


@dataclass(frozen=True)
class _Block:
    """A model read against one raw value: the unit the walk descends
    through."""

    model: type[BaseModel]
    blob: object


@dataclass(frozen=True)
class _Edge:
    """One extracted reference, carried as a node of the same walk.

    Edges travel with the blocks rather than being accumulated on the
    side so that a depth-first walk emits them in DOCUMENT order: a
    nested block's edges land between the edges of the fields declared
    before and after it, which is the order an operator reading their own
    manifest would predict.
    """

    reference: ConfigReference


_Node = _Block | _Edge


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
    this walker cannot tell the difference. An inheriting host reads its
    two kinds of edge off two different blobs (its ``inherits`` edges off
    its own declaration, its runtime needs off the merged one, FR17), and
    that is therefore two CALLS by the caller, never a parameter here.
    """
    root: _Node = _Block(model=model_cls, blob=blob)
    walk = iter_descendants(root, lambda node: _below(node, owner), key=_identity_of)
    return tuple(node.reference for node in walk if isinstance(node, _Edge))


def _identity_of(node: _Node) -> Hashable:
    """What "the same node" means to the walk, and the whole of its cycle
    guard.

    A block is identified by the pair of the model and the RAW VALUE it
    is read against, not by the model alone. Keying on the model would
    stop the walk the second time a type appeared, which for finite data
    is not a cycle at all: a secret two blocks deep in a self-recursive
    model validates fine and would simply be missing from the graph, with
    nothing reported. Keying on the value terminates on the one input
    that cannot terminate on its own, a blob reachable from itself, which
    a YAML anchor can produce.

    Identity is by ``id`` because a raw blob need not be hashable and two
    equal tables are still two blocks an operator wrote. Reuse of a freed
    ``id`` cannot bite: only the blocks on the CURRENT path are guarded,
    and every one of them is reachable from the root, hence alive.

    An edge is its own identity. It has nothing under it, so it can only
    ever suppress something else, and it must not.
    """
    return (node.model, id(node.blob)) if isinstance(node, _Block) else id(node)


def _below(node: _Node, owner: RefOwner) -> Iterator[_Node]:
    """What ``node`` contains, in declaration order: the edge each marked
    field names, and the block each nested field opens.

    An unresolvable model contributes nothing rather than raising.
    Registration-time conformance is what keeps that unreachable in
    practice.
    """
    if isinstance(node, _Edge):
        return
    for shape, present, value in _field_values(node):
        if shape.marker is not None:
            yield from _scalar_edge(shape.marker, present=present, value=value, owner=owner)
        elif shape.collection is not None:
            yield from _collection_nodes(shape, value)
        elif shape.nested_model is not None:
            yield _Block(model=shape.nested_model, blob=value)
        elif shape.arms and shape.discriminator is not None:
            yield from _arm_block(shape.arms, shape.discriminator, value)


def _field_values(block: _Block) -> Iterator[tuple[FieldShape, bool, object]]:
    """Each declared field of ``block``'s model, with the raw value the
    blob holds for it and whether the operator wrote the key at all."""
    fields = model_fields_of(block.model)
    if fields is None:
        return
    if issubclass(block.model, RootModel):
        # A root model has one field, ``root``, whose value IS the blob:
        # there is no key to read it out of. A bare-scalar root then
        # names nothing, which is every shipped backend mapping, while a
        # model-rooted one carries whatever its root model carries.
        root = fields.get("root")
        if root is not None:
            yield shape_of(root), True, block.blob
        return
    if not isinstance(block.blob, Mapping):
        return
    for name, field in fields.items():
        yield shape_of(field), name in block.blob, block.blob.get(name)


def _scalar_edge(
    marker: RefMarker,
    *,
    present: bool,
    value: object,
    owner: RefOwner,
) -> Iterator[_Edge]:
    """A marked scalar field: the operator's name, else the marker's
    owner-templated default, else nothing.

    A value that is present but is not a non-empty string destroys the
    edge's identity, so the edge is omitted rather than guessed at. That
    reproduces what every hand-rolled ``dependencies`` does today.
    """
    if isinstance(value, str) and value:
        yield _Edge(_reference(marker, value))
        return
    if present and value is not None:
        return
    default = marker.render_default(owner)
    if default:
        yield _Edge(_reference(marker, default))


def _collection_nodes(shape: FieldShape, value: object) -> Iterator[_Node]:
    """A field holding many values: every element contributes.

    An element is a marked scalar (one edge per element that names
    something), a model (a block, walked like a nested one), or a tagged
    union of models (the arm its own tag names). No template default at
    any element: a collection has no single default identity, and there
    is no element to default when the collection is absent.
    """
    for element in _elements_of(shape.collection, value):
        if shape.item_marker is not None:
            if isinstance(element, str) and element:
                yield _Edge(_reference(shape.item_marker, element))
        elif shape.item_model is not None:
            yield _Block(model=shape.item_model, blob=element)
        elif shape.item_arms and shape.item_discriminator is not None:
            yield from _arm_block(shape.item_arms, shape.item_discriminator, element)


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


def _arm_block(arms: tuple[UnionArmType, ...], discriminator: str, value: object) -> Iterator[_Block]:
    """The arm the RAW tag names, as a block to descend into.

    An absent or unrecognized tag contributes nothing; the arm is
    selected from the blob and the union type alone, never from a
    registry, which is what keeps this walk a pure function of the model.
    """
    if not isinstance(value, Mapping):
        return
    tag = value.get(discriminator)
    if not isinstance(tag, str):
        return
    for arm in arms:
        if arm.tag == tag:
            yield _Block(model=arm.model, blob=value)
            return


def _reference(marker: RefMarker, name: str) -> ConfigReference:
    return ConfigReference(
        kind=marker.kind,
        name=name,
        usage=marker.usage,
        relationship=marker.relationship,
    )
