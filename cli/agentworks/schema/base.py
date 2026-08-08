"""``AgwModel`` and ``AgwRootModel``: the shared model bases.

Every modeled agentworks surface (kind specs and capability config alike)
extends one of these two, so the posture below is stated once and holds
everywhere:

- **strict**: no silent coercion. The manifest frontend is YAML through
  pyyaml's safe loader, which already yields real ``int`` / ``float`` /
  ``bool`` / ``str`` / ``None`` / ``list`` / ``dict``, so a quoted
  ``"8"`` where an integer belongs is an operator mistake, not a value
  to convert. (``int`` IS accepted where a ``float`` is declared, which
  is pydantic's strict semantics and the one conversion we want:
  ``memory: 8`` against ``memory: float``.)
- **frozen**: declaration objects are immutable, matching the
  frozen-dataclass discipline the registry already relies on.
- **closed world**: an unknown key is a hard error, not a warning.
- **validated defaults**: a declared default is checked rather than
  trusted. Note when: on validation of a document that OMITS the field,
  not at class definition.
- **re-validated instances**: a nested model instance is re-checked
  rather than trusted, so binding an already-built instance cannot
  smuggle unvalidated data past the boundary.

Where a single field genuinely wants a lenient rule, the opt-in is per
field (``Annotated[float, Field(strict=False)]``) with a comment saying
why, never a relaxation of the config below. One global posture, local
exceptions a reader can see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final
from weakref import WeakKeyDictionary

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from agentworks.errors import StateError
from agentworks.schema._shape import (
    marker_of,
    markers_in,
    model_fields_of,
    models_in,
    shape_of,
    structurally_addressable_arms,
    table_addresses_block,
)
from agentworks.schema.markers import REF_SCHEMA_KEY
from agentworks.schema.shorthand import ScalarShorthand, shorthand_field_error

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic.fields import FieldInfo
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

    from agentworks.schema._shape import FieldShape
    from agentworks.schema.markers import RefMarker

#: Per-class cache for :func:`_marked_fields`.
_MARKED_FIELD_CACHE: Final[WeakKeyDictionary[type[BaseModel], tuple[tuple[str, RefMarker], ...]]] = WeakKeyDictionary()

# The settings both bases share. Kept as one literal so the two configs
# below cannot drift; the ONLY intended difference between them is
# ``extra``.
_SHARED_SETTINGS: Final = ConfigDict(
    frozen=True,
    strict=True,
    validate_default=True,
    use_attribute_docstrings=True,
    revalidate_instances="always",
)

_AGW_MODEL_CONFIG: Final = ConfigDict(extra="forbid", **_SHARED_SETTINGS)

# No ``extra`` here, and that is not a style choice: ``RootModel``
# refuses the setting outright (``__init_subclass__`` raises
# ``PydanticUserError`` with code ``root-model-extra``), so a shared
# config carrying ``extra="forbid"`` would fail at class definition and
# this module would not import. Closed-world is not weakened by the
# split, because a root model has no keys of its own to be unknown: its
# strictness is its root type, and a root model wrapping an ``AgwModel``
# inherits that model's ``extra="forbid"`` for the mapping it wraps.
_AGW_ROOT_MODEL_CONFIG: Final = ConfigDict(**_SHARED_SETTINGS)


class AgwModel(BaseModel):
    """Base for every mapping-shaped agentworks schema model: kind specs
    and capability config alike. Strict, frozen, closed-world.

    See the module docstring for what each setting buys.
    """

    model_config = _AGW_MODEL_CONFIG

    scalar_shorthand: ClassVar[ScalarShorthand | None] = None
    """The bare-scalar spelling this model ALSO accepts, or ``None`` for
    the models that are only ever written as a table.

    One authored fact, three derivations, which is the whole point of
    declaring it here rather than hand-writing each of them: the
    before-validator below folds the scalar, the JSON Schema hook below
    offers it as an arm, and the field-documentation stream widens every
    annotation naming this model
    (:func:`~agentworks.schema._shape.accepted_annotation`). See
    :mod:`agentworks.schema.shorthand` for why a hand-written pair of the
    first two left the third silently wrong.
    """

    @model_validator(mode="before")
    @classmethod
    def _fold_scalar_shorthand(cls, data: Any) -> Any:
        """``data`` as the mapping a bare-scalar shorthand stands for, so
        everything after this point sees one shape.

        The ONLY rewrite this base makes. In particular, an owner-templated
        default is deliberately not resolved here: rendering it needs the
        owner, which is a fact about where the blob was declared and not
        about its content, so the boundary that knows the owner renders it
        into the blob before validation ever runs
        (:func:`~agentworks.schema.filled_defaults`). Keeping the model
        layer owner-free is what makes every model constructible standalone.
        """
        return data if cls.scalar_shorthand is None else cls.scalar_shorthand.folded(data)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse a shorthand that folds into a field the model does not
        have, at import of the module declaring it.

        Pydantic's own post-build hook rather than ``__init_subclass__``,
        because ``model_fields`` is what the check reads and it does not
        exist until the class is built. See
        :func:`~agentworks.schema.shorthand.shorthand_field_error` for
        what the author would otherwise see instead.
        """
        super().__pydantic_init_subclass__(**kwargs)
        reason = shorthand_field_error(cls)
        if reason is not None:
            raise StateError(reason)

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """The corrections emitted schema needs and pydantic cannot make.

        **A model with a scalar shorthand accepts that scalar too**, and
        nothing pydantic can see says so: the fold is a before-validator,
        which ``model_json_schema`` does not read. Left out, a
        schema-aware editor flags ``FOO: a value`` in every env table an
        operator writes. The arm is generated from the shorthand's CORE
        schema through the caller's own handler rather than written as a
        literal, so the emitter's YAML-spelling widening still applies to
        it (see :mod:`agentworks.schema.shorthand`).

        The rest are corrections to the model's own object schema, and
        they go INSIDE that arm rather than around the union: they
        describe the table form's properties.
        """
        json_schema = _with_marker_corrections(cls, handler(core_schema), handler)
        shorthand = cls.scalar_shorthand
        if shorthand is None:
            return json_schema
        return {"anyOf": [handler(shorthand.core_schema()), json_schema]}


def _with_marker_corrections(
    model_cls: type[BaseModel],
    json_schema: JsonSchemaValue,
    handler: GetJsonSchemaHandler,
) -> JsonSchemaValue:
    """``json_schema`` with the two reference-marker corrections applied.

    **An owner-templated field is neither required nor non-nullable.**
    The boundary fill (:func:`~agentworks.schema.filled_defaults`)
    resolves such a field from its marker before validation reads the
    blob, and it treats an omitted value and an explicit ``null`` alike,
    on purpose: the two spellings are one instruction, and both resolve
    to the same name extraction reads off the same filled blob. Emitted
    schema has to state BOTH halves of that, and pydantic can state
    neither, because it computes ``required`` and nullability from the
    declared field and knows nothing about the fill:

    - not required, or an editor red-underlines the very omission the
      mechanism exists to resolve (the stored git-token arm with no
      ``secret`` key);
    - nullable, or it red-underlines ``secret: null``, which is that same
      instruction spelled out, and which loads today.

    **Every marked field carries its ``x-agw-ref`` on the property
    itself**, which is the position the marker's whole purpose depends on
    being readable; see :func:`_ref_at_top_level`. That half runs for
    every marked field, templated or not, because the widening above is
    only one of the two ways a marker ends up buried.

    Stated here, on the base every filled model extends, rather than in
    the emitter: both rules are properties of how the framework loads
    these models, and a consumer correcting for them downstream would be
    a second place to keep in sync.
    """
    marked = _marked_fields(model_cls)
    if not marked:
        return json_schema
    templated = {name for name, marker in marked if marker.default_template is not None}
    # The model's schema may arrive as a ``$ref`` into ``$defs``; the
    # required list and the properties live on the definition it points at.
    resolved = handler.resolve_ref_schema(json_schema)
    if templated:
        required = [name for name in resolved.get("required", ()) if name not in templated]
        if required:
            resolved["required"] = required
        else:
            resolved.pop("required", None)
    properties = resolved.get("properties", {})
    for name, _marker in marked:
        # A hidden field (``SkipJsonSchema``) has no property to correct,
        # which is the same absence the field-reference stream honors.
        prop = properties.get(name)
        if prop is None:
            continue
        properties[name] = _ref_at_top_level(_nullable(prop) if name in templated else prop)
    return json_schema


class AgwRootModel[T](RootModel[T]):
    """Base for a modeled surface whose value is NOT a mapping.

    A secret backend's mapping is the shipped example: env-var's is a
    bare string and onepassword's is a string or a table, neither of
    which a ``BaseModel`` can be. Same settings as :class:`AgwModel`
    minus ``extra``, which ``RootModel`` refuses; see the comment on
    ``_AGW_ROOT_MODEL_CONFIG``.
    """

    model_config = _AGW_ROOT_MODEL_CONFIG


NonEmptyStr = Annotated[str, Field(min_length=1)]
"""A string an operator must actually fill in.

Declared once here rather than spelled ``Field(min_length=1)`` at each
of the two dozen fields that want it, because a floor that drifts is a
floor nobody notices drifting. The bridge renders its violation as
"must not be empty" (only at a floor of 1, where that paraphrase is
true)."""

PositiveInt = Annotated[int, Field(gt=0)]
"""A count or size that must be at least one.

Carries the bool-is-an-int concern for free: strict mode rejects ``True``
for an ``int`` field, which is what the hand-rolled
``isinstance(value, bool)`` guards did by hand."""


#: JSON Schema keywords that DESCRIBE a property rather than constrain
#: it. When a property gains a null arm they belong on the outside, where
#: an editor reads them, not buried in one branch. The split is JSON
#: Schema 2020-12's own (its "meta-data" vocabulary), and the result is
#: byte for byte what pydantic emits for a natively optional field.
_ANNOTATION_KEYWORDS: Final = frozenset(
    {"title", "description", "default", "deprecated", "readOnly", "writeOnly", "examples"}
)


def _nullable(prop: JsonSchemaValue) -> JsonSchemaValue:
    """``prop`` widened to accept ``null``, or unchanged if it already
    does."""
    if _accepts_null(prop):
        return prop
    outer = {key: value for key, value in prop.items() if key in _ANNOTATION_KEYWORDS}
    inner = {key: value for key, value in prop.items() if key not in _ANNOTATION_KEYWORDS}
    return {"anyOf": [inner, {"type": "null"}], **outer}


def _ref_at_top_level(prop: JsonSchemaValue) -> JsonSchemaValue:
    """``prop`` with its ``x-agw-ref`` on the property itself.

    A marker says what THE FIELD means, so the property is the one place
    a reader should have to look for it. Pydantic puts it wherever the
    ``Annotated`` sat, which for the two shapes a marked optional takes
    (``Annotated[str, SecretRef(...)] | None`` natively, and a required
    templated field after :func:`_nullable` widens it) is inside one
    ``anyOf`` branch. Buried there an editor hover shows nothing, and
    every consumer has to search a subtree to answer "does this field
    name a Resource?", which is a question the property should answer.

    A COLLECTION's element marker is deliberately untouched: it describes
    one element rather than the field, so ``items`` is where it belongs
    and where the walkers read it. Only the property's own branches are
    considered here, so an element marker one level down is never lifted
    onto the field that holds it.
    """
    if REF_SCHEMA_KEY in prop:
        return prop
    keyword = next((candidate for candidate in ("anyOf", "oneOf") if candidate in prop), None)
    if keyword is None:
        return prop
    branches: list[JsonSchemaValue] = prop[keyword]
    # The first is the only one, because a field carries ONE marker:
    # ``marker_of`` reads the first on the field's spine, so a second
    # branch could only repeat the same authored fact.
    extension = next((branch[REF_SCHEMA_KEY] for branch in branches if REF_SCHEMA_KEY in branch), None)
    if extension is None:
        return prop
    lifted = [{key: value for key, value in branch.items() if key != REF_SCHEMA_KEY} for branch in branches]
    return {**prop, keyword: lifted, REF_SCHEMA_KEY: extension}


def _accepts_null(prop: JsonSchemaValue) -> bool:
    """Whether ``prop`` already permits ``null``.

    Two shapes, which are the two this layer could be handed twice: a bare
    null type, and a combinator carrying a null branch.
    """
    if prop.get("type") == "null":
        return True
    branches = prop.get("anyOf") or prop.get("oneOf") or ()
    return any(branch.get("type") == "null" for branch in branches)


def _marked_fields(model_cls: type[BaseModel]) -> tuple[tuple[str, RefMarker], ...]:
    """The model's own fields carrying a reference marker on the field
    ITSELF, in declaration order.

    Cached per class, since it is a pure function of the class and the
    schema hook above runs it for every model that emits. The cache is
    weak so a model class defined inside a function (a test, mostly) is
    still collectable.
    """
    cached = _MARKED_FIELD_CACHE.get(model_cls)
    if cached is not None:
        return cached
    marked: list[tuple[str, RefMarker]] = []
    for name, field in model_cls.model_fields.items():
        marker = marker_of(field)
        if marker is not None:
            marked.append((name, marker))
    computed = tuple(marked)
    _MARKED_FIELD_CACHE[model_cls] = computed
    return computed


def reference_marker_error(model_cls: type[BaseModel]) -> str | None:
    """Why one of ``model_cls``'s reference markers cannot mean what it
    says, or ``None`` when every one of them can.

    A marker names ONE Resource, so it belongs on a field that holds one
    name. Put on a field that holds MANY, or on one that opens a block,
    nothing in this layer can honor it, and each of the three consumers
    fails differently and silently:

    - the boundary fill writes the marker's rendered default where a list
      or a table belongs, and validation then rejects the field on a key
      the operator never wrote;
    - emitted schema widens the COLLECTION with a null arm rather than
      its elements, and hangs the marker off the whole field;
    - reference extraction reads the field as a scalar, so it emits an
      edge for the filled-in default and none of the edges the elements
      actually imply.

    Refused here rather than handled at those three sites, because the
    mistake is an author's and has exactly one fix: move the marker onto
    the element type (``list[Annotated[str, ResourceRef(...)]]``, which is
    how every shipped collection spells it) or onto the block's own
    field. Registration conformance runs this over an implementation's
    declared config model, so a plugin author sees it at registration
    rather than an operator seeing its consequences at load.

    A marker the CLASSIFIER cannot place is refused by the same call, and
    for the same reason one sentence up. Extraction reads a field through
    :class:`~agentworks.schema._shape.FieldShape`, which names the field's
    own marker and the marker on one element of a collection, and those
    are the only two positions any walker looks at. A marker written
    anywhere else in the annotation (under an unrecognized origin, or
    under a second level of collection) is read by nothing at all, so the
    field contributes no edge while validating perfectly well. That
    failure is the worst-shaped one this layer has: the dependency graph
    is built from the extracted edges BEFORE validation, so the Resource
    is never gated, never resolved, and never reported, and the operator's
    document looks correct throughout. Refusing at registration is what
    keeps the set of shapes
    :func:`~agentworks.schema._shape._collection_element` recognizes from
    doubling as a set of silent failures: whatever it does not classify is
    loud here instead.

    A marker fact no consumer reads is refused the same way, which today
    means one: a default template on a COLLECTION's element marker. The
    boundary fill (:func:`~agentworks.schema.filled_defaults`) renders a
    template into the field it defaults, and an omitted collection has no
    element to write into, so the authored template would be prose
    nothing executes.

    The rule behind all of the refusals is one invariant, and it is what
    this function enforces: **every model pydantic can select is a model
    some walker reaches, or no reference marker hides inside it.** The
    check is recursive over the models the walkers descend into (both
    kinds of union arm and the single addressable block of an untagged
    scalar-or-block union, at the field and at one element alike), and it
    is CLOSED over the models validation can construct that no walker
    reaches: an arm of a two-model union nothing tags, a block a table
    member shadows out of pre-validation addressability, an arm behind a
    non-string tag, a model under a shape the classifier does not
    recognize. Each of those validates happily while extraction, by sound
    refusal, never walks it, so a marker anywhere inside one is the same
    silent gating bypass one shape over, and the whole model is refused
    unless it is marker-free at every depth. Stated as a subtraction
    (models validation offers minus models the walkers reach,
    marker-free remainder required) rather than as a list of known bad
    shapes, so the next walker gap an author can spell is refused here
    without anyone having to predict it. A model that cannot be built
    reports nothing: it has no fields to judge, and its own check
    (:func:`~agentworks.schema.model_is_complete`) already refuses it.
    """
    return _marker_error(model_cls, ())


#: How a field that cannot carry a marker holds its value, by the
#: :class:`~agentworks.schema._shape.FieldShape` attribute that says so.
_UNMARKABLE_SHAPES: Final = (
    ("collection", "holds many values"),
    ("nested_model", "opens a nested block"),
    ("arms", "selects a discriminated union arm"),
)


def _marker_error(model_cls: type[BaseModel], visiting: tuple[type[BaseModel], ...]) -> str | None:
    """:func:`reference_marker_error` over one model and everything it
    reaches.

    ``visiting`` is the current PATH, so a model reachable from itself
    terminates, exactly as both walkers in this package handle it.
    """
    if model_cls in visiting:
        return None
    fields = model_fields_of(model_cls)
    if fields is None:
        return None
    visiting = (*visiting, model_cls)
    for name, field in fields.items():
        shape = shape_of(field)
        held = next((prose for attribute, prose in _UNMARKABLE_SHAPES if getattr(shape, attribute)), None)
        if shape.marker is not None and held is not None:
            return (
                f"{model_cls.__name__}.{name} carries a reference marker on a field that {held}, "
                f"where nothing can honor it; a marker names one Resource, so move it onto the "
                f"value that names one"
            )
        if _has_unplaceable_marker(field, shape):
            return (
                f"{model_cls.__name__}.{name} carries a reference marker in a position this layer "
                f"cannot classify, so no edge would ever be extracted for it; a marker is read on "
                f"the field's own value or on one element of a list, a set, or a table, so spell "
                f"the field as one of those (a nested collection needs a model for the inner one)"
            )
        if shape.item_marker is not None and shape.item_marker.default_template is not None:
            return (
                f"{model_cls.__name__}.{name} declares a default template on a collection's "
                f"element marker, which nothing renders: an omitted collection has no element to "
                f"default and a present element is named by the operator, so drop the template or "
                f"move the marker onto a scalar field that holds one name"
            )
        reachable = _reachable_models(shape)
        for stranded in models_in(field.annotation):
            if stranded not in reachable and _hides_marker(stranded, ()):
                return (
                    f"{model_cls.__name__}.{name} lets validation select {stranded.__name__}, "
                    f"which no walker reaches, and a reference marker hides inside "
                    f"{stranded.__name__}, so a Resource a document names there would never "
                    f"become a graph edge; make the model addressable from a raw value (a tagged "
                    f"union arm with string tags, the one table-shaped member of its union, or a "
                    f"directly nested block), or remove the marker"
                )
        for model in reachable:
            reason = _marker_error(model, visiting)
            if reason is not None:
                return reason
    return None


def _has_unplaceable_marker(field: FieldInfo, shape: FieldShape) -> bool:
    """Whether ``field`` carries a marker the classifier did not place.

    Compared by IDENTITY, not equality: two fields may legitimately carry
    equal markers (the same kind and usage), and the question here is
    whether THIS marker object is the one the shape reports, not whether
    something like it is.

    One-directional on purpose. A marker pydantic lifted off the
    annotation onto ``field.metadata`` (which is what it does with an
    outermost ``Annotated[str, SecretRef(...)]``) is placed as
    ``shape.marker`` while appearing nowhere in the annotation tree, and
    that is the ordinary case rather than a fault.
    """
    placed = (shape.marker, shape.item_marker)
    return any(all(marker is not found for found in placed) for marker in markers_in(field.annotation))


def _reachable_models(shape: FieldShape) -> tuple[type[BaseModel], ...]:
    """Every model a WALKER can descend into from one field.

    Both kinds of union arm and both untagged scalar-or-block blocks are
    here beside the plain nested block and a collection's element model,
    because reference extraction reaches all six, and a marker this check
    does not visit is one an author ships.

    An untagged union's block counts only when extraction can actually
    address it (:func:`~agentworks.schema._shape.table_addresses_block`,
    the same predicate extraction asks): a block shadowed by a table
    member validates fine and is walked by nothing, which is exactly what
    the stranded-model subtraction in :func:`_marker_error` exists to
    refuse when a marker hides inside one.
    """
    models = (
        shape.nested_model,
        shape.item_model,
        _addressable_block(shape.union_model, shape.union_members),
        _addressable_block(shape.item_union_model, shape.item_union_members),
        *(arm.model for arm in shape.arms),
        *(arm.model for arm in shape.item_arms),
        *structurally_addressable_arms(shape.structural_arms),
        *structurally_addressable_arms(shape.item_structural_arms),
    )
    return tuple(model for model in models if model is not None)


def _addressable_block(model: type[BaseModel] | None, members: tuple[object, ...]) -> type[BaseModel] | None:
    """``model`` when a raw table can be attributed to it, else ``None``."""
    if model is None or not table_addresses_block(model, members):
        return None
    return model


def _hides_marker(model_cls: type[BaseModel], visiting: tuple[type[BaseModel], ...]) -> bool:
    """Whether a reference marker is written anywhere inside ``model_cls``
    or a model reachable from it, in any position, placed or not.

    The question asked of a model VALIDATION can construct and no walker
    reads. Such a model is fine to accept and worthless to mark, since no
    marker inside it can ever feed the graph, so any marker at any depth
    makes the whole model refusable where none at all leaves it alone.
    Both marker carriers are read, because pydantic keeps both: the
    field's own metadata (where an outermost ``Annotated`` marker is
    lifted) and the annotation tree (everywhere else).

    ``visiting`` is this walk's OWN path, and the sole caller deliberately
    starts it at ``()`` rather than handing down the path
    :func:`_marker_error` is carrying. That difference is load-bearing:
    the question here is about one stranded model's contents, not about
    where the outer walk happened to reach it from. Threading the outer
    path in would make this return ``False`` for any stranded model
    already on it, silently skipping the very check the subtraction
    exists to perform. Do not "tidy" the call to match the recursion.

    A model that cannot be built hides nothing this can see; its own
    check (:func:`~agentworks.schema.model_is_complete`) already refuses
    it.
    """
    if model_cls in visiting:
        return False
    fields = model_fields_of(model_cls)
    if fields is None:
        return False
    visiting = (*visiting, model_cls)
    for field in fields.values():
        if marker_of(field) is not None or markers_in(field.annotation):
            return True
        if any(_hides_marker(child, visiting) for child in models_in(field.annotation)):
            return True
    return False
