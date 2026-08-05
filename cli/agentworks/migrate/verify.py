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
            rows[(kind, name)] = strip_source_fields(resource)
    return rows


def first_difference(pre: dict[tuple[str, str], Any], post: dict[tuple[str, str], Any]) -> str | None:
    """First divergence of ``pre`` against ``post``, pre-keys-scoped, or None.

    ``pre`` is the migrated units only (the oracle over the original TOML
    plus the decoded original YAML, scoped to ``plan.units``); ``post`` is
    the full registry rebuilt from the rewritten config plus manifests. Each
    pre ``(kind, name)`` must be present and equal in post. The symmetric
    "added" branch is deliberately dropped: once ``pre`` is scoped to the
    selected units, ``post`` legitimately carries rows ``pre`` does not
    (built-ins, auto-declared, other manifests), so an "added" check would
    false-fail on every run. The narrow fabrication gap this opens (an
    emission inventing an extra row for a selected unit) is closed by the
    emitted-key-set guard in ``execute._verify``.
    """
    missing = sorted(set(pre) - set(post))
    if missing:
        kind, name = missing[0]
        return f"{kind}/{name}: present before migration, missing after"
    for key in sorted(pre):
        if pre[key] != post[key]:
            kind, name = key
            return f"{kind}/{name}: content differs after migration"
    return None


def strip_source_fields(resource: Any) -> Any:
    """Drop the source-dependent fields so TOML- and manifest-sourced
    rows compare equal. Shared with the decode-parity tests, which
    import it from here so the two normalizations cannot drift."""
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
