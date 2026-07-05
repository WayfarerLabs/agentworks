"""Per-run registry-equivalence verification.

``build_registry`` is pure, so a migration is proven correct by
rebuilding from the rewritten TOML plus manifests and comparing against
the pre-migration registry. The comparison is KEYED by ``(kind, name)``
-- iteration order legitimately changes when rows move between
publishers, so an ordered comparison would false-positive on every
partial migration -- and rows are normalized for the source-dependent
fields (declaration location, origin, reference attribution locations),
the same normalization the decode-parity tests use.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentworks.resources.registry import Registry


def normalized_rows(registry: Registry) -> dict[tuple[str, str], Any]:
    """Every registry row, keyed by (kind, name), source-normalized."""
    from agentworks.resources import KIND_REGISTRY
    from agentworks.resources.access import kind_dict

    rows: dict[tuple[str, str], Any] = {}
    for kind in KIND_REGISTRY:
        for name, resource in kind_dict(registry, kind).items():
            rows[(kind, name)] = _strip(resource)
    return rows


def first_difference(
    pre: dict[tuple[str, str], Any], post: dict[tuple[str, str], Any]
) -> str | None:
    """Human-readable description of the first divergence, or None."""
    missing = sorted(set(pre) - set(post))
    if missing:
        kind, name = missing[0]
        return f"{kind}/{name}: present before migration, missing after"
    added = sorted(set(post) - set(pre))
    if added:
        kind, name = added[0]
        return f"{kind}/{name}: absent before migration, present after"
    for key in sorted(pre):
        if pre[key] != post[key]:
            kind, name = key
            return f"{kind}/{name}: content differs after migration"
    return None


def _strip(resource: Any) -> Any:
    """Drop the source-dependent fields so TOML- and manifest-sourced
    rows compare equal (mirrors the decode-parity tests' ``_strip``)."""
    if not dataclasses.is_dataclass(resource) or isinstance(resource, type):
        return resource
    kwargs: dict[str, Any] = {}
    for field in ("origin", "declared_at"):
        if hasattr(resource, field):
            kwargs[field] = None
    if hasattr(resource, "references"):
        kwargs["references"] = ()
    if not kwargs:
        return resource
    return dataclasses.replace(resource, **kwargs)
