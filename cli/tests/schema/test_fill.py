"""``filled_defaults``: the boundary fill's own contracts.

What resolves where is pinned in ``test_owner_templates.py`` (against
validation and extraction) and the walk suites; this file pins the
properties of the fill ITSELF: copy-on-write, idempotence, the
default-materialization rule, the shorthand fold, totality, and the
cycle guard.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

import pytest
from pydantic import Discriminator, Field

from agentworks.schema import AgwModel, RefOwner, ScalarShorthand, SecretRef, filled_defaults

from ._fixture_models import (
    ALL_FIXTURES,
    DefaultedUnionSite,
    GithubLike,
    RawDefaultedProvider,
    ScalarOrBlockLike,
    ShorthandLike,
    ShorthandTemplatedLike,
    SiteLike,
)

OWNER = RefOwner(kind="git-credential", name="prod")


def test_a_blob_nothing_fills_comes_back_as_the_same_object() -> None:
    """Copy-on-write, asserted by identity: the common case (no templated
    field anywhere, or every one already written) costs a walk and
    allocates nothing, so callers can fill unconditionally."""
    written = {"token": "custom", "api_url": "https://example"}
    assert filled_defaults(GithubLike, written, OWNER) is written

    nested = {"platform": {"name": "proxmox", "token_secret": "explicit"}}
    assert filled_defaults(SiteLike, nested, OWNER) is nested


def test_filling_twice_is_the_identity_the_second_time() -> None:
    """Idempotence is what lets the production boundaries compose: the
    construct path fills, and validation's own fill downstream must
    change nothing."""
    filled = filled_defaults(GithubLike, {}, OWNER)
    assert filled_defaults(GithubLike, filled, OWNER) is filled


def test_the_input_blob_is_never_mutated() -> None:
    blob: dict[str, object] = {"platform": {"name": "proxmox"}}
    inner = blob["platform"]
    filled = filled_defaults(SiteLike, blob, OWNER)
    assert filled is not blob
    assert blob == {"platform": {"name": "proxmox"}}
    assert inner == {"name": "proxmox"}


def test_a_raw_default_that_needs_a_fill_is_materialized() -> None:
    """Validation answers an omitted field with its declared default, and
    a default authored as raw data may leave a templated field unset, so
    the fill writes the FILLED default into the blob: without this the
    required field inside the default would report itself missing."""
    assert filled_defaults(RawDefaultedProvider, {}, OWNER) == {
        "sourcing": {"name": "proxmox", "token_secret": "proxmox-token"}
    }
    assert RawDefaultedProvider.model_validate(filled_defaults(RawDefaultedProvider, {}, OWNER)) is not None


def test_a_default_that_needs_no_fill_is_left_absent() -> None:
    """The other half of the materialization rule: an instance default is
    complete by construction (building it required every field), so the
    blob stays exactly what the operator wrote and pydantic applies the
    default itself."""
    blob: dict[str, object] = {}
    assert filled_defaults(DefaultedUnionSite, blob, OWNER) is blob


def test_the_shorthand_is_folded_before_the_fill() -> None:
    """The fill acts on a mapping, so a bare-scalar shorthand is folded
    first, exactly as the model's own before-validator folds it: folded
    after, an owner-templated field would resolve for the operator who
    wrote the table form and silently not for the one who wrote the
    scalar, which is the same value spelled two ways."""
    assert filled_defaults(ShorthandTemplatedLike, "a value", OWNER) == {
        "value": "a value",
        "token": "shorthand-prod",
    }


def test_a_shorthand_with_nothing_to_fill_stays_a_scalar() -> None:
    """The fold is kept only when something inside it fills; otherwise
    the operator's scalar comes back untouched (by identity, being a
    ``str``) and the validator folds it itself."""
    assert filled_defaults(ShorthandLike, "plain", OWNER) == "plain"


def test_an_untagged_unions_block_arm_is_filled_when_a_table_addresses_it() -> None:
    """The same ``table_addresses_block`` decision extraction makes, so
    the fill reaches exactly the blocks the walkers reach. ``CredsLike``
    carries no template, so the identity answer doubles as the check that
    the arm was actually walked without being rewritten."""
    blob = {"mapping": {"secret": "named"}}
    assert filled_defaults(ScalarOrBlockLike, blob, OWNER) is blob


def test_collections_are_filled_per_element() -> None:
    class Entry(AgwModel):
        name: Literal["proxmox"]
        token: Annotated[str, SecretRef(usage="a token", default_template="tok-{owner_name}")]

    class Holder(AgwModel):
        entries: dict[str, Entry] = Field(default_factory=dict)
        entry_list: list[Entry] = Field(default_factory=list)

    blob = {
        "entries": {"a": {"name": "proxmox"}, "b": {"name": "proxmox", "token": "own"}},
        "entry_list": [{"name": "proxmox"}],
    }
    assert filled_defaults(Holder, blob, OWNER) == {
        "entries": {"a": {"name": "proxmox", "token": "tok-prod"}, "b": {"name": "proxmox", "token": "own"}},
        "entry_list": [{"name": "proxmox", "token": "tok-prod"}],
    }


def test_a_tag_naming_no_arm_fills_nothing() -> None:
    """Validation owns the refusal vocabulary for a bad tag; the fill
    passes the block through untouched rather than guessing an arm."""
    blob = {"platform": {"name": "no-such-arm"}}
    assert filled_defaults(SiteLike, blob, OWNER) is blob


def test_a_value_that_is_not_the_models_shape_passes_through() -> None:
    """Totality's everyday face: the fill never raises and never rewrites
    what it cannot read, so validation refuses the original value in its
    own vocabulary."""
    for blob in (7, "a string", None, ["a", "list"], {"platform": "not-a-table"}):
        assert filled_defaults(SiteLike, blob, OWNER) is blob


@pytest.mark.parametrize("model_cls", ALL_FIXTURES, ids=lambda m: m.__name__)
@pytest.mark.parametrize(
    "blob",
    [None, 7, True, "a string", [], ["x", 8], {}, {"name": None}, {8: "non-string-key"}, {"platform": []}],
    ids=repr,
)
def test_the_fill_is_total_over_garbage(model_cls: type[AgwModel], blob: object) -> None:
    """Never raises, for any model and any input whatsoever: the fill
    runs inside the same graph-building paths extraction's totality
    protects, so a blob nobody can make sense of must pass through
    rather than sink the walk."""
    filled_defaults(model_cls, blob, OWNER)


def test_a_blob_reachable_from_itself_terminates() -> None:
    """The cycle guard extraction keys its walk on, for the one input
    that cannot terminate on its own: a YAML anchor can produce a blob
    that contains itself."""

    class Node(AgwModel):
        token: Annotated[str, SecretRef(usage="t", default_template="tok-{owner_name}")] | None = None
        child: Node | None = None

    blob: dict[str, object] = {}
    blob["child"] = blob
    filled = filled_defaults(Node, blob, OWNER)
    assert filled["token"] == "tok-prod"  # type: ignore[index]


def test_the_template_outranks_a_declared_default() -> None:
    """The precedence the before-validator used to enforce by running
    before pydantic could reach for the default."""

    class Both(AgwModel):
        secret: Annotated[str, SecretRef(usage="s", default_template="tpl-{owner_name}")] = "declared-default"

    assert filled_defaults(Both, {}, OWNER) == {"secret": "tpl-prod"}


def test_a_shorthand_declared_beside_a_tagged_union_still_fills_the_arm() -> None:
    """The fold and the descent compose in one pass."""

    class Arm(AgwModel):
        name: Literal["proxmox"]
        token: Annotated[str, SecretRef(usage="t", default_template="tok-{owner_name}")]

    class Shorted(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="label")

        label: str | None = None
        platform: Annotated[Arm, Discriminator("name")] | None = None

    blob = {"label": "written", "platform": {"name": "proxmox"}}
    assert filled_defaults(Shorted, blob, OWNER) == {
        "label": "written",
        "platform": {"name": "proxmox", "token": "tok-prod"},
    }
