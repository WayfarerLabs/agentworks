"""Service-layer introspection and rendering for ``agw resource`` commands.

``list_resources`` / ``render_resource_table`` back ``agw resource list``;
``describe_resource`` / ``render_resource_description`` back
``agw resource describe <kind> <name>`` (FRD R12 / Phase 2c).

The cross-kind shape **stops at framework-uniform fields**: kind, name,
origin (variant + sub-fields), usage list, description. Kind-specific
detail (secret backend mappings, template inheritance chains, resolved
field lookups) lives in the per-kind commands (``agw secret describe``,
etc.); rendering it here would require semantic knowledge the cross-kind
command intentionally doesn't carry.

Description is reliably populated across kinds thanks to Phase 2a's
generalized polish: operator-declared resources carry the operator's
text (when their Resource type has a ``description`` field), and
auto-declared resources get a framework-synthesized
``"(auto) <usage> for <kind>:<name>"`` / ``"(auto) auto-declared default
<kind>"``. Kinds whose Resource type has no ``description`` field
render an empty cell -- that's the cross-kind cost the SDD accepts.

The framework reads ``origin`` / ``description`` / ``usage`` off each
Resource via ``getattr`` rather than a shared ``Resource`` base class:
kind types share these fields by convention (every kind today declares
all three), but Phase 2a deliberately kept the kinds free-form so a
future kind can omit a field without breaking the registry. ``getattr``
with a default keeps the cross-kind walk safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks import output
from agentworks.resources.render import format_origin_line

if TYPE_CHECKING:
    from agentworks.resources import Registry
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry


OriginFilter = Literal["operator", "auto", "code"]


@dataclass(frozen=True)
class ResourceSummary:
    """One row in ``agw resource list``: the framework-uniform fields
    for one Registry-published Resource.

    ``reference_count`` is the number of inbound ``ReferenceEntry``
    instances on the Resource (how many config points name it). The list
    view renders this as the REFS column.
    """

    kind: str
    name: str
    origin: Origin | None
    reference_count: int
    description: str


@dataclass(frozen=True)
class ResourceListing:
    """Full table for ``agw resource list``."""

    rows: tuple[ResourceSummary, ...]
    operator_count: int
    auto_count: int
    code_count: int


@dataclass(frozen=True)
class ResourceDescription:
    """Per-resource detail view for ``agw resource describe``."""

    kind: str
    name: str
    origin: Origin | None
    description: str
    references: tuple[ReferenceEntry, ...]


# -- Filter parsing ---------------------------------------------------------

# Origin filter accepts the short forms operators are most likely to
# type. Maps to ``Origin.variant`` strings. The keys are also the single
# source of truth for the valid ``origin_filter`` values -- ``OriginFilter``
# (the public Literal) and ``list_resources``'s argument validation both
# derive from this map.
_ORIGIN_FILTER_MAP: dict[str, str] = {
    "operator": "operator-declared",
    "auto": "auto-declared",
    "code": "code-declared",
}


def _matches_origin(origin: Origin | None, origin_filter: OriginFilter | None) -> bool:
    if origin_filter is None:
        return True
    if origin is None:
        return False
    target_variant = _ORIGIN_FILTER_MAP[origin_filter]
    return origin.variant == target_variant


# -- Service layer ----------------------------------------------------------


def list_resources(
    registry: Registry,
    *,
    kinds: tuple[str, ...] | None = None,
    origin_filter: OriginFilter | None = None,
) -> ResourceListing:
    """Build a ``ResourceListing`` for ``agw resource list``.

    Filters narrow the rows; the summary counts are computed AFTER
    filtering so the header reflects what the operator actually sees.
    Raises ``ValidationError`` when ``origin_filter`` isn't one of
    ``operator`` / ``auto`` / ``code`` (the keys of ``_ORIGIN_FILTER_MAP``).
    The CLI layer stays thin per the service-layer-is-the-authority rule.
    """
    from agentworks.errors import ValidationError

    if origin_filter is not None and origin_filter not in _ORIGIN_FILTER_MAP:
        raise ValidationError(
            "origin_filter must be one of "
            f"{sorted(_ORIGIN_FILTER_MAP)}; got {origin_filter!r}",
            entity_kind="resource",
        )
    if kinds is not None and not kinds:
        raise ValidationError(
            "kinds= must contain at least one kind (or pass None for all)",
            entity_kind="resource",
        )

    target_kinds = tuple(kinds) if kinds else tuple(sorted(registry.iter_kinds()))

    rows: list[ResourceSummary] = []
    operator_count = 0
    auto_count = 0
    code_count = 0

    for kind in target_kinds:
        # Sort by name within each kind so the output is stable across
        # runs and easy to diff. Cross-kind ordering is alphabetized via
        # ``sorted(registry.iter_kinds())`` above.
        items = sorted(registry.iter_kind_items(kind), key=lambda item: item[0])
        for name, resource in items:
            origin = getattr(resource, "origin", None)
            if not _matches_origin(origin, origin_filter):
                continue
            references: tuple[ReferenceEntry, ...] = tuple(getattr(resource, "references", ()))
            description = getattr(resource, "description", "") or ""
            rows.append(
                ResourceSummary(
                    kind=kind,
                    name=name,
                    origin=origin,
                    reference_count=len(references),
                    description=description,
                )
            )
            variant = origin.variant if origin is not None else None
            if variant == "operator-declared":
                operator_count += 1
            elif variant == "auto-declared":
                auto_count += 1
            elif variant == "code-declared":
                code_count += 1

    return ResourceListing(
        rows=tuple(rows),
        operator_count=operator_count,
        auto_count=auto_count,
        code_count=code_count,
    )


def describe_resource(
    registry: Registry,
    kind: str,
    name: str,
) -> ResourceDescription:
    """Build a ``ResourceDescription`` for ``agw resource describe``.

    Raises ``NotFoundError`` if the kind isn't registered or the name
    isn't in the registry. Service-layer-typed so CLI / future
    API surfaces render uniformly (project's
    service-layer-is-the-authority rule).
    """
    from agentworks.errors import NotFoundError
    from agentworks.resources import KIND_REGISTRY

    if kind not in KIND_REGISTRY:
        known = sorted(KIND_REGISTRY.keys())
        raise NotFoundError(
            f"unknown kind {kind!r}",
            entity_kind="resource-kind",
            entity_name=kind,
            hint=f"known kinds: {', '.join(known)}",
        )

    try:
        resource = registry.lookup(kind, name)
    except KeyError:
        # Tailor the hint: if the kind has any published resources, point
        # at the scoped list. If it's empty (a known kind with no current
        # rows), tell the operator directly so they don't run a query
        # that returns "No resources match.".
        has_any = any(True for _ in registry.iter_kind_items(kind))
        if has_any:
            hint = f"check `agw resource list --kind {kind}` for available names"
        else:
            hint = f"no {kind} resources are currently published"
        raise NotFoundError(
            f"no {kind} named {name!r} in the registry",
            entity_kind=kind,
            entity_name=name,
            hint=hint,
        ) from None

    return ResourceDescription(
        kind=kind,
        name=name,
        origin=getattr(resource, "origin", None),
        description=getattr(resource, "description", "") or "",
        references=tuple(getattr(resource, "references", ())),
    )


# -- Renderers --------------------------------------------------------------


def render_resource_table(listing: ResourceListing) -> None:
    """Emit the listing as an operator-friendly table.

    Empty-state when no rows survive the filters: a clean
    ``No resources match.`` message. The header summary shows total +
    per-origin breakdown.
    """
    if not listing.rows:
        output.info("No resources match.")
        return

    total = len(listing.rows)
    parts: list[str] = []
    if listing.operator_count:
        parts.append(f"{listing.operator_count} operator-declared")
    if listing.auto_count:
        parts.append(f"{listing.auto_count} auto-declared")
    if listing.code_count:
        parts.append(f"{listing.code_count} code-declared")
    breakdown = f" ({', '.join(parts)})" if parts else ""
    output.info(f"{total} resource{'s' if total != 1 else ''}{breakdown}")
    output.info("")

    headers = ("KIND", "NAME", "ORIGIN", "REFS", "DESCRIPTION")
    rendered: list[tuple[str, ...]] = []
    for row in listing.rows:
        rendered.append(
            (
                row.kind,
                row.name,
                format_origin_line(row.origin),
                str(row.reference_count),
                row.description,
            )
        )
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rendered))
        for i in range(len(headers))
    ]

    def _fmt(cols: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    output.info(_fmt(headers))
    output.info(_fmt(tuple("-" * w for w in widths)))
    for r in rendered:
        output.info(_fmt(r))


def render_resource_description(desc: ResourceDescription) -> None:
    """Emit a ``ResourceDescription`` as operator-friendly sections:
    header (kind, name, description, origin), then the references list.
    Mirrors the shape of ``agw secret describe`` minus the
    secret-specific sections (backend mappings, resolution preview).
    """
    output.info(f"Resource: {desc.kind}:{desc.name}")
    if desc.description:
        output.detail(f"Description: {desc.description}")
    else:
        output.detail("Description: (none)")
    output.detail(f"Origin: {format_origin_line(desc.origin)}")

    output.info("")
    output.info("Referenced by:")
    if not desc.references:
        output.detail("(none recorded)")
        return
    # Dedupe by (source, usage) preserving first-encounter order --
    # same dedupe as agw secret describe (FRD R10).
    seen: set[tuple[tuple[str, str], str]] = set()
    for entry in desc.references:
        key = (entry.source, entry.usage)
        if key in seen:
            continue
        seen.add(key)
        src = f"{entry.source[0]}:{entry.source[1]}"
        output.detail(f"- {src} -- {entry.usage}")
