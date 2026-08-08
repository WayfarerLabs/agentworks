"""The error bridge: a pydantic ``ValidationError`` to operator text.

A strict, closed-world base model is only half a promise. The other half
is that what an operator SEES when they get a field wrong is at least as
good as the hand-rolled message it replaces, rather than
``Input should be a valid string [type=string_type, input_value=8,
input_type=int]``. This module is that half.

:func:`config_error_from` is the one entry point. It builds the
``ConfigError`` a caller raises, and it OWNS the location framing rather
than leaving it to the call site. That is not a convenience: pydantic
reports every problem in one exception, and both of today's framings
assume a single-line message (decode prefixes line 1 only, so lines 2..N
come out unlocated; the finalize pass appends the origin, gluing it to
the LAST line). Framing the batch here is the only shape in which no
error line is unlocated.

A diagnostic surface wanting the same text WITHOUT an exception in hand
(a doctor row, ``describe``) reads :func:`_problems`, the shared core the
framing is built over, and frames the problems its own way. That is why
the core is factored out under a name of its own despite having one
caller today. A pure public wrapper over it shipped for a while and was
retired never having gained one: the reusable-text property lives in
``_problems``, so a wrapper is four lines whenever a real consumer turns
up.

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
from functools import cache
from typing import TYPE_CHECKING, Final

from pydantic import RootModel

from agentworks.errors import ConfigError
from agentworks.schema._shape import Collection, is_hidden, is_model, model_fields_of, shape_of
from agentworks.source_location import SYNTHESIZED_PATH, format_file_path

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import BaseModel
    from pydantic import ValidationError as PydanticValidationError
    from pydantic_core import ErrorDetails

    from agentworks.schema._shape import FieldShape, UnionArmType
    from agentworks.schema.markers import RefOwner
    from agentworks.source_location import SourceLocation

#: The most error lines a raised ``ConfigError`` body carries. The header
#: always states the TRUE count, so a capped batch never hides how bad
#: the document is. The cap is presentation and nothing else:
#: :func:`_problems` yields one problem per mistake however many there
#: are, so a diagnostic surface built on it sets its own limit.
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

#: The same shapes as :data:`_TYPE_MESSAGES`, phrased as ONE ALTERNATIVE
#: in a list ("must be a string, a table, or false"). Only the type
#: errors appear: an alternative is a SHAPE the value could have taken,
#: and an error type that reports something else about a value (a length
#: floor, a bound) does not name a shape. A union whose members are not
#: all in here is left uncollapsed rather than described with a phrase
#: this module would have to invent; see :func:`_collapsed`.
_ALTERNATIVE_PHRASES: Final[Mapping[str, str]] = {
    "string_type": "a string",
    "int_type": "an integer",
    "bool_type": "a boolean",
    "float_type": "a number",
    "list_type": "a list",
    "dict_type": "a table",
    "model_type": "a table",
    "model_attributes_type": "a table",
}

#: Pydantic's marker for a failure in a mapping KEY rather than in its
#: value: ``('env', '1BAD', '[key]')``. The key itself is already the
#: preceding segment, so the marker is a segment the operator never
#: wrote. Dropped by :func:`_resolve_path`, and only where one can
#: legitimately occur, so a table whose key really is spelled ``[key]``
#: still renders that key.
_KEY_MARKER: Final = "[key]"


def config_error_from(
    exc: PydanticValidationError,
    *,
    model_cls: type[BaseModel],
    owner: RefOwner,
    location: SourceLocation | None = None,
    hint: str | None = None,
    provenance: Mapping[str, RefOwner] | None = None,
) -> ConfigError:
    """The ``ConfigError`` a caller raises for ``exc``, framed by
    ``location``.

    One problem renders as a single line in decode's shipped shape
    (``<file>:<line>: <owner>.<path>: <message>``), so the common case
    looks exactly like what operators see today. Several render as a
    located header naming the true count, then one indented line each:

    .. code-block:: text

        sites.yaml:12: vm-site/lab: 3 problems
          platform.placement: must be a table
          platform.cpus: is required
          platform.regions: unknown field; expected one of: cpus, placement

    Because this owns the framing, a caller must NOT also wrap the
    result with a location of its own: that would frame it twice. There is
    no marker type saying so any more, and none is needed: every error a
    resource's ``validate_config`` raises comes from here, so the finalize
    pass has one framing to leave alone rather than two to tell apart.

    ``provenance`` maps a top-level key of the validated blob to the owner
    that DECLARED it, for a blob assembled by merging an inheritance chain.
    A problem on a key some other owner declared renders with an
    ``(inherited from <owner>)`` tail, so the operator is sent to the file
    that has the mistake in it rather than to the child that inherited it.
    Omitted for every unmerged surface, where the owner already is the
    declarer.
    """
    problems = _problems(exc, model_cls, owner, provenance)
    return ConfigError(
        _framed_batch(problems, owner, location),
        entity_kind=owner.kind,
        entity_name=owner.name,
        hint=hint,
    )


@dataclass(frozen=True)
class _Problem:
    """One rendered validation error, before it is framed.

    ``path`` is the operator's dotted address for the offending value,
    empty for a whole-document problem. ``inherited_from`` is the display
    of the owner that declared the value, when that is not the owner being
    validated.
    """

    path: str
    message: str
    inherited_from: str | None = None
    alternatives: tuple[str, ...] = ()
    """The shapes this problem says the value could have taken, when it
    is one member's report of an undiscriminated union's failure.
    Pydantic reports such a failure as an error per member, all at the
    same address, and :func:`_collapsed` folds the run back into the
    single problem the operator actually has.

    Non-empty means the member rejected the value on its SHAPE ALONE and
    got no further, which is what makes the phrase an alternative in the
    first place. Empty covers both a problem that is not a union
    member's, and a member that ACCEPTED the shape and failed on the
    value's content: :func:`_collapsed` reads the distinction that way."""

    union_path: str | None = None
    """The address of the OUTERMOST undiscriminated union this problem
    arose at or inside, or ``None`` when no union is involved.

    A member's shape rejection and a failure deep inside a sibling
    member are one union's report at two different addresses. This is
    what lets :func:`_collapsed` see them as one group, which
    :attr:`path` cannot: there, the two differ by construction.

    Outermost rather than innermost because the grouping question is
    "which single mistake is this a report of", and for a union nested
    inside another member's field, that is the outer one. Keying on the
    inner union would leave the outer union's own arm noise in a group
    by itself, with nothing in it to reveal the arm noise as noise."""

    def render(self) -> str:
        text = f"{self.path}: {self.message}" if self.path else self.message
        return f"{text} (inherited from {self.inherited_from})" if self.inherited_from else text


def _problems(
    exc: PydanticValidationError,
    model_cls: type[BaseModel],
    owner: RefOwner,
    provenance: Mapping[str, RefOwner] | None = None,
) -> list[_Problem]:
    return _collapsed([_problem(model_cls, detail, owner, provenance) for detail in exc.errors(include_url=False)])


def _problem(
    model_cls: type[BaseModel],
    detail: ErrorDetails,
    owner: RefOwner,
    provenance: Mapping[str, RefOwner] | None,
) -> _Problem:
    address = _resolve_path(model_cls, detail["loc"])
    return _Problem(
        path=address.path,
        message=_message(detail, address.container),
        inherited_from=_inherited_from(address.path, owner, provenance),
        alternatives=_alternatives(detail) if address.at_union else (),
        union_path=address.union_path,
    )


def _collapsed(problems: list[_Problem]) -> list[_Problem]:
    """One union's failure as the ONE problem the operator has.

    Pydantic tries every member of an undiscriminated union and reports a
    failure per member, so the operator's single mistake arrives as
    several problems. Which of them are worth showing depends on how far
    the members got, and there are exactly two cases.

    **No member accepted the value's shape.** Every report is a shape
    rejection, and none of them is more right than the others, so they
    fold into one line naming the alternatives: ``backend_mappings.b: 3``
    arrives as three problems and reads as "must be a string, a table, or
    False".

    **A member accepted the shape and failed on the content.** That
    member is the arm the operator meant, and its report is the only one
    worth reading. The other members' shape rejections are noise, and
    loud noise: a ``{account}`` table missing its ``reference`` led with
    "must be a string" (the ``op://`` arm's rejection) before saying
    anything true, and a malformed ``op://`` string trailed with "must be
    a table". Those lines are dropped here, which is what
    :attr:`_Problem.union_path` exists to make possible: the surviving
    report sits at a DEEPER address than the rejections, so ``path``
    alone cannot tell that they are the same union's.

    A shape rejection is exactly a problem carrying
    :attr:`_Problem.alternatives`, so "some member got past the shape" is
    read off the batch rather than re-derived: see that attribute.

    One narrowness is deliberate. A collapse only ever merges problems at
    ONE address, so a union nested inside another union's member keeps
    its own line rather than having its alternatives merged into the
    outer union's. Its arm noise is still dropped when an outer member
    got further, which is the case that matters.
    """
    entered = {
        problem.union_path for problem in problems if problem.union_path is not None and not problem.alternatives
    }
    answered = [problem for problem in problems if not problem.alternatives or problem.union_path not in entered]
    return _merged_alternatives(answered)


def _merged_alternatives(problems: list[_Problem]) -> list[_Problem]:
    """A run of shape rejections at one address as a single line naming
    every shape the value could have taken."""
    collapsed: list[_Problem] = []
    for problem in problems:
        previous = collapsed[-1] if collapsed else None
        if previous is None or not problem.alternatives or not previous.alternatives or previous.path != problem.path:
            collapsed.append(problem)
            continue
        # Deduplicated, because a two-arm union of models reports "a
        # table" once per arm.
        merged = previous.alternatives + tuple(
            item for item in problem.alternatives if item not in previous.alternatives
        )
        collapsed[-1] = _Problem(
            path=previous.path,
            message="must be " + _one_of(merged),
            inherited_from=previous.inherited_from,
            alternatives=merged,
            union_path=previous.union_path,
        )
    return collapsed


def _alternatives(detail: ErrorDetails) -> tuple[str, ...]:
    """The shape this error says the value could have taken, as one item
    of an alternatives list, or nothing when it names no shape."""
    if detail["type"] == "literal_error":
        # Pydantic pre-formats the values ("'a' or 'b'"), which reads as
        # one alternative of the outer list without further work.
        expected = (detail.get("ctx") or {}).get("expected")
        return (expected,) if isinstance(expected, str) else ()
    phrase = _ALTERNATIVE_PHRASES.get(detail["type"])
    return (phrase,) if phrase is not None else ()


def _one_of(items: Sequence[str]) -> str:
    """``items`` as prose: "a", "a or b", "a, b, or c"."""
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"


def _inherited_from(path: str, owner: RefOwner, provenance: Mapping[str, RefOwner] | None) -> str | None:
    """The owner that declared the value at ``path``, when that is someone
    other than ``owner``; ``None`` otherwise.

    ``None`` covers the three uninteresting cases together: the blob was
    not merged (no provenance at all), the key has no recorded declarer
    (a ``missing`` error, where nobody wrote the key by construction), or
    the declarer is the owner already named at the head of the line, where
    a tail would only repeat it.

    Provenance is per TOP-LEVEL key, because that is the granularity a
    merge has: a capability's merge combines whole values, so ``command``
    has a declarer and ``sandbox.writable_dirs`` inherits its parent key's.
    """
    if not path or not provenance:
        return None
    key = path.split(".", 1)[0].split("[", 1)[0]
    declarer = provenance.get(key)
    return None if declarer is None or declarer == owner else declarer.display


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
    """``text`` prefixed with where the operator can go and fix it.

    ``SourceLocation`` carries its own sentinels and they are honored
    rather than rendered: ``line == 0`` means the resource was not
    introduced by a specific declaration site, and the synthesized path
    means there is no file either (a framework-constructed row). A
    location an operator cannot navigate to is worse than no location, so
    those frame nothing.

    The path renders home-relative, matching every other operator-facing
    rendering of a config path.
    """
    if location is None or location.file == SYNTHESIZED_PATH:
        return text
    where = format_file_path(location.file)
    if location.line:
        where = f"{where}:{location.line}"
    return f"{where}: {text}"


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
        case "string_pattern_mismatch":
            # A constrained STRING shape. Rendered from the pattern rather
            # than paraphrased, because only the field knows what its
            # pattern MEANS and this module never invents phrasing: what
            # it can do is spell the rule plainly instead of pydantic's
            # "String should match pattern '...'". The intent belongs in
            # the field's description, which the sample and describe
            # surfaces render beside it.
            #
            # Backticks rather than the ``/.../`` a regex usually wears:
            # one shipped pattern (a github ``repos`` entry) contains a
            # slash, so slash delimiters would leave an operator unable to
            # see where the rule ends.
            pattern = ctx.get("pattern")
            return f"must match `{pattern}`" if isinstance(pattern, str) else None
        case "literal_error":
            expected = ctx.get("expected")
            # Pydantic pre-formats the alternatives ("'a', 'b' or 'c'"),
            # so they are quoted here rather than re-derived from the
            # annotation, which would be a second enumeration to keep in
            # sync with the first.
            return f"must be one of: {expected}" if isinstance(expected, str) else None
        case "bool_type":
            return _quoted_boolean(detail.get("input"))
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


@cache
def _loader_boolean_spellings() -> frozenset[str]:
    """Every plain scalar the manifest loader reads as a boolean.

    Derived from pyyaml's own table through a real load rather than
    listed, the way ``manifests.emit.YAML_11_ONLY_BOOLEANS`` is derived in
    its own tests. The question this answers is what the LOADER would have
    done with the same text unquoted, so the loader's parser is the only
    honest source for it.

    Imported inside the function: this package is the model layer and
    nothing else in it depends on the serialization format. The dependency
    is real but narrow, and it belongs to this one message.
    """
    import yaml

    words = yaml.constructor.SafeConstructor.bool_values
    casings = {casing for word in words for casing in (word.lower(), word.upper(), word.capitalize())}
    return frozenset(text for text in casings if isinstance(yaml.safe_load(f"a: {text}")["a"], bool))


def _quoted_boolean(value: object) -> str | None:
    """ "must be a boolean", naming the quotes when they are the cause.

    This message is now the ONLY signal an operator gets for a quoted
    ``"no"``. The emitted schema deliberately accepts it: under YAML 1.2 a
    bare ``no`` and a quoted ``"no"`` are the same parsed string, so the
    widening that stopped editors red-underlining the valid bare form also
    made them silent on the invalid quoted one (emission LLD, section
    2.3). The editor having nothing to say is what puts the whole weight
    here.

    Naming the cause rather than the shape, because "must be a boolean"
    reads as a contradiction to someone looking at a line that says
    ``no``: the value is right and the quotes are what is wrong. The
    field docstrings say the same thing in advance; this says it at the
    moment it happens.

    Only for a string the loader would have read as a boolean. Anything
    else (``verify_ssl: 5``) goes back to the flat table, because the
    quotes are not the story there.
    """
    if isinstance(value, str) and value in _loader_boolean_spellings():
        return f"must be a boolean, and '{value}' is quoted, which makes it a string; write it unquoted"
    return None


def _unknown_field(container: type[BaseModel] | None) -> str:
    """The unknown-key message, naming the valid fields.

    Naming them is the whole reason ``model_cls`` is a parameter of this
    module: it is where we beat pydantic's default text, and it matches
    what today's hand-rolled validators say ("unknown azure-vm platform
    field(s): ..."). Sorted, as every other unknown-key message in the
    codebase lists its alternatives.

    A hidden field is not offered, because it is not a field the operator
    may write HERE: a declared row carries its envelope metadata and its
    provenance beside its spec fields, and listing those would answer an
    operator's mistake with a worse one.
    """
    fields = model_fields_of(container) if container is not None else None
    if not fields:
        return "unknown field"
    offered = sorted(name for name, field in fields.items() if not is_hidden(field))
    if not offered:
        return "unknown field"
    return "unknown field; expected one of: " + ", ".join(offered)


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
    operator wrote no such key: for ``platform: {name: lima, placement: 8}``
    the loc is ``('platform', 'lima', 'placement')`` and the address the
    operator can act on is ``platform.placement``. Knowing the arms is
    what lets the tag be dropped only when it really is one.

    NESTED unions get the same treatment by the same rule, which is what
    keeps a bad key inside lima's ``placement`` reading as
    ``platform.placement.host`` rather than growing a ``ssh`` segment in
    the middle.
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
    item_union_members: tuple[object, ...] = ()
    """What one element holds when it is an undiscriminated union, so the
    walk can step into :class:`_AtUnion` rather than losing track and
    rendering pydantic's member labels (``backend_mappings.b.str``)."""

    item_arms: tuple[UnionArmType, ...] = ()
    """What one element may BE when it is a discriminated union, so the
    walk can step into :class:`_AtTag` and drop the tag pydantic inserts,
    exactly as it does for a tagged union written directly on a field."""

    mapping: bool = False
    """Whether the elements are addressed by an operator-written KEY.
    Only a mapping can produce pydantic's ``[key]`` marker, so this is
    what lets the marker be dropped without also dropping a table key
    that is genuinely spelled that way."""


#: Where a ``loc`` walk currently stands. ``None`` is "lost track", which
#: is not a failure: the remaining segments render verbatim, which is
#: pydantic's own rendering and is never wrong, only less polished.
_Cursor = _AtModel | _AtTag | _AtUnion | _AtElement | None


@dataclass(frozen=True)
class _Address:
    """Where one error happened, as the operator can address it."""

    path: str
    """The dotted address the operator wrote, empty for a whole-document
    problem."""

    container: type[BaseModel] | None
    """The model that CONTAINS the last segment, which is what an
    unknown-key message lists the valid fields of."""

    at_union: bool
    """Whether the walk ended ON an undiscriminated union rather than
    inside one of its members: the mark that this error is one member's
    report of a single failure the operator sees once."""

    union_path: str | None
    """The address of the OUTERMOST undiscriminated union the walk passed
    through, whether it ended there or carried on into a member. ``None``
    when it passed through none. See :attr:`_Problem.union_path`."""


def _resolve_path(model_cls: type[BaseModel], loc: tuple[int | str, ...]) -> _Address:
    """``loc`` as the address the operator can act on.

    Walking the model alongside the loc is what makes every adjustment
    honest: a union arm tag is dropped because the model says it is one,
    a key marker is dropped only where a mapping key can produce one, and
    an index becomes ``[i]`` on the field that holds the collection.
    """
    parts: list[str] = []
    cursor: _Cursor = _initial_cursor(model_cls)
    container: type[BaseModel] | None = None
    at_union = False
    union_path: str | None = None
    after_mapping_key = False
    last = len(loc) - 1
    for index, segment in enumerate(loc):
        # Pydantic appends the key marker AFTER the key it is about, so it
        # is always the final segment. Requiring that, and not just that
        # the walk stands past a mapping key, is what keeps a table whose
        # key really is ``[key]`` from having it dropped: nested one level
        # down, that key is not last and the marker after it is.
        if segment == _KEY_MARKER and after_mapping_key and index == last:
            continue
        container = cursor.model if isinstance(cursor, _AtModel) else None
        at_union = isinstance(cursor, _AtUnion)
        if at_union and union_path is None:
            # The segment about to be consumed is a member LABEL, so the
            # path built so far is the union's own address, whether this
            # error stops here or continues inside the member. First one
            # wins: see ``_Problem.union_path`` for why outermost.
            union_path = ".".join(parts)
        after_mapping_key = isinstance(cursor, _AtElement) and cursor.mapping
        keep, cursor = _advance(cursor, segment)
        if not keep:
            continue
        if isinstance(segment, int):
            _append_index(parts, segment)
        else:
            parts.append(segment)
    return _Address(path=".".join(parts), container=container, at_union=at_union, union_path=union_path)


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
        if cursor.item_model is not None:
            return True, _AtModel(cursor.item_model)
        if cursor.item_arms:
            return True, _AtTag(cursor.item_arms)
        return True, _AtUnion(cursor.item_union_members) if cursor.item_union_members else None
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
        return _AtElement(
            shape.item_model,
            item_union_members=shape.item_union_members,
            item_arms=shape.item_arms,
            mapping=shape.collection is Collection.MAPPING,
        )
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
