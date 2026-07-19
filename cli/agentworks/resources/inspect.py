"""Service-layer introspection and rendering for ``agw resource`` commands.

``list_resources`` / ``render_resource_table`` back ``agw resource list``;
``describe_resource`` / ``render_resource_description`` back
``agw resource describe KIND/NAME``.

The cross-kind shape **stops at framework-uniform fields**: kind, name,
origin (variant + sub-fields), usage list, description. Kind-specific
detail (secret backend mappings, template inheritance chains, resolved
field lookups) lives in the per-kind commands (``agw secret describe``,
etc.); rendering it here would require semantic knowledge the cross-kind
command intentionally doesn't carry.

Description is reliably populated across kinds because the framework
fills it generally: operator-declared resources carry the operator's
text (when their Resource type has a ``description`` field), and
auto-declared resources get a framework-synthesized
``"(auto) <usage> for <kind>/<name>"`` / ``"(auto) auto-declared default
<kind>"``. Kinds whose Resource type has no ``description`` field
render an empty cell, the accepted cost of the cross-kind view.

The framework reads ``origin`` / ``description`` / ``usage`` off each
Resource via ``getattr`` rather than a shared ``Resource`` base class:
kind types share these fields by convention (every kind today declares
all three), but the kinds are deliberately free-form so a
future kind can omit a field without breaking the registry. ``getattr``
with a default keeps the cross-kind walk safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks import output
from agentworks.resources.render import format_file_path, format_origin_line

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from agentworks.resources import Registry
    from agentworks.resources.kind import InstanceRef
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry


OriginFilter = Literal["operator", "auto", "builtin"]


@dataclass(frozen=True)
class ResourceSummary:
    """One row in ``agw resource list``: the framework-uniform fields
    for one Registry-published Resource.

    - ``reference_count`` is the number of inbound ``ReferenceEntry``
      instances on the Resource (how many config points name it). The
      list view renders this as the REFS column.
    - ``used_by_count`` is the number of live DB instances that depend
      on this Resource per the current config, computed via the kind's
      ``instances(db, registry, resource)`` hook. ``None`` for kinds
      with no instance concept (catalog, providers, backends); the
      list view renders ``None`` as ``-`` in the USED BY column.
    - ``disabled_reason`` is the kind's generic disabled hook's answer
      (``None`` = enabled, or the kind has no disabled concept). The
      list view marks disabled rows; describe shows the reason.
    """

    kind: str
    name: str
    origin: Origin | None
    reference_count: int
    used_by_count: int | None
    description: str
    disabled_reason: str | None = None


@dataclass(frozen=True)
class ResourceListing:
    """Full table for ``agw resource list``."""

    rows: tuple[ResourceSummary, ...]
    operator_count: int
    auto_count: int
    code_count: int


@dataclass(frozen=True)
class ResourceDescription:
    """Per-resource detail view for ``agw resource describe``.

    - ``references`` lists the inbound ``ReferenceEntry`` instances --
      config points that name this Resource. Rendered as the
      "Referenced by:" section.
    - ``used_by`` lists the live DB instances that depend on this
      Resource per the current config, projected via the kind's
      ``instances`` hook. ``None`` for kinds with no instance concept;
      rendered as the "Used by:" section (with a "(per current config)"
      annotation when present).
    """

    kind: str
    name: str
    origin: Origin | None
    description: str
    references: tuple[ReferenceEntry, ...]
    used_by: tuple[InstanceRef, ...] | None
    disabled_reason: str | None = None


# -- Filter parsing ---------------------------------------------------------

# Origin filter accepts the short forms operators are most likely to
# type. Maps to ``Origin.variant`` strings. The keys are also the single
# source of truth for the valid ``origin_filter`` values -- ``OriginFilter``
# (the public Literal) and ``list_resources``'s argument validation both
# derive from this map.
_ORIGIN_FILTER_MAP: dict[str, str] = {
    "operator": "operator-declared",
    "auto": "auto-declared",
    "builtin": "built-in",
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
    db: Database | None = None,
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

    ``db`` is optional: when provided, each row's ``used_by_count`` is
    populated via the kind's ``instances`` hook. When ``None`` (e.g.
    tests that don't care about the dynamic dimension), every row's
    ``used_by_count`` stays ``None`` -- the list renderer shows ``-``.
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
            used_by_count = _count_used_by(db, registry, kind, resource)
            rows.append(
                ResourceSummary(
                    kind=kind,
                    name=name,
                    origin=origin,
                    reference_count=len(references),
                    used_by_count=used_by_count,
                    description=description,
                    disabled_reason=disabled_reason_for(registry, kind, resource),
                )
            )
            variant = origin.variant if origin is not None else None
            if variant == "operator-declared":
                operator_count += 1
            elif variant == "auto-declared":
                auto_count += 1
            elif variant == "built-in":
                code_count += 1

    return ResourceListing(
        rows=tuple(rows),
        operator_count=operator_count,
        auto_count=auto_count,
        code_count=code_count,
    )


def disabled_reason_for(
    registry: Registry, kind: str, resource: object
) -> str | None:
    """Project the kind's generic disabled hook: why ``resource``
    cannot run on this host, or ``None`` when it can (or the kind has
    no disabled concept). Same structural-duck-typing gate as
    ``used_by_for``: absent-on-class IS the "never disabled" signal.
    """
    from agentworks.resources import KIND_REGISTRY

    handler = KIND_REGISTRY.get(kind)
    if handler is None:
        return None
    method = getattr(handler, "disabled_reason", None)
    if method is None:
        return None
    reason = method(registry, resource)
    if reason is not None and not isinstance(reason, str):
        from agentworks.errors import StateError

        raise StateError(
            f"{kind}.disabled_reason returned {type(reason).__name__}, expected str | None"
        )
    return reason


def used_by_for(
    db: Database | None, registry: Registry, kind: str, resource: object
) -> tuple[InstanceRef, ...] | None:
    """Project ``(kind, resource) -> tuple[InstanceRef, ...] | None`` via
    the kind's ``instances`` hook. ``None`` for kinds that don't
    implement the hook (catalog, providers, backends) or when ``db``
    isn't available; callers treat ``None`` as ``-`` rather than ``0``
    to distinguish "kind has no instance concept" from "kind has zero
    instances right now."

    The ``instances`` method is intentionally NOT on the ``ResourceKind``
    Protocol; absent-on-class IS the "no instance concept" signal (see
    ``resources/kind.py``'s comment for the Liskov-based rationale).
    """
    if db is None:
        return None
    from agentworks.resources import KIND_REGISTRY

    handler = KIND_REGISTRY.get(kind)
    if handler is None:
        return None
    method = getattr(handler, "instances", None)
    if method is None:
        return None
    return tuple(method(db, registry, resource))


def _count_used_by(
    db: Database | None, registry: Registry, kind: str, resource: object
) -> int | None:
    """``len()`` variant of ``used_by_for`` used by the list-row builder.
    Returns ``None`` (renderer shows ``-``) when the kind has no
    instance concept; otherwise the count of live instances.
    """
    refs = used_by_for(db, registry, kind, resource)
    return None if refs is None else len(refs)


def describe_resource(
    registry: Registry,
    kind: str,
    name: str,
    db: Database | None = None,
) -> ResourceDescription:
    """Build a ``ResourceDescription`` for ``agw resource describe``.

    Raises ``NotFoundError`` if the kind isn't registered or the name
    isn't in the registry. Service-layer-typed so CLI / future
    API surfaces render uniformly (project's
    service-layer-is-the-authority rule).

    ``db`` is optional: when provided, the ``used_by`` field is
    populated via the kind's ``instances`` hook. When ``None``,
    ``used_by`` stays ``None`` and the describe view omits the
    "Used by:" section.
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
        used_by=used_by_for(db, registry, kind, resource),
        disabled_reason=disabled_reason_for(registry, kind, resource),
    )


@dataclass(frozen=True)
class KindRow:
    """One row of ``agw resource kinds``: the per-kind metadata that is
    constant across every resource of the kind (which is why it renders
    here and not as a per-row column in ``resource list``)."""

    kind: str
    category: str
    resources: int
    description: str


def list_kinds(registry: Registry) -> list[KindRow]:
    """Every kind the app defines, sorted by name, with current registry
    row counts. Kinds are baked into the app -- plugins publish
    resources of existing kinds (declarable and capability alike),
    never new kinds -- so this is a read-only, code-defined
    inventory."""
    from agentworks.resources import KIND_REGISTRY

    return [
        KindRow(
            kind=name,
            category=handler.category,
            resources=sum(1 for _ in registry.iter_kind(name)),
            description=handler.description,
        )
        for name, handler in sorted(KIND_REGISTRY.items())
    ]


def render_kind_table(rows: list[KindRow]) -> None:
    kind_w = max(len("KIND"), *(len(r.kind) for r in rows))
    cat_w = max(len("CATEGORY"), *(len(r.category) for r in rows))
    res_w = len("RESOURCES")
    output.info(
        f"{'KIND':<{kind_w}}  {'CATEGORY':<{cat_w}}  {'RESOURCES':<{res_w}}  DESCRIPTION"
    )
    for r in rows:
        output.info(
            f"{r.kind:<{kind_w}}  {r.category:<{cat_w}}  {r.resources:<{res_w}}  {r.description}"
        )


def edit_location(registry: Registry, kind: str, name: str) -> tuple[Path, int]:
    """Resolve ``agw resource edit KIND/NAME`` to the manifest to open.

    Only operator-declared YAML manifests are editable through this
    command. The other origins error with the right next step
    (maintainer ruling, 2026-07-05, keep-it-simple scope):

    - operator-declared in TOML: point at ``agw resource migrate`` or
      ``agw config edit`` rather than opening config.toml here.
    - built-in: not on disk in editable form.
    - auto-declared: nothing on disk at all.

    Reuses ``describe_resource``'s validated lookup so unknown kinds and
    names error identically across the resource group.
    """
    from agentworks.errors import ValidationError
    from agentworks.resources import KIND_REGISTRY

    desc = describe_resource(registry, kind, name)
    origin = desc.origin
    if origin is None or origin.variant != "operator-declared":
        variant = origin.variant if origin is not None else "unknown-origin"
        # Capability kinds have no declarable form; a sample pointer
        # would send the operator to an error.
        handler = KIND_REGISTRY.get(kind)
        declarable = handler is not None and handler.category == "declarable"
        sample_hint = f"`agw resource sample {kind} --write {kind}s.yaml`."
        if variant == "built-in":
            raise ValidationError(
                f"{kind}/{name} is built-in; there is no file to edit",
                hint=(
                    f"Declare an operator resource instead: {sample_hint}"
                    if declarable
                    else f"{kind} is a capability provided by the app; "
                    f"there is nothing to declare or edit."
                ),
            )
        raise ValidationError(
            f"{kind}/{name} is {variant}; there is no file to edit",
            hint=f"Declare it explicitly first: {sample_hint}",
        )
    assert origin.file is not None and origin.line is not None  # variant contract
    if origin.file.suffix == ".toml":
        raise ValidationError(
            f"{kind}/{name} is declared in TOML "
            f"({format_file_path(origin.file)}:{origin.line})",
            hint=(
                f"Move it to a YAML manifest with `agw resource migrate "
                f"{kind}/{name}`, or edit the config directly with "
                f"`agw config edit`."
            ),
        )
    return origin.file, origin.line


# ``_collect_used_by`` previously duplicated ``used_by_for``'s guard
# structure with a near-identical body. Both call sites now go through
# ``used_by_for`` (the describe builder calls it directly; the list
# builder wraps it in ``_count_used_by``).


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
        parts.append(f"{listing.code_count} built-in")
    breakdown = f" ({', '.join(parts)})" if parts else ""
    output.info(f"{total} resource{'s' if total != 1 else ''}{breakdown}")
    output.info("")

    headers = ("KIND", "NAME", "ORIGIN", "REFS", "USED BY", "DESCRIPTION")
    rendered: list[tuple[str, ...]] = []
    for row in listing.rows:
        # ``used_by_count`` is None for kinds with no instance concept
        # (catalog, providers, backends); render as ``-`` to distinguish
        # "no instance concept" from "zero instances right now."
        used_by_cell = "-" if row.used_by_count is None else str(row.used_by_count)
        # Disabled rows are marked in the DESCRIPTION cell, never the
        # NAME cell: the rendered name must stay the exact selector an
        # operator copies into `agw resource describe KIND/NAME`.
        # `describe` carries the full reason.
        description_cell = (
            row.description
            if row.disabled_reason is None
            else f"(disabled) {row.description}".rstrip()
        )
        rendered.append(
            (
                row.kind,
                row.name,
                format_origin_line(row.origin),
                str(row.reference_count),
                used_by_cell,
                description_cell,
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
    output.info(f"Resource: {desc.kind}/{desc.name}")
    if desc.description:
        output.detail(f"Description: {desc.description}")
    else:
        output.detail("Description: (none)")
    output.detail(f"Origin: {format_origin_line(desc.origin)}")
    if desc.disabled_reason is not None:
        output.detail(f"Disabled: {desc.disabled_reason}")

    output.info("")
    output.info("Referenced by:")
    if not desc.references:
        output.detail("(none recorded)")
    else:
        # Dedupe by (source, usage) preserving first-encounter order --
        # same dedupe as agw secret describe.
        seen: set[tuple[tuple[str, str], str]] = set()
        for entry in desc.references:
            key = (entry.source, entry.usage)
            if key in seen:
                continue
            seen.add(key)
            src = f"{entry.source[0]}/{entry.source[1]}"
            output.detail(f"- {src} -- {entry.usage}")

    if desc.used_by is not None:
        output.info("")
        output.info("Used by (per current config):")
        if not desc.used_by:
            output.detail("(no live instances)")
        else:
            # Group by instance_kind for readability; preserve
            # first-encounter order within a kind.
            grouped: dict[str, list[str]] = {}
            for ref in desc.used_by:
                grouped.setdefault(ref.instance_kind, []).append(ref.instance_name)
            for instance_kind in grouped:
                for instance_name in grouped[instance_kind]:
                    output.detail(f"- {instance_kind}/{instance_name}")
