"""Extraction agrees with VALIDATION: no reference a validated value
carries is missing from the extracted edges.

The totality suite (``test_extract_totality.py``) proves the walker never
raises and never invents an edge. That is only half the contract. The
other half is the one whose failure is silent: a blob that validates
cleanly while a secret inside it never becomes a graph edge, so finalize
passes, the graph is short one node, and the operator finds out when
something tries to resolve it.

The oracle is the VALIDATED object, walked as a plain Python object graph:
``isinstance`` decides what is a block and what is a collection, and the
model is asked one question, through the package's public
:func:`~agentworks.schema.marker_of`, which is whether a field carries a
marker of its own. That is deliberate. The finding this suite exists for
was a TRAVERSAL bug (the walk stopped at the first repeated model type,
so finite data below that lost its edges), so the oracle's traversal has
to be its own.

Markers on a COLLECTION's elements are deliberately outside the oracle:
reading them would mean reaching into the package's private classifier,
and the assertion is one-directional anyway, so leaving them out costs
coverage of a case the walk suite already pins and buys an oracle with no
shared machinery worth speaking of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from agentworks.schema import RefOwner, extract_references, marker_of, validation_context

from ._fixture_models import (
    AzureLike,
    CatalogLike,
    DiamondLike,
    FieldTaggedCollectionSite,
    GithubLike,
    MappingRoot,
    RenamedArmSite,
    SelfReferential,
    SiteLike,
    TaggedCollectionSite,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

OWNER = RefOwner(kind="vm-site", name="lab")


def _referenced_names(value: object) -> set[str]:
    """Every Resource name the VALIDATED ``value`` carries on a marked
    field of its own or of a block below it.

    Structure comes from the objects themselves: a nested block is a block
    because it IS a model, and an element is an element because the thing
    holding it is a list or a table.
    """
    return set(_walk(value))


def _walk(value: object) -> Iterator[str]:
    if isinstance(value, BaseModel):
        for name, field in type(value).model_fields.items():
            held = getattr(value, name, None)
            if marker_of(field) is not None:
                if isinstance(held, str) and held:
                    yield held
            else:
                yield from _walk(held)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _walk(item)


def _nested(depth: int) -> dict[str, object]:
    """A ``SelfReferential`` blob nesting ``depth`` levels, every level
    naming a secret of its own and every level finite."""
    blob: dict[str, object] = {"secret": f"level-{depth}"}
    for level in reversed(range(depth)):
        blob = {"secret": f"level-{level}", "child": blob}
    return blob


#: Blobs that VALIDATE, so every reference in them is one the framework
#: accepted and therefore one the graph has to carry.
_VALID = [
    pytest.param(GithubLike, {"token": "custom"}, id="marked-scalar"),
    pytest.param(
        AzureLike,
        {"region": "eastus", "service_principal": {"client_id": "c", "tenant_id": "t", "secret": "s"}},
        id="nested",
    ),
    pytest.param(
        DiamondLike,
        {"primary": {"secret": "one"}, "fallback": {"secret": "two"}},
        id="two-siblings-of-one-model",
    ),
    pytest.param(
        CatalogLike,
        {
            "accounts": [{"secret": "a"}, {"secret": "b"}],
            "accounts_by_name": {"prod": {"secret": "c"}},
            "extra_secrets": {"one": "d"},
            "templates": ("base",),
        },
        id="collections",
    ),
    pytest.param(SiteLike, {"platform": {"name": "proxmox", "token_secret": "lab"}}, id="tagged-union"),
    pytest.param(RenamedArmSite, {"platform": {"name": "ec2", "access_key_secret": "key"}}, id="renamed-arm"),
    pytest.param(MappingRoot, {"token": "rooted"}, id="root-model"),
    pytest.param(
        TaggedCollectionSite,
        {
            "platforms": [{"name": "proxmox", "token_secret": "in-a-list"}, {"name": "lima"}],
            "platforms_by_name": {"x": {"name": "proxmox", "token_secret": "in-a-table"}},
        },
        id="collection-of-tagged-blocks",
    ),
    pytest.param(
        FieldTaggedCollectionSite,
        {"platforms": [{"name": "proxmox", "token_secret": "field-tagged"}]},
        id="collection-of-tagged-blocks-other-spelling",
    ),
    *(pytest.param(SelfReferential, _nested(depth), id=f"recursive-model-depth-{depth}") for depth in range(6)),
]


@pytest.mark.parametrize(("model_cls", "blob"), _VALID)
def test_no_reference_a_validated_value_carries_is_missing_from_the_edges(
    model_cls: type[BaseModel],
    blob: object,
) -> None:
    validated = model_cls.model_validate(blob, context=validation_context(OWNER))
    expected = _referenced_names(validated)
    extracted = {ref.name for ref in extract_references(model_cls, blob, OWNER)}

    # Non-vacuity per case: an oracle that found nothing would make the
    # subset assertion below true for a walker that extracted nothing.
    assert expected, "this case carries no reference, so it proves nothing"
    assert expected <= extracted, f"{sorted(expected - extracted)} validated but produced no edge"


def test_the_oracle_sees_what_the_walker_would_miss() -> None:
    """Non-vacuity for the oracle itself.

    The suite is worth nothing if the oracle happens to walk the same way
    the walker does. Pin the one place they must differ: an oracle that
    stopped at the first repeated model type would report one name here,
    and the whole point is that it reports four.
    """
    validated = SelfReferential.model_validate(_nested(3), context=validation_context(OWNER))

    assert _referenced_names(validated) == {"level-0", "level-1", "level-2", "level-3"}
