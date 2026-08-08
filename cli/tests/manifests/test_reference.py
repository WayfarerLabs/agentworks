"""The collector and the skeleton renderer, over FIXTURE schemas.

The shipped kinds are covered end to end by ``test_samples.py`` (every
kind renders, uncomments, loads, and builds a registry). What that cannot
cover is a shape no shipped model happens to have, or a shape whose
handling is right today by luck. So the shapes are pinned here against
models the app does not ship: requiredness, defaults, examples, a nested
block, a collection of blocks, a discriminated union with one arm
rendered, an owner-templated field, and a root model.

``skeleton_text`` over a hand-built record renders a fixture model as a
kind's spec, which is what lets a fixture shape be asserted without
registering a kind for it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

import pytest
import yaml
from pydantic import Discriminator, Field

from agentworks.errors import ValidationError
from agentworks.manifests.field_tree import FieldEntry, field_tree, worth_showing
from agentworks.manifests.reference import SchemaReference, reference_for
from agentworks.manifests.skeleton import skeleton_text
from agentworks.schema import MAPPING_KEY, UNSET, AgwModel, AgwRootModel, SecretRef
from tests.schema._fixture_models import (
    AzureLike,
    CatalogLike,
    FieldTaggedCollectionSite,
    GithubLike,
    SiteLike,
    TaggedCollectionSite,
    TemplateLike,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class Exampled(AgwModel):
    """A model whose author wrote example values."""

    host: str = Field(examples=["me@gpu-box"])
    packages: list[str] = Field(default_factory=list, examples=[["zsh", "ripgrep"]])
    mode: Literal["fast", "safe"] = "safe"
    replicas: int = 3
    tags: list[str] = Field(default_factory=list)
    note: str | None = None
    enabled: bool = False


class Route(AgwModel):
    """One element of a required list of tables."""

    host: str = Field(examples=["gpu-box"])
    user: str = Field(examples=["me"])


class Routed(AgwModel):
    """A model with a REQUIRED list of tables.

    No shipped kind has one, which is the only reason the sequence
    element rendered as a mapping key named ``-`` for as long as it did:
    the defect was always commented out, so the end-to-end
    uncomment-and-load suite never met it.
    """

    routes: list[Route]


class Layout(StrEnum):
    """A closed field spelled as an enum rather than as a ``Literal``."""

    TILED = "tiled"
    VERTICAL = "vertical"


class Enumed(AgwModel):
    """A model with an enum-typed field carrying a default.

    ``FieldDoc`` carries enum MEMBERS, and a member is not something
    pyyaml can represent, so the default reached the dumper and took the
    whole document down with a ``RepresenterError``.
    """

    layout: Layout = Layout.VERTICAL
    host: str = Field(examples=["me@gpu-box"])


def _reference(model: type[AgwModel]) -> SchemaReference:
    """``model`` as a documented spec, with no kind registered for it."""
    return SchemaReference(
        target="fixture",
        kind="fixture",
        implementation=None,
        category="declarable",
        title="Fixtures",
        summary="a fixture kind",
        overview="What a fixture is.",
        metadata=(),
        spec=field_tree(model),
        alternatives=(),
        root_value=None,
    )


def _spec_lines(model: type[AgwModel]) -> list[str]:
    """The rendered spec block, one line per entry, prose dropped."""
    text = skeleton_text(_reference(model))
    body = text.split("#spec:\n", 1)[1]
    return body.splitlines()


def _entries_by_name(reference: SchemaReference) -> dict[str, FieldEntry]:
    return {entry.name: entry for entry in reference.spec}


def _walk(entries: tuple[FieldEntry, ...]) -> Iterator[FieldEntry]:
    for entry in entries:
        yield entry
        yield from _walk(entry.children)


# --- what a value is -------------------------------------------------------


def test_an_authored_example_is_what_the_sample_writes() -> None:
    assert "#  host: me@gpu-box" in _spec_lines(Exampled)


def test_a_closed_field_with_ONE_value_writes_it() -> None:
    """The union arm's tag: one value, so it is the value rather than a
    choice among values, and that is what makes a rendered arm's ``name``
    correct rather than guessed."""
    assert "#    name: lima" in _spec_lines(SiteLike)


def test_a_closed_field_with_SEVERAL_values_writes_its_default() -> None:
    """``mode`` could hold either, and the type line already lists both.
    Writing the first one would offer an arbitrary pick beside a
    parenthetical naming a different default, which is what
    ``tmux_layout`` did: suggested ``tiled``, said ``default:
    aw-session-vertical``."""
    lines = _spec_lines(Exampled)

    assert "#  # mode: safe" in lines
    assert "#  # mode: fast" not in lines


def test_a_default_worth_showing_is_shown_and_an_empty_one_is_not() -> None:
    """``replicas: 3`` says what omitting the field does. ``tags: []`` does
    not, so the placeholder says what may go in it instead."""
    lines = _spec_lines(Exampled)

    assert "#  # replicas: 3" in lines
    assert "#  # tags: [<string>]" in lines
    assert "#  # enabled: false" in lines, "false is a value, not an absence"


def test_a_field_with_no_default_and_no_example_gets_a_typed_placeholder() -> None:
    assert "#  # note: <string>" in _spec_lines(Exampled)


@pytest.mark.parametrize(
    ("default", "shown"),
    [(3, True), (0, True), (False, True), ("", False), ([], False), ({}, False), (None, False), (UNSET, False)],
)
def test_worth_showing_keeps_falsy_scalars_and_drops_empty_containers(default: object, shown: bool) -> None:
    assert worth_showing(default) is shown


# --- required versus optional ----------------------------------------------


def test_only_required_fields_are_live_document_lines() -> None:
    """The property the whole design rests on: an uncommented skeleton
    carries exactly the fields an operator must write, so it loads."""
    lines = _spec_lines(Exampled)
    live = [line for line in lines if line.startswith("#  ") and not line.startswith("#  #")]

    assert live == ["#  host: me@gpu-box"]


def test_an_owner_templated_field_is_not_the_operators_to_write() -> None:
    """``token`` is required to pydantic and optional to the operator: the
    model fills it from its owner. The skeleton says so, and says what the
    omission resolves to."""
    lines = _spec_lines(GithubLike)

    assert "#  # token: <string>" in lines
    # Rejoined, because the explanation wraps: what is pinned is the text,
    # not where the wrap lands.
    flowed = " ".join(line.removeprefix("#").strip(" #") for line in lines)
    assert "defaults to the resource named `git-token-<this resource's name>`" in flowed


def test_a_reference_field_says_what_it_names() -> None:
    assert any("names a vm-template" in line for line in _spec_lines(TemplateLike))


# --- nesting ---------------------------------------------------------------


def test_a_nested_block_renders_as_a_block() -> None:
    lines = _spec_lines(AzureLike)

    assert "#  # service_principal:" in lines
    assert "#    # tenant_id: <string>" in lines


def test_a_collection_of_blocks_renders_one_element_under_a_placeholder() -> None:
    """A model says a collection holds tables without saying how many, so
    ONE element is rendered under a placeholder that says so. Leaving it
    out is what made FR10's "complete skeleton" promise false for a
    catalog field.

    Both shapes of collection, over the one fixture that has each: a
    sequence, whose element has no key, and a table, whose element hangs
    under a placeholder key. They were two tests reading the same rendered
    document for the same rule.
    """
    lines = _spec_lines(CatalogLike)

    assert "#  # vm_sizes:" in lines
    assert "#    # one element, as an example:" in lines
    # A sequence element has no key. `-:` is a mapping under a key
    # literally named `-`, which is a different document.
    assert "#    # -" in lines
    assert "#      # cpus: <integer>" in lines

    assert "#    # one entry, as an example:" in lines
    assert "#    # <key>:" in lines


def _uncommented_spec(model: type[AgwModel]) -> object:
    """The spec block of ``model``'s skeleton, put through the documented
    one-``#`` strip and read as YAML."""
    text = skeleton_text(_reference(model))
    stripped = "\n".join(line.removeprefix("#") for line in text.splitlines())
    document = yaml.safe_load(stripped)
    assert isinstance(document, dict)
    return document["spec"]


def test_an_uncommented_required_list_of_tables_loads_and_validates() -> None:
    """The module's headline promise, over the one shape no shipped kind
    has.

    Every other required field renders as a scalar on its own key line, so
    a broken element opener stayed invisible: a commented line is not YAML
    and nothing was ever asked to read it. Uncommenting is what asks.
    """
    spec = _uncommented_spec(Routed)

    assert spec == {"routes": [{"host": "gpu-box", "user": "me"}]}
    assert Routed.model_validate(spec).routes[0].host == "gpu-box"


def test_an_enum_default_renders_as_the_value_a_document_carries() -> None:
    """``FieldDoc.choices`` carries enum MEMBERS, and pyyaml has no
    representation for one, so an enum-typed field with a default took the
    whole rendered document down rather than printing a line."""
    assert "#  # layout: vertical" in _spec_lines(Enumed)


# --- unions ----------------------------------------------------------------


def test_one_arm_is_rendered_and_the_rest_are_listed() -> None:
    """A document holds one arm, so rendering them all would produce a
    sample that cannot be uncommented."""
    lines = _spec_lines(SiteLike)

    assert any("One of: lima, proxmox. Shown here: lima." in line for line in lines)
    assert "#    name: lima" in lines
    assert not any("proxmox" in line and "name:" in line for line in lines)


def test_the_rendered_arm_is_the_first_registered_one() -> None:
    entry = _entries_by_name(_reference(SiteLike))["platform"]

    assert entry.rendered == "lima"
    assert [alt.name for alt in entry.alternatives] == ["lima", "proxmox"]
    assert {child.name for child in entry.children} == {"name", "vm_host"}


# "a union that is not a capability offers no pointer" stood here and was
# VACUOUS: :func:`_reference` builds its tree with ``field_tree(model)``,
# no capability kind, and ``field_tree`` can only address an arm it was
# given a kind for (``field_tree.py:409``), so every ``target`` was None
# by construction. Neither mutation of that line (address every arm,
# address none) could fail it. The test below is the one that catches
# both, because it drives the case that can actually go wrong.


def test_an_alternative_gets_an_address_only_when_the_address_exists() -> None:
    """Being collected UNDER a capability kind does not make every union in
    the tree a union of its implementations.

    A kind's whole spec is collected under the capability it hosts, arms
    included, so any other tagged block an author writes inside one was
    handed `agw resource describe-kind vm-platform/leaf`: a printed command
    that fails. The registry decides, since it is what the command asks.
    """
    (element,) = field_tree(Nodes, "vm-platform")[0].children

    assert [alt.name for alt in element.alternatives] == ["group", "leaf"]
    assert [alt.target for alt in element.alternatives] == [None, None]
    # The counterpart, over the real registry: an arm that IS an
    # implementation keeps its address.
    platform = _entries_by_name(reference_for("vm-site"))["platform"]
    assert platform.alternatives
    assert all(alt.target == f"vm-platform/{alt.name}" for alt in platform.alternatives)


class SelfReachingArm(AgwModel):
    """A union arm reachable from itself, which a plugin's config model
    may be: a group whose members are groups."""

    name: Literal["group"]
    member: Annotated[SelfReachingArm, Discriminator("name")] | None = None


class SelfReaching(AgwModel):
    """A field whose union arm reaches itself."""

    group: Annotated[SelfReachingArm, Discriminator("name")]


SelfReachingArm.model_rebuild()
SelfReaching.model_rebuild()


def test_a_union_arm_reachable_from_itself_stops_rather_than_recurring() -> None:
    """``iter_field_docs`` threads a cycle guard, and the tree re-entered
    it from scratch for every expanded arm, so a self-reachable arm ran
    until the interpreter gave up: ``describe-kind``, ``sample``, and the
    guide's field reference all died on the same model.

    One level is expanded and the second is not, and ``rendered`` says so
    rather than claiming an arm whose fields are absent.
    """
    entry = _entries_by_name(_reference(SelfReaching))["group"]

    assert entry.rendered == "group"
    inner = {child.name: child for child in entry.children}["member"]
    assert [alt.name for alt in inner.alternatives] == ["group"]
    assert inner.children == ()
    assert inner.rendered is None


# --- a collection whose ELEMENTS are a union -------------------------------


class LeafNode(AgwModel):
    """The arm that holds a value."""

    name: Literal["leaf"]
    value: str


class GroupNode(AgwModel):
    """An arm whose members are arms: a tagged collection reachable from
    itself, which is the shape a plugin author writes for a tree."""

    name: Literal["group"]
    members: list[Annotated[GroupNode | LeafNode, Discriminator("name")]] = Field(default_factory=list)


class Nodes(AgwModel):
    """A REQUIRED collection of tagged blocks.

    Required, because that is what asks the skeleton its headline
    question: an optional block renders commented, and a commented line is
    not YAML that anything has to read.
    """

    nodes: list[Annotated[GroupNode | LeafNode, Discriminator("name")]]


GroupNode.model_rebuild()
Nodes.model_rebuild()


def test_a_collection_of_tagged_blocks_renders_an_element_naming_its_arms() -> None:
    """The element is where the arms belong.

    Read one level up, the arm's fields would land beside the collection
    rather than inside an element of it. Read nowhere, which is what
    happened until the stream carried them, the whole field rendered as an
    opaque "list of table" and an operator was never told that a member
    says which kind of member it is.
    """
    lines = _spec_lines(Nodes)

    assert "#  nodes:" in lines
    assert "#    # one element, as an example:" in lines
    assert any("One of: group, leaf. Shown here: group." in line for line in lines)
    assert "#    -" in lines
    assert "#      name: group" in lines


def test_an_uncommented_required_list_of_tagged_blocks_loads_and_validates() -> None:
    """The same headline promise as the plain list of tables, over the
    shape that used to render as ``[<value>]``: a placeholder shaped like
    a list, holding a string where the loader wants a tagged table."""
    spec = _uncommented_spec(Nodes)

    assert spec == {"nodes": [{"name": "group"}]}
    assert Nodes.model_validate(spec).nodes[0].name == "group"


def test_a_table_of_tagged_blocks_hangs_its_element_under_a_placeholder_key() -> None:
    entries = _entries_by_name(_reference(TaggedCollectionSite))
    (element,) = entries["platforms_by_name"].children

    assert element.name == MAPPING_KEY
    assert element.rendered == "lima"
    assert {child.name for child in element.children} == {"name", "vm_host"}


def test_the_tree_offers_every_element_arm_the_stream_does() -> None:
    """The presenter drops no arm the stream carries, at any depth of any
    model: one element node, listing every alternative in the order the
    author declared them.

    Three models in one loop, reporting every element node that dropped an
    arm. The presenter is shared, so a rule that starts dropping drops on
    all three, and the list of which nodes lost what is the report worth
    reading.
    """
    dropped: list[str] = []
    for model_cls in (TaggedCollectionSite, FieldTaggedCollectionSite, Nodes):
        offered = 0
        for entry in _walk(field_tree(model_cls)):
            if not entry.doc.item_union_arms:
                continue
            offered += 1
            (element,) = entry.children
            offers = [alt.name for alt in element.alternatives]
            carries = [arm.tag for arm in entry.doc.item_union_arms]
            if offers != carries:
                dropped.append(f"{model_cls.__name__}.{'.'.join(entry.doc.path)}: offers {offers}, stream {carries}")
        assert offered, f"{model_cls.__name__} has no tagged collection, so it proves nothing"
    assert not dropped, "\n".join(dropped)


def test_a_tagged_collection_arm_reachable_from_itself_stops_rather_than_recurring() -> None:
    """The element goes through the tree's own path guard, not around it.

    A group whose members are groups is finite as a model and unbounded as
    a document, so one level is expanded and the next says what may go
    there without opening it. Expanding the element outside the guard
    would run until the interpreter gave up and take ``describe-kind``,
    ``sample``, and the guide's field reference down together.
    """
    (element,) = _entries_by_name(_reference(Nodes))["nodes"].children
    inner = {child.name: child for child in element.children}["members"]
    (deeper,) = inner.children

    assert element.rendered == "group"
    assert [alt.name for alt in deeper.alternatives] == ["group", "leaf"]
    assert deeper.children == ()
    assert deeper.rendered is None, "nothing was expanded here, and the record says so"


def test_an_unexpanded_arm_does_not_name_what_is_shown() -> None:
    """``rendered`` is None there, and "Shown here: None." is Python
    talking to an operator."""
    flowed = " ".join(_spec_lines(Nodes))

    assert "One of: group, leaf." in flowed
    assert "Shown here: None" not in flowed


def test_two_sibling_fields_sharing_an_arm_each_expand_it() -> None:
    """The guard is the current PATH, not an accumulating set. A set would
    expand the first field's union and leave the second field's a bare
    line, which in a generated sample is a whole block an operator has to
    write and is not told about."""

    class TwoGroups(AgwModel):
        first: Annotated[SelfReachingArm, Discriminator("name")]
        second: Annotated[SelfReachingArm, Discriminator("name")]

    TwoGroups.model_rebuild()
    entries = _entries_by_name(_reference(TwoGroups))

    assert entries["first"].rendered == "group"
    assert entries["second"].rendered == "group"


# --- root models -----------------------------------------------------------


class WrappedSite(AgwModel):
    """A field whose type is a root model wrapping a union: the shape the
    capability config union actually has."""

    platform: RootUnion


class LimaArmed(AgwModel):
    name: Literal["lima"]
    vm_host: str | None = None


class ProxmoxArmed(AgwModel):
    name: Literal["proxmox"]
    token: Annotated[str, SecretRef(usage="the token")] | None = None


class RootUnion(AgwRootModel[Annotated[LimaArmed | ProxmoxArmed, Discriminator("name")]]):
    """The generated union wrapper, as ``capability_config_union`` builds it."""


WrappedSite.model_rebuild()


def test_a_root_model_wrapper_contributes_no_path_segment() -> None:
    """``root`` is the wrapper's mechanism and never a key an operator
    writes. Without the collapse, every capability block would render an
    imaginary ``root:`` line between the field and its config."""
    entry = _entries_by_name(_reference(WrappedSite))["platform"]

    assert entry.rendered == "lima"
    assert {child.name for child in entry.children} == {"name", "vm_host"}
    assert "root" not in {found.name for found in _walk((entry,))}


# --- the real surface ------------------------------------------------------


def test_a_capability_kind_lists_its_implementations() -> None:
    reference = reference_for("vm-platform")

    assert reference.category == "capability"
    assert {alt.name for alt in reference.alternatives} >= {"lima", "wsl2"}
    assert reference.spec == ()


def test_an_implementation_documents_the_model_it_declares() -> None:
    reference = reference_for("vm-platform/lima")

    assert reference.implementation == "lima"
    assert {entry.name for entry in reference.spec} == {"name", "placement"}


def test_a_nested_tagged_union_lists_its_arms_and_expands_one() -> None:
    """lima's ``placement`` is the framework's first discriminated union
    that is NOT a capability-config union, so this pins that the shared
    field-tree machinery expands it with no special casing: both arms are
    offered, the first is expanded, and its own fields are the children.

    ``target`` is None on both, unlike a capability arm's: there is no
    ``describe-kind`` address for one arm of an ordinary union, and
    inventing one would print a command that fails."""
    (placement,) = [e for e in reference_for("vm-platform/lima").spec if e.name == "placement"]

    assert [alt.name for alt in placement.alternatives] == ["local", "ssh"]
    assert all(alt.target is None for alt in placement.alternatives)
    assert placement.rendered == "local"
    assert [child.name for child in placement.children] == ["mode"]
    # The arm summaries come from the arm MODELS' docstrings (a capability
    # union reads its impls' one-liners instead), so they are real prose.
    assert placement.alternatives[1].summary == "Run limactl on another host over SSH."


def test_a_root_model_config_is_described_as_a_value() -> None:
    """A secret backend's per-secret mapping may be a bare string, which no
    mapping-shaped model can be, so its config is a root model and there is
    no field list to print."""
    reference = reference_for("secret-backend/env-var")

    assert reference.spec == ()
    assert reference.root_value is not None
    assert reference.root_value.type_label == "string"


def test_an_unknown_kind_and_an_unknown_implementation_are_typed_refusals() -> None:
    with pytest.raises(ValidationError, match="unknown kind"):
        reference_for("nope")
    with pytest.raises(ValidationError, match="no vm-platform named 'nope'"):
        reference_for("vm-platform/nope")
    with pytest.raises(ValidationError, match="has no implementations"):
        reference_for("secret/npm-token")
