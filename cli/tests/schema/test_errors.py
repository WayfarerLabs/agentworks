"""Tests for the error bridge.

The backbone of this file is the FRD's representative-mistakes corpus
(unknown key, wrong type, missing required field, bad capability name),
each asserting owner framing and file/position context at least as good
as what the hand-rolled validators produce today.

Framing is pinned at BOTH cardinalities, because that is where the
design nearly broke: a single error has to keep decode's shipped
one-line shape, and a MULTI-error batch has to leave no line unlocated,
which neither of today's call-site framings can do. A test that only
checks "framing composes without doubling" passes vacuously on one line,
so the multi-error case is asserted explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from agentworks.errors import ConfigError
from agentworks.schema import (
    MAX_ERROR_LINES,
    AgwModel,
    AgwRootModel,
    RefOwner,
    config_error_from,
    render_validation_error,
    validation_context,
)
from agentworks.source_location import SourceLocation

from ._fixture_models import (
    CatalogLike,
    MappingValueLike,
    OneArmSite,
    PrincipalLike,
    ShoutyKey,
    SiteLike,
    StringOrTableRoot,
    StringRoot,
    TableWithConstrainedKeys,
    TemplateLike,
    UndiscriminatedSite,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

OWNER = RefOwner(kind="vm-site", name="lab")
WHERE = SourceLocation(file=Path("sites.yaml"), line=12)

#: One blob producing three problems in one arm: a wrong type and two
#: unknown keys. The multi-error framing has nothing to say on a
#: single-line message, so every batch assertion runs against this.
THREE_PROBLEMS: Mapping[str, object] = {"platform": {"name": "lima", "vm_host": 8, "regions": "x", "cpus": 1}}


class Choosy(AgwModel):
    """A model carrying the constrained and closed-choice spellings whose
    messages depend on the error's context rather than its type alone."""

    arch: Literal["arm64", "x86_64"] = "x86_64"
    label: str = Field(default="x", min_length=1)
    slug: str = Field(default="abc", min_length=3)


class Bounded(AgwModel):
    """A model with a bound the normalization table deliberately does not
    cover, so the fall-through has something real to carry."""

    cpus: int = Field(default=1, ge=1)


def _fails(model_cls: type[BaseModel], blob: object) -> PydanticValidationError:
    """The ``ValidationError`` ``blob`` raises against ``model_cls``.

    The owner rides the validation context, as it does at every real call
    site: a model with an owner-templated field refuses to validate
    without it.
    """
    with pytest.raises(PydanticValidationError) as caught:
        model_cls.model_validate(blob, context=validation_context(OWNER))
    return caught.value


def _lines(model_cls: type[BaseModel], blob: object) -> list[str]:
    return render_validation_error(_fails(model_cls, blob), model_cls=model_cls, owner=OWNER)


def _raised(
    model_cls: type[BaseModel],
    blob: object,
    *,
    location: SourceLocation | None = WHERE,
) -> ConfigError:
    return config_error_from(
        _fails(model_cls, blob),
        model_cls=model_cls,
        owner=OWNER,
        location=location,
    )


# -- The representative-mistakes corpus ---------------------------------------
#
# A fifth entry, the retired sibling capability shape, lives END TO END in
# ``tests/manifests/test_capability_shape.py`` instead of here. It is a
# whole-document mistake rather than a blob-versus-model one: to this layer
# it is just an unknown ``platform_config`` key beside a ``platform`` that
# is not a table, which is the generic pair its error exists to beat. See
# ``test_sibling_shape_is_rejected_with_the_exact_rewrite``.


def test_an_unknown_key_names_the_fields_that_are_valid() -> None:
    lines = _lines(PrincipalLike, {"client_id": "c", "tenant_id": "t", "client_ids": 1})

    assert lines == ["vm-site/lab.client_ids: unknown field; expected one of: client_id, secret, tenant_id"]


def test_a_wrong_type_says_what_the_field_must_be() -> None:
    assert _lines(PrincipalLike, {"client_id": 8, "tenant_id": "t"}) == ["vm-site/lab.client_id: must be a string"]


def test_a_missing_required_field_says_it_is_required() -> None:
    assert _lines(PrincipalLike, {"tenant_id": "t"}) == ["vm-site/lab.client_id: is required"]


def test_a_bad_capability_name_lists_the_names_that_are_registered() -> None:
    lines = _lines(SiteLike, {"platform": {"name": "lmia", "vm_host": "h"}})

    assert lines == ["vm-site/lab.platform: unknown name 'lmia'; registered: 'lima', 'proxmox'"]


def test_an_absent_capability_name_reads_as_a_missing_field() -> None:
    assert _lines(SiteLike, {"platform": {"vm_host": "h"}}) == ["vm-site/lab.platform: name is required"]


@pytest.mark.parametrize(
    "blob",
    [
        {"client_id": 8, "tenant_id": "t"},
        {"tenant_id": "t"},
        {"client_id": "c", "tenant_id": "t", "client_ids": 1},
    ],
)
def test_every_corpus_entry_is_owner_framed(blob: dict[str, object]) -> None:
    (line,) = _lines(PrincipalLike, blob)

    assert line.startswith("vm-site/lab.")


# -- Framing: one error -------------------------------------------------------


def test_one_error_renders_in_decodes_shipped_single_line_shape() -> None:
    error = _raised(PrincipalLike, {"client_id": 8, "tenant_id": "t"})

    assert str(error) == "sites.yaml:12: vm-site/lab.client_id: must be a string"


def test_one_error_without_a_location_is_just_the_owner_framed_line() -> None:
    error = _raised(PrincipalLike, {"client_id": 8, "tenant_id": "t"}, location=None)

    assert str(error) == "vm-site/lab.client_id: must be a string"


def test_the_owner_rides_along_as_the_errors_entity() -> None:
    error = _raised(PrincipalLike, {"client_id": 8, "tenant_id": "t"})

    assert (error.entity_kind, error.entity_name) == ("vm-site", "lab")


def test_a_hint_is_carried_through() -> None:
    error = config_error_from(
        _fails(PrincipalLike, {"client_id": 8, "tenant_id": "t"}),
        model_cls=PrincipalLike,
        owner=OWNER,
        hint="see `agw resource describe`",
    )

    assert error.hint == "see `agw resource describe`"


def test_the_bridge_produces_a_config_error() -> None:
    """Never the agentworks ``ValidationError``, which is a different
    thing (invalid input at the command surface)."""
    assert isinstance(_raised(PrincipalLike, {"tenant_id": "t"}), ConfigError)


# -- Framing: several errors --------------------------------------------------


def test_several_errors_render_under_one_located_header() -> None:
    error = _raised(SiteLike, THREE_PROBLEMS)

    assert str(error) == (
        "sites.yaml:12: vm-site/lab: 3 problems\n"
        "  platform.vm_host: must be a string\n"
        "  platform.regions: unknown field; expected one of: name, vm_host\n"
        "  platform.cpus: unknown field; expected one of: name, vm_host"
    )


def test_no_line_of_a_multi_error_batch_is_unlocated() -> None:
    """The assertion the design turns on: not merely that nothing is
    doubled, but that EVERY line is reachable from one location. Both of
    today's call-site framings fail this one."""
    header, *body = str(_raised(SiteLike, THREE_PROBLEMS)).splitlines()

    assert header.startswith("sites.yaml:12: ")
    assert body, "a multi-error batch renders its problems on their own lines"
    assert all(line.startswith("  ") for line in body), "an unindented line reads as a new, unlocated message"


def test_a_multi_error_header_states_the_true_count() -> None:
    header = str(_raised(SiteLike, THREE_PROBLEMS)).splitlines()[0]

    assert header.endswith("vm-site/lab: 3 problems")


def test_a_multi_error_batch_without_a_location_still_reads_as_one_message() -> None:
    header, *body = str(_raised(SiteLike, THREE_PROBLEMS, location=None)).splitlines()

    assert header == "vm-site/lab: 3 problems"
    assert all(line.startswith("  ") for line in body)


def _overlong_blob() -> dict[str, object]:
    """A document with three more problems than the cap shows."""
    unknown: dict[str, object] = {f"unknown_{index}": index for index in range(MAX_ERROR_LINES + 3)}
    return {"platform": {"name": "lima"}, **unknown}


def test_a_capped_batch_says_how_many_it_did_not_show() -> None:
    header, *body = str(_raised(SiteLike, _overlong_blob())).splitlines()

    assert header == f"sites.yaml:12: vm-site/lab: {MAX_ERROR_LINES + 3} problems"
    assert len(body) == MAX_ERROR_LINES + 1
    assert body[-1] == "  ... and 3 more"


def test_the_pure_renderer_is_uncapped() -> None:
    """The cap belongs to the raised message, not to the diagnostic text:
    a surface rendering the lines itself decides how many to show."""
    assert len(_lines(SiteLike, _overlong_blob())) == MAX_ERROR_LINES + 3


def test_every_error_the_exception_carries_gets_a_line() -> None:
    exc = _fails(SiteLike, THREE_PROBLEMS)

    assert len(render_validation_error(exc, model_cls=SiteLike, owner=OWNER)) == exc.error_count()


# -- Path rendering -----------------------------------------------------------


def test_the_union_arm_tag_is_dropped_from_the_path() -> None:
    """Pydantic's loc is ``('platform', 'lima', 'vm_host')``, but the
    operator wrote no ``lima`` key to go and fix."""
    assert _lines(SiteLike, {"platform": {"name": "lima", "vm_host": 8}}) == [
        "vm-site/lab.platform.vm_host: must be a string"
    ]


def test_an_unknown_key_inside_a_union_arm_lists_that_arms_fields() -> None:
    assert _lines(SiteLike, {"platform": {"name": "lima", "regions": 1}}) == [
        "vm-site/lab.platform.regions: unknown field; expected one of: name, vm_host"
    ]


def test_a_nested_block_renders_as_a_dotted_path() -> None:
    assert _lines(CatalogLike, {"accounts_by_name": {"prod": {"secret": 8}}}) == [
        "vm-site/lab.accounts_by_name.prod.secret: must be a string"
    ]


def test_a_deeper_block_keeps_its_whole_path() -> None:
    class Deep(AgwModel):
        """The innermost block."""

        entry: str = "x"

    class Middle(AgwModel):
        """The block between."""

        deep: Deep = Deep()

    class Top(AgwModel):
        """The document."""

        middle: Middle = Middle()

    assert _lines(Top, {"middle": {"deep": {"entry": 8}}}) == ["vm-site/lab.middle.deep.entry: must be a string"]


def test_a_list_index_belongs_to_the_field_that_holds_it() -> None:
    blob = {"vm_sizes": [{"cpus": 1, "memory": 1.0, "size": "s"}, {"cpus": "two", "memory": 1.0, "size": "s"}]}

    assert _lines(CatalogLike, blob) == ["vm-site/lab.vm_sizes[1].cpus: must be an integer"]


def test_a_marked_list_element_is_addressed_by_its_index() -> None:
    assert _lines(TemplateLike, {"inherits": ["a", 8]}) == ["vm-site/lab.inherits[1]: must be a string"]


def test_a_whole_document_problem_carries_no_path() -> None:
    assert _lines(StringRoot, {"a": 1}) == ["vm-site/lab: must be a string"]


def test_a_root_models_problem_is_framed_by_its_location_too() -> None:
    assert str(_raised(StringRoot, {"a": 1})) == "sites.yaml:12: vm-site/lab: must be a string"


def test_a_root_model_addresses_the_fields_of_what_it_wraps() -> None:
    """A root model's errors carry no ``root`` segment, so the walk has
    to start inside what the root wraps or it loses the path on the very
    first segment."""

    class Wrapper(AgwRootModel[SiteLike]):
        """A root model wrapping a mapping-shaped model."""

    assert _lines(Wrapper, {"platform": {"name": "lima", "vm_host": 8}}) == [
        "vm-site/lab.platform.vm_host: must be a string"
    ]


def test_a_single_arm_unions_tag_is_dropped_like_any_other() -> None:
    """``Union[(X,)]`` is ``X``, so a one-arm discriminated union collapses
    to a bare model while pydantic goes on dispatching on the tag. Read as
    an ordinary nested block it would render the tag as a field the
    operator never wrote, and lose the arm's field list with it. Live the
    moment a capability kind has a single registered implementation."""
    assert _lines(OneArmSite, {"platform": {"name": "lima", "vm_host": 8}}) == [
        "vm-site/lab.platform.vm_host: must be a string"
    ]
    assert _lines(OneArmSite, {"platform": {"name": "lima", "bogus": 1}}) == [
        "vm-site/lab.platform.bogus: unknown field; expected one of: name, vm_host"
    ]


def test_an_undiscriminated_unions_arm_name_is_dropped_too() -> None:
    """Pydantic tries every arm of an undiscriminated union and prefixes
    each failure with that arm's own CLASS NAME, which the operator never
    wrote and could not act on. It is the same problem as a
    discriminated union's tag one shape over, so it gets the same answer:
    the segment is dropped and the walk continues inside the named arm.
    Live, not hypothetical: a secret backend's mapping is a bare string
    or a table, which has no tag to discriminate on.
    """
    lines = _lines(UndiscriminatedSite, {"platform": {"name": "lima", "vm_host": 8}})

    assert "vm-site/lab.platform.vm_host: must be a string" in lines


def test_an_undiscriminated_union_keeps_its_arms_field_list() -> None:
    """Continuing INTO the arm is what the drop buys: without it the walk
    loses track and the unknown-key message cannot name the valid
    fields."""
    lines = _lines(UndiscriminatedSite, {"platform": {"name": "lima", "bogus": 1}})

    assert "vm-site/lab.platform.bogus: unknown field; expected one of: name, vm_host" in lines


def test_a_constrained_table_key_is_addressed_without_pydantics_key_marker() -> None:
    """Pydantic's loc is ``('env', 'lower', '[key]')``: the marker says
    the failure is in the KEY rather than the value, and the key is
    already the segment before it, so the marker is one more thing the
    operator never wrote."""
    assert _lines(TableWithConstrainedKeys, {"env": {"lower": "x"}}) == [
        "vm-site/lab.env.lower: invalid key 'lower' (must be upper case)"
    ]


def test_a_table_key_spelled_like_the_marker_is_still_addressed() -> None:
    """The drop is keyed on the walk standing just past a MAPPING key, so
    a table whose key genuinely is ``[key]`` keeps it. Silly input,
    but a rule that silently renamed an operator's key would be a wrong
    answer rather than a rough one."""
    assert _lines(TableWithConstrainedKeys, {"env": {"[key]": "x"}}) == [
        "vm-site/lab.env.[key]: invalid key '[key]' (must be upper case)"
    ]


def test_a_nested_table_keeps_a_key_spelled_like_the_marker() -> None:
    """The drop is conditioned on the marker being the LAST segment as
    well as on standing past a mapping key, and that is what this shape
    needs: one level down, the operator's literal ``[key]`` is not last
    and pydantic's marker after it is. Without the last-segment condition
    the operator's own key was the one dropped."""

    class Nested(AgwModel):
        """A table of tables whose inner keys are constrained."""

        outer: dict[str, dict[ShoutyKey, int]] = Field(default_factory=dict)

    assert _lines(Nested, {"outer": {"a": {"[key]": 1}}}) == [
        "vm-site/lab.outer.a.[key].[key]: invalid key '[key]' (must be upper case)"
    ]


def test_a_nested_tables_key_marker_is_noise_the_walk_does_not_reach() -> None:
    """The half this does NOT fix, said out loud. The walk loses track at
    a collection whose elements are themselves a collection, so it cannot
    tell the marker from a segment the operator wrote and keeps it. Noise
    in the path, never a wrong one, and no kind spec has this shape: the
    env table is the only constrained-key mapping and its values are
    models."""

    class Nested(AgwModel):
        """A table of tables whose inner keys are constrained."""

        outer: dict[str, dict[ShoutyKey, int]] = Field(default_factory=dict)

    assert _lines(Nested, {"outer": {"a": {"low": 1}}}) == [
        "vm-site/lab.outer.a.low.[key]: invalid key 'low' (must be upper case)"
    ]


def test_a_value_matching_no_alternative_of_a_union_is_one_problem() -> None:
    """Pydantic tries each member of an undiscriminated union and reports
    a failure per member, so one mistake arrives as three problems at one
    address. The operator has one thing to fix and reads one line."""
    assert _lines(MappingValueLike, {"backend_mappings": {"npm": 3}}) == [
        "vm-site/lab.backend_mappings.npm: must be a string, a table, or False"
    ]


def test_a_collapsed_union_line_replaces_pydantics_member_labels() -> None:
    """The paths those three problems carried (``...npm.str``,
    ``...npm.dict[str,any]``) named pydantic's members, which is neither
    something the operator wrote nor something they can act on."""
    exc = _fails(MappingValueLike, {"backend_mappings": {"npm": 3}})

    assert exc.error_count() == 3
    assert len(render_validation_error(exc, model_cls=MappingValueLike, owner=OWNER)) == 1


def test_a_failure_inside_a_union_member_is_not_collapsed_away() -> None:
    """A problem DEEPER than the union position is the informative kind
    (it names a field of the member the operator clearly meant), so the
    collapse deliberately leaves those alone."""
    lines = _lines(UndiscriminatedSite, {"platform": {"name": "lima", "vm_host": 8}})

    assert "vm-site/lab.platform.vm_host: must be a string" in lines


def test_alternatives_that_name_no_shape_keep_their_own_lines() -> None:
    """A union member that fails on a LENGTH rather than on a shape has
    no phrase to contribute to an alternatives list, so the run is left
    uncollapsed rather than described with a phrase this module would
    have to invent."""
    lines = _lines(StringOrTableRoot, "")

    assert "vm-site/lab: must not be empty" in lines
    assert len(lines) > 1


def test_an_undiscriminated_root_models_scalar_arm_renders_unprefixed() -> None:
    """A non-model arm has no fields to continue into, so dropping its
    name leaves the message alone at the document level, which is where a
    root model's problems belong anyway."""
    lines = _lines(StringOrTableRoot, "")

    assert "vm-site/lab: must not be empty" in lines


# -- Normalization ------------------------------------------------------------


@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        ({"vm_sizes": 3}, "vm-site/lab.vm_sizes: must be a list"),
        ({"accounts_by_name": 3}, "vm-site/lab.accounts_by_name: must be a table"),
        ({"vm_sizes": [3]}, "vm-site/lab.vm_sizes[0]: must be a table"),
    ],
)
def test_container_types_read_as_the_shapes_an_operator_writes(blob: dict[str, object], expected: str) -> None:
    assert _lines(CatalogLike, blob) == [expected]


def test_a_non_table_where_a_capability_block_belongs_says_table() -> None:
    assert _lines(SiteLike, {"platform": "lima"}) == ["vm-site/lab.platform: must be a table"]


@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        ({"arch": "arm"}, "vm-site/lab.arch: must be one of: 'arm64' or 'x86_64'"),
        ({"label": ""}, "vm-site/lab.label: must not be empty"),
    ],
)
def test_closed_choices_and_emptiness_are_normalized(blob: dict[str, object], expected: str) -> None:
    assert _lines(Choosy, blob) == [expected]


def test_a_constrained_string_shape_reads_as_the_rule_it_must_match() -> None:
    """Pydantic says "String should match pattern '...'". The rule is the
    useful half and the house spells one ``/.../``, as the env-var-name
    validator's own message already does."""

    class Filed(AgwModel):
        """A model with a pattern-constrained field."""

        source_file: str = Field(default="ok.list", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

    assert _lines(Filed, {"source_file": "../etc/passwd"}) == [
        r"vm-site/lab.source_file: must match `^[a-zA-Z0-9][a-zA-Z0-9._-]*$`"
    ]


def test_a_pattern_containing_a_slash_is_still_readable() -> None:
    """Why the rule is not wrapped in ``/.../``: a github ``repos`` entry's
    pattern contains a slash, and slash delimiters would leave an operator
    unable to see where the rule ends."""

    class Scoped(AgwModel):
        """A model whose pattern contains the delimiter a regex usually wears."""

        repo: str = Field(default="a/b", pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

    assert _lines(Scoped, {"repo": "nope"}) == [r"vm-site/lab.repo: must match `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$`"]


def test_a_length_floor_above_one_keeps_pydantics_exact_wording() -> None:
    """Saying "must not be empty" for a ``min_length`` of 3 would be a
    paraphrase that is simply false, so pydantic's own text wins."""
    assert _lines(Choosy, {"slug": "ab"}) == ["vm-site/lab.slug: String should have at least 3 characters"]


def test_an_unmapped_error_type_falls_through_verbatim() -> None:
    """No paraphrase is invented for an error type the table has not
    considered: pydantic's own message is carried through unchanged."""
    exc = _fails(Bounded, {"cpus": 0})
    (detail,) = exc.errors(include_url=False)

    assert detail["type"] not in {"missing", "int_type"}
    assert render_validation_error(exc, model_cls=Bounded, owner=OWNER) == [f"vm-site/lab.cpus: {detail['msg']}"]


def test_the_pure_renderer_answers_for_every_corpus_entry() -> None:
    """The diagnostic entry point is usable from a surface that has no
    business handling an exception of its own."""
    for model_cls, blob in (
        (PrincipalLike, {}),
        (PrincipalLike, {"client_id": 8, "nope": 1}),
        (SiteLike, {"platform": {"name": "nope"}}),
        (SiteLike, {"platform": []}),
        (CatalogLike, {"vm_sizes": [{"cpus": "x"}]}),
        (StringRoot, {"a": 1}),
    ):
        assert _lines(model_cls, blob), f"{model_cls.__name__} rendered no line for {blob!r}"


# -- A validator's own message, and the framing marker -------------------------


class Exclusive(AgwModel):
    """A model whose rule spans two fields, which is where an authored
    ``ValueError`` reaches the bridge."""

    repos: list[str] | None = None
    owner: str | None = None

    @model_validator(mode="after")
    def _mutually_exclusive(self) -> Exclusive:
        if self.repos is not None and self.owner is not None:
            raise ValueError("repos and owner are mutually exclusive")
        return self


def test_a_validators_own_message_is_carried_without_pydantics_prefix() -> None:
    """Pydantic renders an authored ``ValueError`` as "Value error, ...".
    The message is the author's and the prefix is pydantic's
    presentation, so the message is read off the error context rather
    than sliced off the text."""
    assert _lines(Exclusive, {"repos": ["a/b"], "owner": "o"}) == [
        "vm-site/lab: repos and owner are mutually exclusive"
    ]


def test_the_bridge_produces_a_plain_config_error() -> None:
    """There is no marker type any more, and none is needed: every error a
    resource's ``validate_config`` raises comes from here, so the finalize
    pass has one framing to leave alone rather than two to tell apart."""
    raised = _raised(PrincipalLike, {})

    assert type(raised) is ConfigError


def test_an_owner_label_overrides_the_kind_slash_name_framing() -> None:
    """``agw resource migrate`` reports against the TOML file it has not
    rewritten yet, so it frames in that file's vocabulary."""
    owner = RefOwner(kind="vm-site", name="lab", label="[azure]")
    exc = _fails(PrincipalLike, {})

    assert render_validation_error(exc, model_cls=PrincipalLike, owner=owner)[0].startswith("[azure].")
