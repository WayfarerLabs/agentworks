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

from typing import TYPE_CHECKING, Annotated, Any, Final
from weakref import WeakKeyDictionary

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from agentworks.errors import StateError
from agentworks.schema._shape import marker_of, model_fields_of, shape_of
from agentworks.schema.markers import REF_SCHEMA_KEY, RefOwner

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler, ValidationInfo
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

    from agentworks.schema.markers import RefMarker

#: The key an owner rides under in pydantic's validation context. Build
#: the context with :func:`validation_context` rather than spelling it.
OWNER_CONTEXT_KEY: Final = "owner"

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

    @model_validator(mode="before")
    @classmethod
    def _fill_owner_templated_defaults(cls, data: Any, info: ValidationInfo) -> Any:
        """Resolve an omitted reference field from its marker's owner
        template, so the validated instance carries the same name the
        reference extractor derived for the graph.

        The model cannot know its owner by itself, so the owner rides the
        validation context (:func:`validation_context`). Models with no
        templated field ignore the context entirely, which keeps a
        context-free ``model_validate`` legal for them.

        An omitted value and an explicit ``null`` are treated alike, and
        deliberately so: reference extraction makes the same call, and
        the resolved name has to be the same on both paths or the graph
        edge and the validated instance would name different secrets.
        """
        templated = _owner_templated_fields(cls)
        # ``dict``, not ``Mapping``: strict mode accepts a dict or a model
        # instance and nothing else, so widening here would let some other
        # mapping type through for exactly the models that happen to have
        # an unset templated field, which is an invisible local exception
        # to the global posture.
        if not templated or not isinstance(data, dict):
            return data
        unset = [(name, marker) for name, marker in templated if data.get(name) is None]
        if not unset:
            return data

        owner = info.context.get(OWNER_CONTEXT_KEY) if isinstance(info.context, dict) else None
        if not isinstance(owner, RefOwner):
            # A framework bug (a call site forgot the context), never an
            # operator mistake, so this is not a ConfigError.
            names = ", ".join(name for name, _marker in unset)
            raise StateError(
                f"{cls.__name__} has owner-templated field(s) ({names}) but was validated "
                "with no owner in context; pass validation_context(owner) to model_validate"
            )

        filled = dict(data)
        for name, marker in unset:
            rendered = marker.render_default(owner)
            if rendered:
                filled[name] = rendered
        return filled

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Two corrections emitted schema needs and pydantic cannot make.

        **An owner-templated field is neither required nor non-nullable.**
        The before-validator above resolves such a field from its marker,
        and it treats an omitted value and an explicit ``null`` alike, on
        purpose: reference extraction makes the same call, so the two
        paths cannot come to name different secrets. Emitted schema has to
        state BOTH halves of that, and pydantic can state neither, because
        it computes ``required`` and nullability from the declared field
        and knows nothing about the validator:

        - not required, or an editor red-underlines the very omission the
          mechanism exists to resolve (``provider: {name: github}`` with
          no ``token``, which is what every unscoped credential writes);
        - nullable, or it red-underlines ``token: null``, which is that
          same instruction spelled out, and which loads today.

        **Every marked field carries its ``x-agw-ref`` on the property
        itself**, which is the position the marker's whole purpose depends
        on being readable; see :func:`_ref_at_top_level`. That half runs
        for every marked field, templated or not, because the widening
        above is only one of the two ways a marker ends up buried.

        Stated here, on the class that does the filling, rather than in
        the emitter: both rules are properties of this model's validation
        behavior, and a consumer correcting for them downstream would be a
        second place to keep in sync.
        """
        json_schema = handler(core_schema)
        marked = _marked_fields(cls)
        if not marked:
            return json_schema
        templated = {name for name, marker in marked if marker.default_template is not None}
        # The model's schema may arrive as a ``$ref`` into ``$defs``; the
        # required list and the properties live on the definition it
        # points at.
        resolved = handler.resolve_ref_schema(json_schema)
        if templated:
            required = [name for name in resolved.get("required", ()) if name not in templated]
            if required:
                resolved["required"] = required
            else:
                resolved.pop("required", None)
        properties = resolved.get("properties", {})
        for name, _marker in marked:
            # A hidden field (``SkipJsonSchema``) has no property to
            # correct, which is the same absence the field-reference
            # stream honors.
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


def validation_context(owner: RefOwner) -> dict[str, object]:
    """The validation context every framework ``model_validate`` call
    passes: it is what lets an owner-templated field resolve its default.

    A model with no templated field ignores it, so passing it always is
    cheaper than remembering which models need it.
    """
    return {OWNER_CONTEXT_KEY: owner}


def _marked_fields(model_cls: type[BaseModel]) -> tuple[tuple[str, RefMarker], ...]:
    """The model's own fields carrying a reference marker on the field
    ITSELF, in declaration order.

    Cached per class, since it is a pure function of the class and the
    filling above runs on every validation of every model. The cache is
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


def _owner_templated_fields(model_cls: type[BaseModel]) -> tuple[tuple[str, RefMarker], ...]:
    """The marked fields whose marker declares a default template.

    Scalar fields only, and now provably so:
    :func:`reference_marker_error` refuses a marker on a collection or on
    a nested block at registration, so the filling above can write a
    rendered name straight into the field. It was a comment here before
    that, which is a promise no shape had to keep.

    Filtered off :func:`_marked_fields` rather than cached separately: a
    model has at most a handful of marked fields and most have none, so
    the walk this saves is the expensive half.
    """
    return tuple((name, marker) for name, marker in _marked_fields(model_cls) if marker.default_template is not None)


def reference_marker_error(model_cls: type[BaseModel]) -> str | None:
    """Why one of ``model_cls``'s reference markers cannot mean what it
    says, or ``None`` when every one of them can.

    A marker names ONE Resource, so it belongs on a field that holds one
    name. Put on a field that holds MANY, or on one that opens a block,
    nothing in this layer can honor it, and each of the three consumers
    fails differently and silently:

    - validation writes the marker's rendered default where a list or a
      table belongs, then rejects the field on a key the operator never
      wrote;
    - emitted schema widens the COLLECTION with a null arm rather than
      its elements, and hangs the marker off the whole field;
    - reference extraction reads the field as a scalar, so it emits a
      bogus edge for the rendered default and none of the edges the
      elements actually imply.

    Refused here rather than handled at those three sites, because the
    mistake is an author's and has exactly one fix: move the marker onto
    the element type (``list[Annotated[str, ResourceRef(...)]]``, which is
    how every shipped collection spells it) or onto the block's own
    field. Registration conformance runs this over an implementation's
    declared config model, so a plugin author sees it at registration
    rather than an operator seeing its consequences at load.

    Recursive over the same shapes validation reaches, since a nested
    block, a collection's element model, and a union arm each fill and
    extract on their own. A model that cannot be built reports nothing:
    it has no fields to judge, and its own check
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
        for reachable in (shape.nested_model, shape.item_model, *(arm.model for arm in shape.arms)):
            if reachable is not None:
                reason = _marker_error(reachable, visiting)
                if reason is not None:
                    return reason
    return None
