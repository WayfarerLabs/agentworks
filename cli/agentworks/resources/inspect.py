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
from agentworks.machine_output import (
    JsonObject,
    JsonValue,
    project_instance_references,
    project_origin,
    project_references,
)
from agentworks.resources.graph import Enablement
from agentworks.resources.render import format_origin_line, format_reference_entry

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from agentworks.origin import Origin
    from agentworks.resources import Registry
    from agentworks.resources.kind import InstanceRef
    from agentworks.resources.reference import ReferenceEntry


OriginFilter = Literal["operator", "auto", "builtin", "plugin"]


@dataclass(frozen=True)
class ResourceSummary:
    """One row in ``agw resource list``: the framework-uniform fields
    for one Registry-published Resource.

    - ``reference_count`` is the number of inbound ``ReferenceEntry``
      instances on the Resource's graph node (how many config points name
      it), from ``Registry.graph.dependents_of``. The list view renders
      this as the REFS column.
    - ``used_by_count`` is the number of live DB instances that depend
      on this Resource per the current config, computed via the kind's
      ``instances(db, registry, resource)`` hook. ``None`` for kinds
      with no instance concept (apt / install-commands, providers,
      backends); the list view renders ``None`` as ``-`` in the USED BY
      column.
    - ``not_ready_reason`` is the resource's stored readiness verdict's reason,
      read off the graph (``None`` = ready, or the kind has no readiness
      concept). The list view marks not-ready rows; describe shows the reason.
    - ``disabled`` is the row's opt-in axis (``enablement_of is disabled``).
      Only ever ``True`` on a row surfaced by ``list_resources(..., include_disabled=True)``,
      since disabled rows are otherwise skipped. The list view marks it with a
      ``(disabled)`` marker that dominates the ``(not ready)`` marker (a disabled
      row's readiness is a ready placeholder anyway).
    """

    kind: str
    name: str
    origin: Origin | None
    reference_count: int
    used_by_count: int | None
    description: str
    not_ready_reason: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class ResourceListing:
    """Full table for ``agw resource list``."""

    rows: tuple[ResourceSummary, ...]
    operator_count: int
    auto_count: int
    code_count: int
    plugin_count: int


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
    not_ready_reason: str | None = None
    disabled_reason: str | None = None
    """The text of the ``Disabled:`` line, or ``None`` when the resource is
    enabled. Derived from the row's ``system-plugin`` origin plus the opt-in
    axis (``enablement_of``), NOT from a per-node reason (the frozen graph node
    carries none; the disabling reason lives only on the transient
    ``DisabledMark``). ``describe`` renders the named row even when disabled and
    annotates it with this line, exactly as the doctor roster phrases the
    state."""


def resource_listing_data(listing: ResourceListing) -> JsonObject:
    """Project list facts into the closed ``resource.list`` JSON data shape."""
    return {
        "resources": [
            {
                "kind": row.kind,
                "name": row.name,
                "origin": project_origin(row.origin),
                "reference_count": row.reference_count,
                "used_by_count": row.used_by_count,
                "description": row.description,
                "not_ready_reason": row.not_ready_reason,
                "disabled": row.disabled,
            }
            for row in listing.rows
        ],
        "counts": {
            "operator_declared": listing.operator_count,
            "auto_declared": listing.auto_count,
            "built_in": listing.code_count,
            "system_plugin": listing.plugin_count,
        },
    }


def resource_description_data(description: ResourceDescription) -> JsonObject:
    """Project describe facts into the closed ``resource.describe`` JSON data shape."""
    references: list[JsonValue] = [reference for reference in project_references(description.references)]
    used_by: list[JsonValue] | None = (
        None
        if description.used_by is None
        else [reference for reference in project_instance_references(description.used_by)]
    )
    return {
        "resource": {
            "kind": description.kind,
            "name": description.name,
            "origin": project_origin(description.origin),
            "description": description.description,
            "references": references,
            "used_by": used_by,
            "not_ready_reason": description.not_ready_reason,
            "disabled_reason": description.disabled_reason,
        },
    }


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
    "plugin": "system-plugin",
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
    include_disabled: bool = False,
) -> ResourceListing:
    """Build a ``ResourceListing`` for ``agw resource list``.

    Filters narrow the rows; the summary counts are computed AFTER
    filtering so the header reflects what the operator actually sees.
    Raises ``ValidationError`` when ``origin_filter`` isn't one of the
    keys of ``_ORIGIN_FILTER_MAP`` (``operator`` / ``auto`` / ``builtin`` /
    ``plugin``), and ``NotFoundError`` when a requested kind is not registered.
    The CLI layer stays thin per the
    service-layer-is-the-authority rule.

    ``include_disabled`` sets the default-surface rule (the plugin work is the
    first producer of ``disabled`` rows): a row whose opt-in axis reads
    ``Enablement.disabled`` is hidden by default and revealed only when this is
    ``True``. The filter is on the ENABLEMENT axis (``enablement_of``), never on
    readiness: a host-unsupported built-in (present, enabled, blocked) still
    lists with its ``(not ready)`` marker. "Off by opt-in" hides; "on but
    blocked" shows.

    ``db`` is optional: when provided, each row's ``used_by_count`` is
    populated via the kind's ``instances`` hook. When ``None`` (e.g.
    tests that don't care about the dynamic dimension), every row's
    ``used_by_count`` stays ``None`` -- the list renderer shows ``-``.
    """
    from agentworks.errors import NotFoundError, ValidationError
    from agentworks.resources import KIND_REGISTRY

    if origin_filter is not None and origin_filter not in _ORIGIN_FILTER_MAP:
        raise ValidationError(
            f"origin_filter must be one of {sorted(_ORIGIN_FILTER_MAP)}; got {origin_filter!r}",
            entity_kind="resource",
        )
    if kinds is not None and not kinds:
        raise ValidationError(
            "kinds= must contain at least one kind (or pass None for all)",
            entity_kind="resource",
        )
    if kinds is not None:
        unknown_kinds = sorted(set(kinds).difference(KIND_REGISTRY))
        if unknown_kinds:
            unknown = unknown_kinds[0]
            raise NotFoundError(
                f"unknown kind {unknown!r}",
                entity_kind="resource-kind",
                entity_name=unknown,
                hint=f"known kinds: {', '.join(sorted(KIND_REGISTRY))}",
            )

    target_kinds = tuple(kinds) if kinds else tuple(sorted(registry.iter_kinds()))

    rows: list[ResourceSummary] = []
    operator_count = 0
    auto_count = 0
    code_count = 0
    plugin_count = 0

    for kind in target_kinds:
        # Sort by name within each kind so the output is stable across
        # runs and easy to diff. Cross-kind ordering is alphabetized via
        # ``sorted(registry.iter_kinds())`` above.
        items = sorted(registry.iter_kind_items(kind), key=lambda item: item[0])
        for name, resource in items:
            origin = getattr(resource, "origin", None)
            if not _matches_origin(origin, origin_filter):
                continue
            # Disabled hides, not-ready shows: skip a row that is off by
            # opt-in unless the operator asked for it. Reads the ENABLEMENT
            # axis, never readiness, so a not-ready-but-enabled row survives.
            disabled = registry.graph.enablement_of(kind, name) is Enablement.disabled
            if not include_disabled and disabled:
                continue
            references: tuple[ReferenceEntry, ...] = registry.graph.dependents_of(kind, name)
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
                    not_ready_reason=not_ready_reason_for(registry, kind, name),
                    disabled=disabled,
                )
            )
            variant = origin.variant if origin is not None else None
            if variant == "operator-declared":
                operator_count += 1
            elif variant == "auto-declared":
                auto_count += 1
            elif variant == "built-in":
                code_count += 1
            elif variant == "system-plugin":
                plugin_count += 1

    return ResourceListing(
        rows=tuple(rows),
        operator_count=operator_count,
        auto_count=auto_count,
        code_count=code_count,
        plugin_count=plugin_count,
    )


def not_ready_reason_for(registry: Registry, kind: str, name: str) -> str | None:
    """Project ``(kind, name)``'s stored readiness verdict: why it cannot run
    on this host, or ``None`` when it can (a kind with no readiness concept
    folds to ready, so its reason is ``None``).

    Reads the fold's verdict off the graph (``readiness_of``), the single
    unified read (R10/R11): no recompute, no per-kind readiness hook dispatch,
    no live-registry probe. Feeds the ``(not ready)`` list marker and the
    ``Not ready:`` describe line; "enabled/disabled" is reserved for the opt-in
    axis and never used for host readiness (R6/R9.1).
    """
    return registry.graph.readiness_of(kind, name).reason


def used_by_for(db: Database | None, registry: Registry, kind: str, resource: object) -> tuple[InstanceRef, ...] | None:
    """Project ``(kind, resource) -> tuple[InstanceRef, ...] | None`` via
    the kind's ``instances`` hook. ``None`` for kinds that don't
    implement the hook (apt / install-commands, providers, backends) or
    when ``db`` isn't available; callers treat ``None`` as ``-`` rather
    than ``0`` to distinguish "kind has no instance concept" from "kind
    has zero instances right now."

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


def _count_used_by(db: Database | None, registry: Registry, kind: str, resource: object) -> int | None:
    """``len()`` variant of ``used_by_for`` used by the list-row builder.
    Returns ``None`` (renderer shows ``-``) when the kind has no
    instance concept; otherwise the count of live instances.
    """
    refs = used_by_for(db, registry, kind, resource)
    return None if refs is None else len(refs)


def _plugin_provenance(origin: Origin | None) -> str | None:
    """``"from plugin <name>"`` for a ``system-plugin`` row, else ``None``.

    Reads ``origin.plugin`` already on the row (no separate lookup), so a
    plugin's contributed resources are attributable in the list DESCRIPTION
    cell and the describe header without the plugin being a resource (R12).
    """
    if origin is not None and origin.variant == "system-plugin" and origin.plugin:
        return f"from plugin {origin.plugin}"
    return None


def _disabled_line_text(origin: Origin | None) -> str:
    """The ``Disabled:`` line's text for a disabled row, derived from the row's
    origin plus config the same way the doctor roster derives its state phrase
    (from provenance, not a node reason): ``"not enabled in [plugins].system
    (plugin <name>)"`` for a ``system-plugin`` row. The exact wording differs from the
    roster's line because describe carries the plugin name inline while the
    roster row already labels its plugin.

    The disabling reason is NOT read off the frozen graph node (it carries
    none; the reason lives only on the transient ``DisabledMark``), so the text
    is reconstructed from provenance. A disabled row of any other origin (none
    exists today: the plugin opt-in source is the sole producer) renders the
    bare state word rather than a plugin phrase it cannot substantiate.
    """
    if origin is not None and origin.variant == "system-plugin" and origin.plugin:
        return f"not enabled in [plugins].system (plugin {origin.plugin})"
    return "disabled"


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
    from agentworks.resources.access import ResourceIdentity, resolve_resource

    resolved = resolve_resource(registry, ResourceIdentity(kind=kind, name=name))
    resource = resolved.resource
    origin = resolved.origin
    # describe is an EXPLICIT lookup by name, so it always renders the named row
    # even when disabled (an operator debugging a specific disabled resource
    # asked for it by name), annotating its state off the binary opt-in axis.
    disabled = registry.graph.enablement_of(kind, name) is Enablement.disabled
    return ResourceDescription(
        kind=kind,
        name=name,
        origin=origin,
        description=getattr(resource, "description", "") or "",
        references=registry.graph.dependents_of(kind, name),
        used_by=used_by_for(db, registry, kind, resource),
        not_ready_reason=not_ready_reason_for(registry, kind, name),
        disabled_reason=_disabled_line_text(origin) if disabled else None,
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


def resource_kinds_data(rows: list[KindRow]) -> JsonObject:
    """Project kind facts into the closed ``resource.kinds`` JSON data shape."""
    return {
        "kinds": [
            {
                "kind": row.kind,
                "category": row.category,
                "resource_count": row.resources,
                "description": row.description,
            }
            for row in rows
        ],
    }


def render_kind_table(rows: list[KindRow]) -> None:
    kind_w = max(len("KIND"), *(len(r.kind) for r in rows))
    cat_w = max(len("CATEGORY"), *(len(r.category) for r in rows))
    res_w = len("RESOURCES")
    output.info(f"{'KIND':<{kind_w}}  {'CATEGORY':<{cat_w}}  {'RESOURCES':<{res_w}}  DESCRIPTION")
    for r in rows:
        output.info(f"{r.kind:<{kind_w}}  {r.category:<{cat_w}}  {r.resources:<{res_w}}  {r.description}")


def edit_location(registry: Registry, kind: str, name: str) -> tuple[Path, int]:
    """Resolve ``agw resource edit KIND/NAME`` to the manifest to open.

    Every operator-declared resource is a YAML manifest (ADR 0022), so an
    operator-declared origin resolves straight to its file and line. The
    other origins error with the right next step (maintainer ruling,
    2026-07-05, keep-it-simple scope):

    - built-in: not on disk in editable form.
    - auto-declared: nothing on disk at all.

    Uses the shared validated resource resolver so unknown kinds and names
    retain the resource group's typed errors without constructing a card.
    """
    from agentworks.errors import ValidationError
    from agentworks.resources import KIND_REGISTRY
    from agentworks.resources.access import ResourceIdentity, resolve_resource

    resolved = resolve_resource(registry, ResourceIdentity(kind=kind, name=name))
    origin = resolved.origin
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
                    else f"{kind} is a capability provided by the app; there is nothing to declare or edit."
                ),
            )
        raise ValidationError(
            f"{kind}/{name} is {variant}; there is no file to edit",
            hint=f"Declare it explicitly first: {sample_hint}",
        )
    assert origin.file is not None and origin.line is not None  # variant contract
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
    if listing.plugin_count:
        parts.append(f"{listing.plugin_count} system-plugin")
    breakdown = f" ({', '.join(parts)})" if parts else ""
    output.info(f"{total} resource{'s' if total != 1 else ''}{breakdown}")
    output.info("")

    headers = ("KIND", "NAME", "ORIGIN", "REFS", "USED BY", "DESCRIPTION")
    rendered: list[tuple[str, ...]] = []
    for row in listing.rows:
        # ``used_by_count`` is None for kinds with no instance concept
        # (apt / install-commands, providers, backends); render as ``-``
        # to distinguish "no instance concept" from "zero instances
        # right now."
        used_by_cell = "-" if row.used_by_count is None else str(row.used_by_count)
        # Not-ready rows are marked in the DESCRIPTION cell, never the
        # NAME cell: the rendered name must stay the exact selector an
        # operator copies into `agw resource describe KIND/NAME`.
        # `describe` carries the full reason. A system-plugin row also
        # carries a `from plugin <name>` provenance annotation here.
        description_cell = row.description
        provenance = _plugin_provenance(row.origin)
        if provenance is not None:
            description_cell = f"{description_cell} ({provenance})" if description_cell else provenance
        # A disabled row (only ever surfaced under --include-disabled) is marked
        # with a ``(disabled)`` marker parallel to ``(not ready)``. Disabled
        # dominates: a disabled node folds to a ready-placeholder readiness, so
        # the ``(not ready)`` marker never coexists in practice, but if it ever
        # did, ``(disabled)`` is the honest signal (opt-in, not host support).
        if row.disabled:
            description_cell = f"(disabled) {description_cell}".rstrip()
        elif row.not_ready_reason is not None:
            description_cell = f"(not ready) {description_cell}".rstrip()
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
    widths = [max(len(headers[i]), *(len(r[i]) for r in rendered)) for i in range(len(headers))]

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
    # A system-plugin resource carries a `from plugin <name>` provenance
    # annotation in the header (read off the origin), so a plugin's contributed
    # resources are attributable without the plugin being a resource (R12).
    provenance = _plugin_provenance(desc.origin)
    if desc.description:
        description_line = f"{desc.description} ({provenance})" if provenance is not None else desc.description
        output.detail(f"Description: {description_line}")
    else:
        output.detail(f"Description: {provenance}" if provenance is not None else "Description: (none)")
    output.detail(f"Origin: {format_origin_line(desc.origin)}")
    if desc.disabled_reason is not None:
        output.detail(f"Disabled: {desc.disabled_reason}")
    if desc.not_ready_reason is not None:
        output.detail(f"Not ready: {desc.not_ready_reason}")

    output.info("")
    output.info("Referenced by:")
    if not desc.references:
        output.detail("(none recorded)")
    else:
        # Dedupe by (source, usage) preserving first-encounter order --
        # same dedupe as agw secret describe.
        seen: set[str] = set()
        for entry in desc.references:
            line = format_reference_entry(entry)
            if line in seen:
                continue
            seen.add(line)
            output.detail(f"- {line}")

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
