"""Reading an emitted property the way a consumer of the schema must.

Two facts about a property are not where a first guess puts them, and
both bit a test before they were centralized here.

**A marker rides the branch its annotation sits on, not the property.**
``Annotated[str, SecretRef(...)] | None`` emits

    {"anyOf": [{"type": "string", "x-agw-ref": {...}}, {"type": "null"}],
     "default": null, "title": "..."}

and that is pydantic's own doing, with no agentworks hook involved (it
puts the marker where the ``Annotated`` is). ``AgwModel`` widens an
owner-templated field to exactly the same shape, so a reader that looked
only at the property's top level would have been blind to the native case
too, and silently: it would report "no reference here" for a field that
declares one.

So: search the subtree. One rule for both shapes, and the rule holds for
any future one, because it does not encode where pydantic happens to put
things.
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
