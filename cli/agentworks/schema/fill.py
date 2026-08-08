"""``filled_defaults``: owner-templated defaults, rendered into the blob.

A reference marker may declare an owner-templated default name
(``token: Annotated[str, SecretRef(usage="the auth token",
default_template="git-token-{owner_name}")]``), and something has to turn
that template into a concrete secret name for a document that omits the
field. The owner is a fact about WHERE the blob was declared, never about
its content, so the rendering happens here, at the boundary that knows
the owner, and the filled blob is what everything downstream reads:
callers fill first, then validate and extract the SAME blob. (An earlier
revision threaded the owner through pydantic's validation context and
filled inside a before-validator; that made a model unconstructible
without external context, which is a builder's fact leaking into the
model layer, and it left validation and extraction rendering the same
template independently.)

The placement buys three properties at once:

- **The rendered value is validated.** The name is written into the blob
  before pydantic sees it, so it passes through the field's own rules
  (non-empty, charset, length) exactly as an operator-written name would.
- **Model construction needs no external context.** ``model_validate``
  takes the blob and nothing else, so every model is constructible
  standalone. The price is the other direction: a boundary that validates
  a blob with templated fields must fill first, or the required field
  reports itself missing. That failure is loud and content-shaped, where
  the context mechanism's was a ``StateError`` about plumbing.
- **Extraction and validation cannot disagree on the name.** Both read
  the one filled blob, so there is no second renderer to drift;
  ``extract.py`` deliberately renders nothing.

**What is filled.** A field whose own marker declares a template and
whose value is absent or explicitly ``null``: the two spellings are one
instruction, deliberately, because emitted schema widens such a field to
nullable and the two paths must resolve the same name. The template
outranks a declared default the field may also carry, which is the
precedence the before-validator enforced by running first. A collection's
element marker cannot carry a template and a marker cannot sit on a block
(:func:`~agentworks.schema.reference_marker_error` refuses both at
registration), so a scalar write is the only write this ever makes.

**Absent fields with declared defaults are filled THROUGH.** Validation
answers an omitted field with its declared default
(``validate_default``), and a default authored as raw data may leave a
templated field inside it unset: the shipped shape is a tagged union
defaulting to one arm as a raw mapping. Such a default is materialized
into the blob as its filled self, so what pydantic validates is complete;
a default that needs no fill is left absent, exactly as written. The
substitution is :func:`~agentworks.schema.extract._absent_defaults`,
shared with extraction, so "what an omitted field is worth" has one
answer. A constructed instance default can never need a fill: building it
required every field, so its dump is complete.

**Copy-on-write, and total.** A blob nothing fills comes back as the same
object, so the common case (no templated field anywhere) costs a walk and
allocates nothing, and filling twice is the identity the second time.
Like extraction, this never raises for any input whatsoever: a value that
is not the shape the model wants is passed through untouched for
validation to refuse in its own vocabulary, and the walk descends exactly
the positions the extraction walker does, which registration conformance
guarantees is every position a marker can legally occupy. The blob's
depth is the operator's to choose, so the descent runs on an explicit
stack (:func:`_run`) rather than Python's, per the shared traversal
discipline (:mod:`agentworks.traversal`); ``RecursionError`` on a deep
document would break the same contract a raised exception would.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import BaseModel, RootModel

from agentworks.schema._shape import Collection, model_fields_of, shape_of, table_addresses_block
from agentworks.schema.extract import _absent_defaults
from agentworks.schema.shorthand import scalar_shorthand_of

if TYPE_CHECKING:
    from collections.abc import Generator, Hashable

    from pydantic.fields import FieldInfo

    from agentworks.schema._shape import FieldShape, UnionArmType
    from agentworks.schema.markers import RefOwner

    #: One step of the walk: a generator that yields subtasks and returns
    #: the (possibly rewritten) value it was asked about.
    type _Task = Generator[_Task, object, object]

    #: The blocks open above the current one, as ``(model, id(blob))``
    #: pairs: the same cycle guard extraction keys its walk on, for the
    #: same one input that cannot terminate on its own (a blob reachable
    #: from itself, which a YAML anchor can produce). One mutable set per
    #: ``filled_defaults`` call; tasks complete strictly innermost-first,
    #: so add-then-discard reproduces the current path exactly.
    type _Path = set[Hashable]


def filled_defaults(model_cls: type[BaseModel], blob: object, owner: RefOwner) -> object:
    """``blob`` as validation and extraction must read it: every
    owner-templated default ``model_cls`` declares rendered in, at every
    depth a document can reach.

    Returns ``blob`` itself (the same object) when nothing fills, and a
    copy along the filled paths otherwise; the input is never mutated.
    Never raises, for any inputs whatsoever; see the module docstring for
    both contracts and for exactly what is filled.
    """
    return _run(_filled_block(model_cls, blob, owner, set()))


def _run(task: _Task) -> object:
    """Drive a task tree to its result on an explicit stack.

    The walk below is written as ordinary-looking functions whose
    recursive calls are spelled ``yield``: each yields the subtask where
    a call would recurse and is handed that subtask's result back. This
    driver is what makes that shape iterative, so a document's depth
    costs heap frames rather than Python stack, honoring the traversal
    discipline a rewriting walk cannot get from
    :func:`agentworks.traversal.iter_descendants` (which visits nodes but
    cannot hand a child's REBUILT value back to its parent).
    """
    stack = [task]
    sent: object = None
    while stack:
        try:
            child = stack[-1].send(sent)
        except StopIteration as done:
            sent = done.value
            stack.pop()
        else:
            stack.append(child)
            sent = None
    return sent


def _filled_block(model_cls: type[BaseModel], value: object, owner: RefOwner, on_path: _Path) -> _Task:
    """One model read against one raw value: the unit the walk descends
    through, mirroring extraction's ``_Block``.

    A bare scalar against a model that declares a shorthand is folded
    first, exactly as the base model's before-validator folds it, and for
    the same order reason: the fill acts on a mapping, so a shorthand
    value folded after it would leave templated fields unresolved for
    exactly the operators who wrote the short spelling. The fold is kept
    only when something inside it fills; otherwise the operator's scalar
    comes back untouched and the validator folds it itself.

    ``dict``, not ``Mapping``, mirroring the strict posture: strict mode
    accepts a dict or a model instance and nothing else, so a filled copy
    of some other mapping type would VALIDATE a value the model layer
    refuses unfilled.
    """
    key = (model_cls, id(value))
    if key in on_path:
        return value
    fields = model_fields_of(model_cls)
    if fields is None:
        return value
    on_path.add(key)
    try:
        if issubclass(model_cls, RootModel):
            # A root model has one field, ``root``, whose value IS the blob.
            root = fields.get("root")
            return value if root is None else (yield _filled_value(shape_of(root), value, owner, on_path))
        if isinstance(value, dict):
            return (yield _filled_fields(model_cls, fields, value, owner, on_path))
        shorthand = scalar_shorthand_of(model_cls)
        folded = value if shorthand is None else shorthand.folded(value)
        if folded is value or not isinstance(folded, dict):
            return value
        filled = yield _filled_fields(model_cls, fields, folded, owner, on_path)
        return value if filled is folded else filled
    finally:
        on_path.discard(key)


def _filled_fields(
    model_cls: type[BaseModel],
    fields: Mapping[str, FieldInfo],
    data: dict[object, object],
    owner: RefOwner,
    on_path: _Path,
) -> _Task:
    defaults = _absent_defaults(model_cls)
    updates: dict[object, object] = {}
    for name, field in fields.items():
        shape = shape_of(field)
        if shape.marker is not None:
            # The field's own marker: a scalar position, so a rendered
            # name is the only thing that could go here. Absent and null
            # are one instruction, and the template outranks a declared
            # default; both facts are the module docstring's.
            if shape.marker.default_template is None or data.get(name) is not None:
                continue
            rendered = shape.marker.render_default(owner)
            if rendered:
                updates[name] = rendered
            continue
        held = data[name] if name in data else defaults.get(name)
        if held is None:
            continue
        filled = yield _filled_value(shape, held, owner, on_path)
        if filled is not held:
            updates[name] = filled
    return {**data, **updates} if updates else data


def _filled_value(shape: FieldShape, value: object, owner: RefOwner, on_path: _Path) -> _Task:
    """One field's raw value, filled through whatever block it opens:
    directly, through a tag, through a collection's elements, or by being
    written as a table where an untagged union offers one. The same four
    descents extraction makes, because a position extraction cannot walk
    is a position no marker may occupy."""
    if shape.collection is not None:
        return (yield _filled_collection(shape, value, owner, on_path))
    if shape.nested_model is not None:
        return (yield _filled_block(shape.nested_model, value, owner, on_path))
    if shape.arms and shape.discriminator is not None:
        model = _armed_model(shape.arms, shape.discriminator, value)
        return value if model is None else (yield _filled_block(model, value, owner, on_path))
    if shape.union_model is not None:
        return (yield _filled_union(shape.union_model, shape.union_members, value, owner, on_path))
    return value


def _filled_collection(shape: FieldShape, value: object, owner: RefOwner, on_path: _Path) -> _Task:
    """Each element filled in place, the container's own type kept.

    An element that is a marked scalar is untouched: an element marker
    cannot carry a template (refused at registration), so there is
    nothing to render per element.
    """
    if shape.collection is Collection.MAPPING and isinstance(value, dict):
        updates: dict[object, object] = {}
        for key, element in value.items():
            filled = yield _filled_element(shape, element, owner, on_path)
            if filled is not element:
                updates[key] = filled
        return {**value, **updates} if updates else value
    if shape.collection is Collection.SEQUENCE and isinstance(value, list | tuple):
        elements = []
        for element in value:
            elements.append((yield _filled_element(shape, element, owner, on_path)))
        if all(filled is element for filled, element in zip(elements, value, strict=True)):
            return value
        return tuple(elements) if isinstance(value, tuple) else elements
    return value


def _filled_element(shape: FieldShape, element: object, owner: RefOwner, on_path: _Path) -> _Task:
    if shape.item_model is not None:
        return (yield _filled_block(shape.item_model, element, owner, on_path))
    if shape.item_arms and shape.item_discriminator is not None:
        model = _armed_model(shape.item_arms, shape.item_discriminator, element)
        return element if model is None else (yield _filled_block(model, element, owner, on_path))
    if shape.item_union_model is not None:
        return (yield _filled_union(shape.item_union_model, shape.item_union_members, element, owner, on_path))
    return element


def _filled_union(
    model: type[BaseModel],
    members: tuple[object, ...],
    value: object,
    owner: RefOwner,
    on_path: _Path,
) -> _Task:
    """The one model arm of an UNdiscriminated union, filled when a table
    can only be that arm: the same ``table_addresses_block`` call
    extraction makes, so the two walks address exactly the same blocks."""
    if not isinstance(value, Mapping) or not table_addresses_block(model, members):
        return value
    return (yield _filled_block(model, value, owner, on_path))


def _armed_model(arms: tuple[UnionArmType, ...], discriminator: str, value: object) -> type[BaseModel] | None:
    """The arm the RAW tag names, or ``None`` for a tag naming no arm,
    which validation will refuse in its own vocabulary."""
    if not isinstance(value, Mapping):
        return None
    tag = value.get(discriminator)
    return next((arm.model for arm in arms if arm.tag == tag), None)
