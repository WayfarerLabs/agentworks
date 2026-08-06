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
from agentworks.resources.schema._shape import marker_of
from agentworks.resources.schema.markers import RefOwner

if TYPE_CHECKING:
    from pydantic import ValidationInfo

    from agentworks.resources.schema.markers import RefMarker

#: The key an owner rides under in pydantic's validation context. Build
#: the context with :func:`validation_context` rather than spelling it.
OWNER_CONTEXT_KEY: Final = "owner"

#: Per-class cache for :func:`_owner_templated_fields`.
_TEMPLATED_FIELD_CACHE: Final[WeakKeyDictionary[type[BaseModel], tuple[tuple[str, RefMarker], ...]]] = (
    WeakKeyDictionary()
)

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


def validation_context(owner: RefOwner) -> dict[str, object]:
    """The validation context every framework ``model_validate`` call
    passes: it is what lets an owner-templated field resolve its default.

    A model with no templated field ignores it, so passing it always is
    cheaper than remembering which models need it.
    """
    return {OWNER_CONTEXT_KEY: owner}


def _owner_templated_fields(model_cls: type[BaseModel]) -> tuple[tuple[str, RefMarker], ...]:
    """The model's own fields whose marker declares a default template,
    in declaration order.

    Scalar fields only: a marked list has no single default identity, and
    a nested model fills its own fields when it is validated.

    Cached per class, since it is a pure function of the class and this
    runs on every validation of every model. The cache is weak so a
    model class defined inside a function (a test, mostly) is still
    collectable.
    """
    cached = _TEMPLATED_FIELD_CACHE.get(model_cls)
    if cached is not None:
        return cached
    templated: list[tuple[str, RefMarker]] = []
    for name, field in model_cls.model_fields.items():
        marker = marker_of(field)
        if marker is not None and marker.default_template is not None:
            templated.append((name, marker))
    computed = tuple(templated)
    _TEMPLATED_FIELD_CACHE[model_cls] = computed
    return computed
