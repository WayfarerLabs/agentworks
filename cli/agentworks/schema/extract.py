"""``extract_references``: the reference edges a raw blob implies.

The dependency graph is built before anything is validated, so this
walker reads RAW values off the blob and never runs validation. What it
reads is the model's reference markers, so renaming a marked field, or
adding a second one, changes the extracted edges with no other edit
anywhere.

**This walker renders nothing.** An owner-templated default is resolved
into the blob by the boundary fill
(:func:`~agentworks.schema.filled_defaults`) before either validation or
extraction reads it, so the blob handed here already carries every name
the validated config will carry, and there is no second renderer to
disagree with. Hand it the blob the fill ran over, which is the blob
validation is (or would be) given; an unfilled blob extracts no edge for
a templated field, exactly as it fails validation.

**An absent field with a declared default is read as if the operator had
written the default's value.** Validation answers absence with the
default, so a default that names a Resource (a marked field's own plain
default, or a defaulted block with a marked field somewhere inside it)
is a name the validated config really carries, and therefore an edge the
graph must have: a secret arriving through a default is gated, resolved,
and reported like any other. The substitution is the whole mechanism:
the default is turned into the plain blob it is equivalent to, once per
model (:func:`_absent_defaults`), and the ordinary walk descends into
it, so a nested block, a union inside a union arm, and a collection
inside a default all extract with no walking logic of their own, and a
walker fix or bug reaches defaults and documents alike, which is what
keeps the two from disagreeing. This stays pre-validation in character:
a default is a fact the AUTHOR wrote on the class, known before any
document exists.

**Total by contract: this never raises, for any inputs whatsoever.** A
blob it cannot make sense of contributes no edges. The registry builds
edges in pass 1 and validates in pass 7, so a config with both a
malformed blob and a cycle must still report the cycle. Totality is a
property of the code, not of a guard: the walk performs only membership
tests, ``isinstance`` checks, and ``Mapping.get``.
There is deliberately NO blanket ``except Exception``,
because that would turn a bug in this walker into silently missing graph
edges, and a graph that builds while quietly omitting a secret is worse
than a traceback. The one narrow guard sits in
:func:`_absent_defaults`, around the author's own default machinery
rather than around the walk; see there for why that failure class is
safe to absorb.

The blob is the operator's, so its depth is theirs to choose: the walk
descends through :mod:`agentworks.traversal` rather than through Python's
stack, and ``RecursionError`` on a deeply nested document would break the
same contract a raised exception would.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from weakref import WeakKeyDictionary

from pydantic import BaseModel, RootModel

from agentworks.schema._shape import (
    Collection,
    addressed_arm_model,
    model_fields_of,
    shape_of,
    structural_arm_for,
    table_addresses_block,
)
from agentworks.schema.reference import ConfigReference
from agentworks.schema.shorthand import scalar_shorthand_of
from agentworks.traversal import iter_descendants

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterator

    from pydantic.fields import FieldInfo

    from agentworks.schema._shape import FieldShape, UnionArmType
    from agentworks.schema.markers import RefMarker
    from agentworks.schema.shorthand import UnionScalarShorthand


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

    The parameters are the model and the blob, and that is the whole
    input: extraction is a pure function of the two. There is no owner
    here, and that absence is the design: the owner's one job was
    rendering templated defaults, which the boundary fill now writes into
    the blob before this reads it. Neither is this given the registry,
    the config, the graph, a source location, or a
    declared-versus-effective flag: a blob validated against the same
    model extracts identically no matter where it came from, because
    this walker cannot tell the difference. An inheriting host reads its
    two kinds of edge off two different blobs (its ``inherits`` edges off
    its own declaration, its runtime needs off the merged one, FR17), and
    that is therefore two CALLS by the caller, never a parameter here.
    """
    root: _Node = _Block(model=model_cls, blob=blob)
    walk = iter_descendants(root, _below, key=_identity_of)
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


def _below(node: _Node) -> Iterator[_Node]:
    """What ``node`` contains, in declaration order: the edge each marked
    field names, and the block each nested field opens, whether it opens
    it outright, through a tag, or by being written as a table where a
    union offers one.

    An unresolvable model contributes nothing rather than raising.
    Registration-time conformance is what keeps that unreachable in
    practice.
    """
    if isinstance(node, _Edge):
        return
    for shape, value in _field_values(node):
        if shape.marker is not None:
            yield from _scalar_edge(shape.marker, value)
        elif shape.collection is not None:
            yield from _collection_nodes(shape, value)
        elif shape.nested_model is not None:
            yield _Block(model=shape.nested_model, blob=value)
        elif shape.arms and shape.discriminator is not None:
            yield from _arm_block(shape.arms, shape.discriminator, shape.union_scalar_shorthand, value)
        elif shape.structural_arms:
            yield from _structural_block(shape.structural_arms, value)
        elif shape.union_model is not None:
            yield from _union_block(shape.union_model, shape.union_members, value)


def _field_values(block: _Block) -> Iterator[tuple[FieldShape, object]]:
    """Each declared field of ``block``'s model, with the raw value the
    blob holds for it.

    An absent field arrives carrying its declared default's blob (or
    ``None`` when it has none): validation answers absence with the
    default, so the default is what the field is worth.
    """
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
            yield shape_of(root), block.blob
        return
    if not isinstance(block.blob, Mapping):
        return
    defaults = _absent_defaults(block.model)
    for name, field in fields.items():
        yield shape_of(field), block.blob[name] if name in block.blob else defaults.get(name)


def _scalar_edge(marker: RefMarker, value: object) -> Iterator[_Edge]:
    """A marked scalar field: the name it holds, or nothing.

    ``value`` is the operator's own string, the rendered name the
    boundary fill wrote for an omitted templated field, or the field's
    declared default (which is what an absent field arrives carrying);
    the walker cannot tell which, and does not need to. Anything that is
    not a non-empty string destroys the edge's identity, so the edge is
    omitted rather than guessed at, which is also what a written ``null``
    on an untemplated marked field resolves to.
    """
    if isinstance(value, str) and value:
        yield _Edge(_reference(marker, value))


def _collection_nodes(shape: FieldShape, value: object) -> Iterator[_Node]:
    """A field holding many values: every element contributes.

    An element is a marked scalar (one edge per element that names
    something), a model (a block, walked like a nested one), a tagged
    union of models (the arm its own tag names), or an untagged
    scalar-or-block union (the block, when the element is written as one).
    No owner template at any element: a collection has no single default
    identity. An ABSENT collection still contributes what its declared
    default holds, because ``value`` arrives as that default's blob, so a
    default with named elements extracts them like a written one.
    """
    for element in _elements_of(shape.collection, value):
        if shape.item_marker is not None:
            if isinstance(element, str) and element:
                yield _Edge(_reference(shape.item_marker, element))
        elif shape.item_model is not None:
            yield _Block(model=shape.item_model, blob=element)
        elif shape.item_arms and shape.item_discriminator is not None:
            yield from _arm_block(
                shape.item_arms,
                shape.item_discriminator,
                shape.item_union_scalar_shorthand,
                element,
            )
        elif shape.item_structural_arms:
            yield from _structural_block(shape.item_structural_arms, element)
        elif shape.item_union_model is not None:
            yield from _union_block(shape.item_union_model, shape.item_union_members, element)


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


def _arm_block(
    arms: tuple[UnionArmType, ...],
    discriminator: str,
    union_shorthand: UnionScalarShorthand | None,
    value: object,
) -> Iterator[_Block]:
    """The arm the raw value names, as a block to descend into.

    A table names its arm by tag. A scalar names the unique arm that
    declares it as shorthand, and is folded through that same declaration
    before the block walk reads its fields. An absent, unrecognized, or
    ambiguous value contributes nothing. Selection comes from the blob and
    the union type alone, never from a registry, which keeps this walk a
    pure function of the model.

    Every ``arm.tag`` is a string, because
    :func:`~agentworks.schema._shape._tags_of` keeps only string tags when
    it classifies the union. So a tag an operator wrote as anything else
    simply matches no arm. Scalar eligibility likewise belongs on the
    model as its ``ScalarShorthand`` declaration.
    """
    model = addressed_arm_model(arms, discriminator, union_shorthand, value)
    if model is not None:
        shorthand = scalar_shorthand_of(model)
        blob = value if shorthand is None else shorthand.folded(value)
        yield _Block(model=model, blob=blob)


def _structural_block(arms: tuple[type[BaseModel], ...], value: object) -> Iterator[_Block]:
    """The one closed arm a raw table's required and allowed keys select."""
    arm = structural_arm_for(arms, value)
    if arm is not None:
        yield _Block(model=arm, blob=value)


def _union_block(model: type[BaseModel], members: tuple[object, ...], value: object) -> Iterator[_Block]:
    """The one model arm of an UNdiscriminated union, when a table can
    only be that arm.

    Nothing tags such a union, so what addresses an arm from a raw blob is
    the value's own shape: a table is the block, and a scalar is one of the
    scalar members, which have nothing to walk. A field spelled
    ``str | Creds`` accepts every block ``Creds`` accepts, so a secret
    named inside one is a secret the operator declared, and an arm left
    unwalked drops it from the dependency graph with nothing reported.

    "A table is the block" is a fact rather than a guess only while the
    block is the one member a table could satisfy, which is what
    :func:`~agentworks.schema._shape.table_addresses_block` decides. A
    union offering a bare table beside the model (``dict[str, str] |
    Creds``) addresses no arm before validation, since pydantic settles
    that one by trying the arms and preferring whichever fits; naming the
    block there would invent an edge for a value that validates as the
    table. That is the refusal the classifier already makes for a union
    holding two models, one member further out. The predicate is shared
    with registration conformance, which refuses a marker inside a block
    this refusal leaves unwalked; the sound refusal here and the loud one
    there are two halves of one rule.

    A scalar contributes nothing here rather than being walked as a block,
    and the check is explicit rather than left to
    :func:`_field_values`'s mapping test, because a ROOT model arm has no
    such test: it reads the blob directly, so ``str | SomeRootModel`` would
    extract the operator's plain string as though it were the root model's
    marked value.
    """
    if not isinstance(value, Mapping):
        return
    if not table_addresses_block(model, members):
        return
    yield _Block(model=model, blob=value)


def _reference(marker: RefMarker, name: str) -> ConfigReference:
    return ConfigReference(
        kind=marker.kind,
        name=name,
        usage=marker.usage,
        relationship=marker.relationship,
    )


#: Per-class cache for :func:`_absent_defaults`. Weak for the reason
#: ``base._MARKED_FIELD_CACHE`` is: a model class defined inside a test
#: stays collectable.
_ABSENT_DEFAULT_CACHE: Final[WeakKeyDictionary[type[BaseModel], dict[str, object]]] = WeakKeyDictionary()


def _absent_defaults(model_cls: type[BaseModel]) -> dict[str, object]:
    """What each defaulted field of ``model_cls`` is worth when a document
    omits it, as the plain blob the walk reads: field name to
    as-if-written value, with the no-default fields simply missing.

    Computed once per class and cached, which is what keeps the
    substitution static: a default is class-level data, so the answer
    cannot vary per document, and evaluating it here rather than per blob
    keeps extraction's cost a function of the document.

    Two default spellings, two treatments:

    - A CONSTRUCTED default (a model instance, or a collection holding
      one) is dumped to the mapping it is equivalent to. Such an instance
      never carries an UNSET owner-templated field: building it required
      every field, so its dump is complete.
    - A RAW value (a scalar, or a plain mapping or list the author wrote
      as data) passes through untouched, and this is the spelling that
      CAN leave an owner-templated field unset: the boundary fill
      materializes such a default into the blob, filled, before either
      validation or this walk reads it, so what descends here already
      carries the rendered name.

    Shared with the boundary fill
    (:mod:`agentworks.schema.fill`), which substitutes an absent field's
    default the same way before deciding whether anything inside it needs
    filling; one function is what keeps "what an omitted field is worth"
    a single answer.

    A ``default_factory`` is called once here, like the field-doc
    stream's ``_default_of``, unless it takes validated data: that
    default is a function of the neighboring fields, so it is not a
    static property of the class, and its edges deliberately cannot be
    pre-computed. A config leaning on such a factory to NAME a Resource
    would validate while extraction missed the edge, which is why no
    shipped model does it; the honest state is recorded here rather than
    stretched over.

    The ``except`` guards the one region where AUTHOR code runs (a
    default factory, a serializer a dump invokes), not the walk: a
    failure there is an authoring bug that cannot be an extraction bug,
    and raising it would break graph building for every resource over
    one broken class, against the module's totality contract. The failed
    field contributes no substitution, which answers exactly as the
    walker always has for a value it cannot make sense of.
    """
    cached = _ABSENT_DEFAULT_CACHE.get(model_cls)
    if cached is not None:
        return cached
    defaults: dict[str, object] = {}
    fields = model_fields_of(model_cls) or {}
    for name, field in fields.items():
        try:
            value = _declared_default(field)
            if value is not None:
                # Inside the guard on purpose: _as_blob dumps the value, and a
                # dump runs the author's serializers. A raising field_serializer
                # on a defaulted instance would otherwise escape as an
                # exception from a walk that promises never to raise.
                blob = _as_blob(value)
        except Exception:  # noqa: BLE001  (see the docstring: author code, not the walk)
            continue
        if value is not None:
            defaults[name] = blob
    _ABSENT_DEFAULT_CACHE[model_cls] = defaults
    return defaults


def _declared_default(field: FieldInfo) -> object:
    """The value validation would use for this field when absent, or
    ``None`` when there is none (a required field, an unevaluable
    factory, or a genuine ``None`` default, which names nothing and
    substitutes nothing)."""
    if field.is_required():
        return None
    if field.default_factory is None:
        return field.default
    if field.default_factory_takes_validated_data:
        return None
    return field.get_default(call_default_factory=True)


def _as_blob(value: object) -> object:
    """``value`` as the plain data a document writing it would hold.

    A model instance becomes its dump, a collection converts its values
    and keeps its shape, and anything else (a scalar, an author's raw
    mapping) is already the blob it is.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return {key: _as_blob(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_as_blob(item) for item in value]
    return value
