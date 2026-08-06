"""The error bridge: a pydantic ``ValidationError`` to operator text.

A strict, closed-world base model is only half a promise. The other half
is that what an operator SEES when they get a field wrong is at least as
good as the hand-rolled message it replaces, rather than
``Input should be a valid string [type=string_type, input_value=8,
input_type=int]``. This module is that half.

Two entry points over one rendering:

- :func:`render_validation_error` is PURE: it returns the lines and
  raises nothing, so doctor rows, ``describe``, and any other diagnostic
  surface can show the same text without an exception in hand.
- :func:`config_error_from` builds the ``ConfigError`` a caller raises,
  and it OWNS the location framing rather than leaving it to the call
  site. That is not a convenience: pydantic reports every problem in one
  exception, and both of today's framings assume a single-line message
  (decode prefixes line 1 only, so lines 2..N come out unlocated; the
  finalize pass appends the origin, gluing it to the LAST line). Framing
  the batch here is the only shape in which no error line is unlocated.

**Normalization is an explicit table and nothing else.** An error type
the table does not cover falls through to pydantic's own message
verbatim. A fabricated paraphrase of an error we have not thought about
is worse than a slightly clinical correct one, so this module never
invents phrasing for a type it does not know, and never re-phrases a
message whose specifics it cannot read out of the error's context.

Note the name collision, which is a trap for every file that touches
both: ``agentworks.errors.ValidationError`` is a DIFFERENT thing
(invalid user input at the command surface). Pydantic's is imported
here as ``PydanticValidationError`` and this module produces
``ConfigError``, which is what every config-shape error already is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import RootModel

from agentworks.errors import ConfigError
from agentworks.resources.schema._shape import is_model, model_fields_of, shape_of

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import BaseModel
    from pydantic import ValidationError as PydanticValidationError
    from pydantic_core import ErrorDetails

    from agentworks.resources.schema._shape import FieldShape, UnionArmType
    from agentworks.resources.schema.markers import RefOwner
    from agentworks.source_location import SourceLocation

#: The most error lines a raised ``ConfigError`` body carries. The header
#: always states the TRUE count, so a capped batch never hides how bad
#: the document is. The pure renderer is uncapped: its caller is a
#: diagnostic surface that can decide for itself.
MAX_ERROR_LINES: Final = 10

#: Pydantic's error ``type`` to the operator-facing phrasing, chosen to
#: match what the hand-rolled validators this replaces already say. An
#: error type absent from here keeps pydantic's own message; see the
#: module docstring for why that is the honest default.
_TYPE_MESSAGES: Final[Mapping[str, str]] = {
    "missing": "is required",
    "string_type": "must be a string",
    "int_type": "must be an integer",
    "bool_type": "must be a boolean",
    "float_type": "must be a number",
    "list_type": "must be a list",
    "dict_type": "must be a table",
    "model_type": "must be a table",
    # What a discriminated-union field reports for a non-table value
    # (``platform: lima`` written as a bare scalar), where pydantic says
    # "a valid dictionary or object to extract fields from".
    "model_attributes_type": "must be a table",
}


class FramedConfigError(ConfigError):
    """A ``ConfigError`` that already carries its own location framing.

    :func:`config_error_from` produces only this type, and a caller must
    not re-frame it. The one caller that would is the finalize validate
    pass, which appends an origin suffix to whatever a resource's
    ``validate`` raises (``resources/registry.py``): correct for the
    hand-rolled validators that still raise unframed ``ConfigError``s,
    and a double framing for anything from here.

    Marking the ERROR rather than each call site is what makes the rule
    hold through the four consuming resources, capability construction,
    and the migrator without each of them knowing about a wrapper three
    layers away. This class and that wrapper are one bounded fork with
    one deletion trigger: they both go when the last hand-rolled
    validator does (step 2.5), leaving one framing for everything.
    """


def render_validation_error(
    exc: PydanticValidationError,
    *,
    model_cls: type[BaseModel],
    owner: RefOwner,
) -> list[str]:
    """One owner-framed line per problem in ``exc``, normalized.

    Each line is ``<kind>/<name>.<field.path>: <message>``, or
    ``<kind>/<name>: <message>`` for a whole-document problem (a root
    model, whose errors carry no path).

    Pure: no raising, no I/O, no framing. This is the diagnostic form;
    :func:`config_error_from` is the one that builds an exception.
    """
    return [_owner_framed(owner, problem) for problem in _problems(exc, model_cls)]


def config_error_from(
    exc: PydanticValidationError,
    *,
    model_cls: type[BaseModel],
    owner: RefOwner,
    location: SourceLocation | None = None,
    hint: str | None = None,
) -> FramedConfigError:
    """The ``ConfigError`` a caller raises for ``exc``, framed by
    ``location``.

    One problem renders as a single line in decode's shipped shape
    (``<file>:<line>: <owner>.<path>: <message>``), so the common case
    looks exactly like what operators see today. Several render as a
    located header naming the true count, then one indented line each:

    .. code-block:: text

        sites.yaml:12: vm-site/lab: 3 problems
          platform.vm_host: must be a string
          platform.cpus: is required
          platform.regions: unknown field; expected one of: cpus, vm_host

    Because this owns the framing, a caller must NOT also wrap the
    result with a location of its own (the finalize pass's origin suffix,
    ``resources/registry.py``): that would frame it twice.
    """
    problems = _problems(exc, model_cls)
    return FramedConfigError(
        _framed_batch(problems, owner, location),
        entity_kind=owner.kind,
        entity_name=owner.name,
        hint=hint,
    )


@dataclass(frozen=True)
class _Problem:
    """One rendered validation error, before it is framed.

    ``path`` is the operator's dotted address for the offending value,
    empty for a whole-document problem.
    """

    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


def _problems(exc: PydanticValidationError, model_cls: type[BaseModel]) -> list[_Problem]:
    return [_problem(model_cls, detail) for detail in exc.errors(include_url=False)]


def _problem(model_cls: type[BaseModel], detail: ErrorDetails) -> _Problem:
    path, container = _resolve_path(model_cls, detail["loc"])
    return _Problem(path=path, message=_message(detail, container))


def _owner_framed(owner: RefOwner, problem: _Problem) -> str:
    return f"{owner.display}.{problem.render()}" if problem.path else f"{owner.display}: {problem.message}"


def _framed_batch(
    problems: Sequence[_Problem],
    owner: RefOwner,
    location: SourceLocation | None,
) -> str:
    if len(problems) == 1:
        return _located(location, _owner_framed(owner, problems[0]))
    lines = [_located(location, f"{owner.display}: {len(problems)} problems")]
    lines.extend(f"  {problem.render()}" for problem in problems[:MAX_ERROR_LINES])
    if len(problems) > MAX_ERROR_LINES:
        lines.append(f"  ... and {len(problems) - MAX_ERROR_LINES} more")
    return "\n".join(lines)


def _located(location: SourceLocation | None, text: str) -> str:
    return f"{location.file}:{location.line}: {text}" if location is not None else text


# -- Message normalization ----------------------------------------------------


def _message(detail: ErrorDetails, container: type[BaseModel] | None) -> str:
    """The operator-facing phrasing for one error.

    The contextual cases come first because they read values out of
    pydantic's ``ctx``; everything else is the flat table, and anything
    in neither keeps pydantic's own message.
    """
    contextual = _contextual_message(detail, container)
    if contextual is not None:
        return contextual
    return _TYPE_MESSAGES.get(detail["type"], detail["msg"])


def _contextual_message(detail: ErrorDetails, container: type[BaseModel] | None) -> str | None:
    """The phrasing for the error types whose text depends on the model
    or on the error's context, or ``None`` to leave it to the table.

    Each of these returns ``None`` when the fact it needs is not where it
    should be, which puts the message back on pydantic's own text rather
    than on a guess.
    """
    ctx: Mapping[str, object] = detail.get("ctx") or {}
    match detail["type"]:
        case "extra_forbidden":
            return _unknown_field(container)
        case "string_too_short":
            # Only the min-length-1 case means "must not be empty".
            # Saying that for ``min_length=3`` would be a paraphrase that
            # is simply false, so pydantic's exact wording wins there.
            return "must not be empty" if ctx.get("min_length") == 1 else None
        case "literal_error":
            expected = ctx.get("expected")
            # Pydantic pre-formats the alternatives ("'a', 'b' or 'c'"),
            # so they are quoted here rather than re-derived from the
            # annotation, which would be a second enumeration to keep in
            # sync with the first.
            return f"must be one of: {expected}" if isinstance(expected, str) else None
        case "union_tag_not_found":
            tag_field = _unquoted(ctx.get("discriminator"))
            return f"{tag_field} is required" if tag_field else None
        case "union_tag_invalid":
            return _unknown_tag(ctx)
        case "value_error" | "assertion_error":
            # A model validator's own exception. Pydantic renders it as
            # "Value error, <message>"; the prefix is its presentation and
            # the message is the author's, exactly like the pre-quoted
            # discriminator unquoted above. Reading the message off the
            # context rather than stripping a prefix off the text keeps
            # this from depending on that rendering.
            error = ctx.get("error")
            return str(error) if isinstance(error, Exception) else None
        case _:
            return None


def _unknown_field(container: type[BaseModel] | None) -> str:
    """The unknown-key message, naming the valid fields.

    Naming them is the whole reason ``model_cls`` is a parameter of this
    module: it is where we beat pydantic's default text, and it matches
    what today's hand-rolled validators say ("unknown azure-vm platform
    field(s): ..."). Sorted, as every other unknown-key message in the
    codebase lists its alternatives.
    """
    fields = model_fields_of(container) if container is not None else None
    if not fields:
        return "unknown field"
    return "unknown field; expected one of: " + ", ".join(sorted(fields))


def _unknown_tag(ctx: Mapping[str, object]) -> str | None:
    """The bad-capability-name message: the operator named an arm that
    the union does not have."""
    tag_field = _unquoted(ctx.get("discriminator"))
    tag = ctx.get("tag")
    expected = ctx.get("expected_tags")
    if not tag_field or not isinstance(tag, str) or not isinstance(expected, str):
        return None
    return f"unknown {tag_field} {tag!r}; registered: {expected}"


def _unquoted(value: object) -> str | None:
    """Pydantic renders a discriminator field name already quoted
    (``"'name'"``); the quotes are its presentation, not the name."""
    if not isinstance(value, str):
        return None
    return value[1:-1] if len(value) >= 2 and value.startswith("'") and value.endswith("'") else value


# -- ``loc`` to the path the operator wrote -----------------------------------


@dataclass(frozen=True)
class _AtModel:
    """The next ``loc`` segment names a field of this model, or is an
    unknown key in it."""

    model: type[BaseModel]


@dataclass(frozen=True)
class _AtTag:
    """The next ``loc`` segment is a discriminated union's arm tag.

    Pydantic inserts the selected arm's tag as a path segment, but the
    operator wrote no such key: for ``platform: {name: lima, vm_host: 8}``
    the loc is ``('platform', 'lima', 'vm_host')`` and the address the
    operator can act on is ``platform.vm_host``. Knowing the arms is what
    lets the tag be dropped only when it really is one.
    """

    arms: tuple[UnionArmType, ...]


@dataclass(frozen=True)
class _AtUnion:
    """The next ``loc`` segment names one MEMBER of an undiscriminated
    union.

    Pydantic tries every member and reports an error per failure, each
    prefixed with that member's own name: for a mapping declared
    ``str | AccountRef``, a bad value reports ``('str',)`` and
    ``('AccountRef', ...)``. The operator wrote neither segment, and the
    second is an internal class name, so both are dropped, and the walk
    continues INSIDE the named member when it is a model, which is what
    keeps an unknown-key message able to list the valid fields.
    """

    members: tuple[object, ...]


@dataclass(frozen=True)
class _AtElement:
    """The next ``loc`` segment addresses one element of a collection: a
    list index or a table key, both of which the operator DID write."""

    item_model: type[BaseModel] | None


#: Where a ``loc`` walk currently stands. ``None`` is "lost track", which
#: is not a failure: the remaining segments render verbatim, which is
#: pydantic's own rendering and is never wrong, only less polished.
_Cursor = _AtModel | _AtTag | _AtUnion | _AtElement | None


def _resolve_path(
    model_cls: type[BaseModel],
    loc: tuple[int | str, ...],
) -> tuple[str, type[BaseModel] | None]:
    """``loc`` as the dotted address the operator wrote, plus the model
    that CONTAINS its last segment (which is what an unknown-key message
    lists the valid fields of).

    Walking the model alongside the loc is what makes the two adjustments
    honest: a union arm tag is dropped because the model says it is one,
    and an index becomes ``[i]`` on the field that holds the collection.
    """
    parts: list[str] = []
    cursor: _Cursor = _initial_cursor(model_cls)
    container: type[BaseModel] | None = None
    for segment in loc:
        container = cursor.model if isinstance(cursor, _AtModel) else None
        keep, cursor = _advance(cursor, segment)
        if not keep:
            continue
        if isinstance(segment, int):
            _append_index(parts, segment)
        else:
            parts.append(segment)
    return ".".join(parts), container


def _initial_cursor(model_cls: type[BaseModel]) -> _Cursor:
    """Where the walk starts.

    A root model's value IS the document, so pydantic's loc never
    mentions its one ``root`` field: the walk starts inside whatever the
    root wraps, or nowhere at all when the root is a bare scalar (whose
    errors carry an empty loc anyway).
    """
    if not issubclass(model_cls, RootModel):
        return _AtModel(model_cls)
    fields = model_fields_of(model_cls)
    root = fields.get("root") if fields is not None else None
    return _cursor_for(shape_of(root)) if root is not None else None


def _advance(cursor: _Cursor, segment: int | str) -> tuple[bool, _Cursor]:
    """Consume one ``loc`` segment: whether it belongs in the rendered
    path, and where the walk stands after it."""
    if isinstance(cursor, _AtTag) and isinstance(segment, str):
        for arm in cursor.arms:
            if arm.tag == segment:
                return False, _AtModel(arm.model)
        return True, None
    if isinstance(cursor, _AtUnion) and isinstance(segment, str):
        # Dropped by POSITION, not by matching a spelling: pydantic labels
        # each member in its own vocabulary ("str", "constrained-str", a
        # class name), and a recognizer for those spellings would be a
        # second thing to keep in sync with pydantic. What is reliable is
        # that the segment immediately after an undiscriminated union IS
        # the member label, which the operator never wrote either way.
        # The walk continues inside a member it can name, which is what
        # keeps that member's field list available.
        member = next((m for m in cursor.members if getattr(m, "__name__", None) == segment), None)
        return False, _AtModel(member) if is_model(member) else None
    if isinstance(cursor, _AtElement):
        return True, None if cursor.item_model is None else _AtModel(cursor.item_model)
    if isinstance(cursor, _AtModel) and isinstance(segment, str):
        fields = model_fields_of(cursor.model)
        field = fields.get(segment) if fields is not None else None
        return True, None if field is None else _cursor_for(shape_of(field))
    return True, None


def _cursor_for(shape: FieldShape) -> _Cursor:
    """Where a walk stands once it has entered a field of this shape."""
    if shape.nested_model is not None:
        return _AtModel(shape.nested_model)
    if shape.collection is not None:
        return _AtElement(shape.item_model)
    if shape.arms:
        return _AtTag(shape.arms)
    if shape.union_members:
        return _AtUnion(shape.union_members)
    return None


def _append_index(parts: list[str], index: int) -> None:
    """An index belongs to the segment before it (``vm_sizes[1]``), not
    beside it."""
    if parts:
        parts[-1] += f"[{index}]"
    else:
        parts.append(f"[{index}]")
