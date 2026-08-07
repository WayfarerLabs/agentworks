"""Reading an emitted property the way a consumer of the schema must.

A property's marker is not always at its top level, and a reader that
assumed it was would silently report "no reference here" for a field that
declares one.

**A field's own marker IS on the property.** ``AgwModel`` puts it there
(``schema/base.py``, ``_ref_at_top_level``), whichever branch pydantic
emitted it into: the string arm of ``Annotated[str, SecretRef(...)] | None``,
or the constrained arm of a templated field the same hook widened.

**A COLLECTION's element marker is not**, and should not be: it describes
one element, so ``list[Annotated[str, ResourceRef(...)]]`` states it on
``items`` where the walkers read it, and lifting it onto the field would
claim the list itself names a Resource.

So: search the subtree. One rule for both, and it does not encode where
pydantic happens to put things.
"""

from __future__ import annotations

from typing import Any

from agentworks.schema import REF_SCHEMA_KEY


def ref_extension(prop: dict[str, Any]) -> dict[str, Any] | None:
    """The ``x-agw-ref`` object a property carries, wherever it sits, or
    ``None`` when the field declares no reference."""
    found: list[dict[str, Any]] = [node[REF_SCHEMA_KEY] for node in _nodes(prop) if REF_SCHEMA_KEY in node]
    if not found:
        return None
    first, *rest = found
    assert all(other == first for other in rest), f"one property carries conflicting markers: {found}"
    return first


def accepts_null(prop: dict[str, Any]) -> bool:
    """Whether a property permits ``null``, read the way a validator reads
    it rather than by pattern-matching our own output."""
    branches = prop.get("anyOf") or prop.get("oneOf") or ()
    return prop.get("type") == "null" or any(branch.get("type") == "null" for branch in branches)


def _nodes(node: object) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        return [node, *(child for value in node.values() for child in _nodes(value))]
    if isinstance(node, list):
        return [child for item in node for child in _nodes(item)]
    return []
