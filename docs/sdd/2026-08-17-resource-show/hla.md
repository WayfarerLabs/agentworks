# Resource Show: High-Level Architecture

- Status: Reopened for final review cleanup
- Date: 2026-08-17
- Implements: `frd.md`
- Code basis: `origin/main` at `217930fd`

## Summary

`resource show` composes the existing bulk facts into one focused record. The CLI parses a shared
resource identity, loads one ordinary finalized request registry, and calls one bounded show
service. That service composes the selected row's list-compatible summary, direct graph/live-usage
slice, resource-attributable doctor checks, and normalized declaration, then returns a closed result
for human or JSON projection.

The architectural distinction from the deleted generic card is no longer “contains no overlapping
facts.” It is a precise composition boundary. The old card was an ad hoc presentation service. The
new `show` is the complete focused projection of existing authorities: list summary, graph edges,
doctor checks, manifest model, and shared identity/origin services. It performs no unbounded
traversal and invents no second meaning for a shared fact.

## Current anchors

- `resources.access.parse_resource_identity` and `resolve_resource` own selector syntax and typed
  registry lookup.
- `resources.inspect.ResourceSummary`, `not_ready_reason_for`, and `used_by_for` own the facts
  behind each `resource list` row.
- `DependencyGraph.edges_of` and `incoming_edges_of`, plus graph-query identity/edge ordering, own
  direct declared relationships.
- `DatabaseLiveSource` owns a lazy, read-only, snapshot-scoped live-instance projection.
- `doctor.HealthCheck` and the existing per-row doctor routines own structured status, message, and
  remediation facts.
- `DeclaredResource`, `METADATA_FIELDS`, and `EnvelopeMetadata` define a loaded declarable row's
  normalized envelope.
- `machine_output.py`, `resources.render`, and terminal sanitization own closed JSON, origin
  spelling, and safe human output.

## Architectural decisions

### A1. Extract one shared list-row summary builder

`resources.inspect` gains a focused summary function conceptually equivalent to:

```text
summarize_resource(registry, identity, live_usage) -> ResourceSummary
```

`list_resources` calls that builder for each surviving row. `resource show` calls it once after its
live-usage slice is available. Kind, name, origin, reference count, used-by count, description,
not-ready reason, and disabled state therefore have one producer and retain the exact
`resource.list` meaning.

The summary builder receives the already-projected live-usage result rather than opening a database.
This keeps inventory iteration and focused lifecycle policy outside the pure row projection. A
supported kind with no current instances supplies an empty tuple and count zero; an unsupported kind
supplies null and preserves list's “no instance concept” distinction.

The ordinary request registry is finalized with host readiness probing enabled, exactly as
`resource list` and `doctor` do. `show` resolves the row before asking the finalized graph for
state; an absent graph node is an internal invariant failure, not a second readiness fallback.

### A2. Add a direct focused graph slice beside the traversal service

`resources.graph_query` adds a closed direct-slice record:

```text
FocusedGraphFacts(
    dependencies: tuple[GraphEdge, ...],
    dependents: tuple[GraphEdge, ...],
    used_by: tuple[InstanceRef, ...] | null,
)
```

The service validates the focus, reads `edges_of` and `incoming_edges_of` exactly once, converts
every direct declared reference through the same `GraphEdge` constructor used by `show_graph`, and
orders it with the existing canonical edge key. Direct projection does not filter through graph
traversal policy: a future relationship may be deliberately non-traversable and still remains a
direct fact about the selected row. The service enforces that every dependency starts at the focus,
every dependent ends at it, and every live instance uses it. It asks `DatabaseLiveSource` for
instances only when the selected kind implements the existing `instances` hook. The live source
stays unopened for an unsupported kind and treats an absent database as an empty supported result,
matching graph query semantics.

This is not `show_graph(..., depth=1)` followed by filtering. That traversal intentionally includes
the induced graph among reached neighbors; a focused resource record must contain only edges that
touch the selected row. `show_graph` reuses the extracted declared-edge and ordering helpers, so the
two projections cannot disagree about identity, direction, relationship, usage, or declarer.

### A3. Make resource-attributable doctor checks reusable

`doctor.py` keeps the complete fleet-wide orchestration and adds a narrow service:

```text
checks_for_resource(config, registry, identity) -> tuple[HealthCheck, ...]
```

The service dispatches only to the per-row checks doctor already performs. Small fact builders are
extracted from the existing bulk loops, and both the bulk group and focused service consume the same
`HealthCheck` objects:

- enabled VM platform and secret backend: the stored readiness row; disabled rows have no doctor row
  and therefore no focused diagnostic;
- VM site: stored readiness or the existing per-site preflight result and platform summary;
- secret source: backend, participation, provenance, enablement, and readiness;
- secret: the existing value-free resolution preview; and
- `admin-template/default` only: the existing dotfiles-source check when its source is non-empty.

Cross-row doctor facts stay in the bulk orchestrator: default-site warnings, VM rows that name a
missing/not-ready site, plugin roster, config/manifest health, state schema/contents, install tools,
tailscale, Python, system slug, and completions. `show` already exposes direct dependents and live
usage, so it does not duplicate consumer warnings as if they were the selected row's own check.

The focused service never runs the complete doctor report and never filters rendered names or prose.
It preserves doctor's existing safety: VM-site preflight is the same read-only local preflight
doctor already invokes; secret inspection uses `preview_resolution`; no secret resolves, no prompt
occurs, no authenticated runup runs, and no remote provider mutation is introduced.

### A4. Compose one closed presentation-free record

The focused module carries the graph's existing `Readiness` record and adds the shared fact records
directly:

```text
ResourceShow(
    summary: ResourceSummary,
    category,
    enablement,
    readiness | null,
    declaration,
    relationships: FocusedRelationships,
    used_by: tuple[InstanceRef, ...] | null,
    diagnostics: tuple[HealthCheck, ...],
)
```

`show_resource(config, registry, identity, live_source)` is the complete bounded composition entry
point. It performs the shared validated lookup, obtains its focused graph/live facts, list summary,
and attributable doctor checks, and assembles one immutable result. Every producer is keyed from the
same validated identity; structural parity tests, rather than same-call runtime re-assertions, pin
the relationships between compact and detailed facts. Database lifecycle and diagnostic dispatch are
service-layer work rather than CLI orchestration, so a future non-CLI caller cannot accidentally
build a partial card. The returned result retains no registry, database, handler, config, provider,
or capability implementation object.

The structural `enablement` and `readiness` fields deliberately coexist with list-compatible
`disabled` and `not_ready_reason`. The latter are the stable compact inventory projection; the
former preserve the complete state axes. Tests pin their reconciliation rather than allowing either
surface to drift.

### A5. Preserve normalized declaration projection

For a declarable kind, the row must implement `DeclaredResource`. The projector calls
`model_dump(mode="json", include=declared_field_names, exclude_none=True)` and partitions the result
using the shared base models:

- metadata keys follow `EnvelopeMetadata.model_fields` order;
- spec keys follow concrete Pydantic field order; and
- the envelope is `apiVersion`, `kind`, `metadata`, then `spec`.

The include set is the concrete model's fields minus one exported framework-field set derived beside
`METADATA_FIELDS` and shared with manifest decode. Defaults are included because this is the loaded
row; nulls are omitted. Pydantic JSON mode is the recursive conversion authority, and the closed
JSON carrier rejects an unexpected object instead of converting it to text. That guard remains
load-bearing because plugin-authored manifest rows feed dynamically typed Pydantic JSON-mode output
into the closed finite machine carrier. Declarable model classes and kind definitions remain
core-owned.

For a capability kind, declaration is null without reflecting over implementation code. A category
and row mismatch is an internal invariant failure. Source comments/order, omitted-versus-defaulted
syntax, and effective inheritance merging remain outside this projection.

### A6. Project one stable superset in JSON and human forms

The `resource.show` JSON `resource` object begins with the exact list-row keys and adds the richer
fields:

```json
{
  "resource": {
    "kind": "vm-site",
    "name": "local",
    "origin": {
      "variant": "operator-declared",
      "file": "/home/operator/.config/agentworks/resources/sites.yaml",
      "line": 7,
      "source": null,
      "source_resource": null,
      "plugin": null
    },
    "reference_count": 0,
    "used_by_count": 1,
    "description": "Local development site",
    "not_ready_reason": null,
    "disabled": false,
    "category": "declarable",
    "enablement": "enabled",
    "readiness": {
      "is_ready": true,
      "is_available": true,
      "reason": null
    },
    "relationships": {
      "dependencies": [
        {
          "edge_type": "declared",
          "source": { "node_type": "resource", "kind": "vm-site", "name": "local" },
          "target": { "node_type": "resource", "kind": "vm-platform", "name": "lima" },
          "relationship": "uses",
          "usage": "the VM platform",
          "declared_by": null
        }
      ],
      "dependents": []
    },
    "used_by": [{ "kind": "vm", "name": "agent" }],
    "diagnostics": [
      {
        "name": "local",
        "status": "ok",
        "message": "platform lima (placement: local)",
        "hint": null
      }
    ],
    "declaration": {
      "apiVersion": "agentworks/v1",
      "kind": "vm-site",
      "metadata": {
        "name": "local",
        "description": "Local development site"
      },
      "spec": {
        "platform": {
          "name": "lima",
          "placement": { "mode": "local" }
        }
      }
    }
  }
}
```

Origin, relationship, instance, and health-check projections reuse their existing machine helpers.
The human renderer emits the same facts as concise sections. Repeated authored relationship display
lines may be deduplicated for readability, but JSON retains the authoritative edges.

Every interpolated fact-line scalar in both resource and graph human output passes through one
shared line-safe filter that removes terminal controls, format/surrogate categories, and Unicode
line/paragraph separators. Declaration YAML relies on ASCII-only safe encoding, remains parseable,
and round-trips without a redundant post-encoding sanitizer. JSON assembly and encoding remain
atomic.

### A7. Keep CLI lifecycle conventional

The command parses identity before config work, then loads config and the ordinary request registry.
Human mode passes the same warning flags as `resource list`; JSON suppresses ambient warnings. The
CLI constructs `DatabaseLiveSource(db.DB_PATH)`, passes it with config, registry, and identity to
the single `show_resource` service, then chooses a renderer. It does not coordinate graph reads,
diagnostic dispatch, database entry/exit, or record assembly. All selected-resource fact assembly
completes before human fact output starts; ordinary human loader warnings may already have been
emitted by the shared loader.

The argument remains `ref` and maps to the existing `resource_refs` completion source. The generic
bash, zsh, and PowerShell generators require no algorithm change.

### A8. Prove parity and safety structurally

Tests compare a selected `ResourceShow`'s first eight fields with the matching `ResourceSummary`
from a real `list_resources` call. Direct-graph tests cover both directions, duplicates, inherited
declarer provenance, canonical order, no induced neighbor edge, supported-empty/unsupported/absent
live usage, snapshot lifecycle, and no traversal beyond the focused node.

Doctor tests compare focused checks with the exact `HealthCheck` objects inserted into bulk groups
for each attributed kind. They pin that disabled VM platforms/backends produce no check and that
dotfiles is attributed only to `admin-template/default` with a non-empty source, including the
empty-string no-check case. They also prove unrelated global/cross-row checks are absent and that
secret resolution, authenticated runup, provider mutation, and prompting are never reached.

Existing declaration, category, disabled/readiness, JSON, completion, typed-error, and terminal
safety coverage remains. CLI tests assert human warning flags are true, JSON warning flags false,
machine output is atomic, and the state database is opened only through the read-only live source.

## Rejected alternatives

- **Avoid overlap by keeping only declaration:** rejected by the operator. Bulk inventory and health
  scans are not substitutes for complete focused inspection.
- **Restore `resource describe`:** rejected. The durable factual verb is `show`, with no alias or
  compatibility runway.
- **Run all of doctor and filter its output:** rejected because labels/prose are not resource
  identities and unrelated checks would perform unnecessary work.
- **Call `show_graph(depth=1)` and trim it:** rejected because its induced-neighborhood semantics
  are intentionally broader than direct focused edges.
- **Add per-kind card renderers:** rejected because shared structured summary, graph, health, and
  declaration facts already compose the complete generic view.
- **Reflect capability configuration or preserve source-exact YAML:** rejected because capability
  explain and source manifests remain the respective authorities.
