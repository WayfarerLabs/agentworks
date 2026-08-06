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

from typing import Final

from pydantic import BaseModel, ConfigDict, RootModel

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


class AgwRootModel[T](RootModel[T]):
    """Base for a modeled surface whose value is NOT a mapping.

    A secret backend's mapping is the shipped example: env-var's is a
    bare string and onepassword's is a string or a table, neither of
    which a ``BaseModel`` can be. Same settings as :class:`AgwModel`
    minus ``extra``, which ``RootModel`` refuses; see the comment on
    ``_AGW_ROOT_MODEL_CONFIG``.
    """

    model_config = _AGW_ROOT_MODEL_CONFIG
