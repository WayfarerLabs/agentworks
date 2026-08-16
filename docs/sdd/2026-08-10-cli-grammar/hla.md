# CLI Grammar Correction: High-Level Architecture

- Status: Approved by the operator for implementation design
- Date: 2026-08-15
- Implements: `frd.md`
- Code basis: `origin/main` at `bcde4983`

## Summary

This effort adds one read-only graph-query path over the retained resource dependency graph, while
making three atomic CLI corrections around it. The architecture has four boundaries:

1. the finalized `Registry` remains the authority for declared resource identity and edges;
2. a new query service performs deterministic focus-rooted traversal and returns closed node and
   edge facts;
3. live-instance projection is a lazy, terminal edge source opened read-only only when traversal
   demands it; and
4. human and JSON renderers consume the same complete `GraphResult` without reaching back into the
   registry, database, config, or resource objects.

The direction and depth expansion is deliberate product scope. In particular, `both` may change
direction at every resource expansion. With `--depth all`, that returns the focus's complete weakly
connected resource component, which may equal the whole current registry when the registry is
connected. It remains a focus-rooted query rather than a focus-optional whole-graph operation.

The other corrections do not acquire new architecture: `resource describe-kind` is renamed to the
existing config-free `resource explain`; the fixed schema writer becomes
`resource schema --install`; and `resource describe` and its machine contract are removed after
their remaining callers are separated from its presentation service.

## Current architectural anchors

- `resources/graph.py` retains immutable outbound `ResourceReference` objects and reduced inbound
  `ReferenceEntry` objects per finalized node. The graph builder receives the full outbound objects
  keyed by both source and target before reducing the inbound side.
- A `ResourceReference` already carries target identity, source identity, semantic relationship
  (`uses` or `inherits`), usage, and optional declaration provenance. `ReferenceEntry` intentionally
  drops the relationship because it was built for the old inbound describe card.
- Each eligible `ResourceKind` may provide an optional
  `instances(db, registry, resource) -> Iterable[InstanceRef]` projection. These hooks describe live
  instances that depend on one resource under current config; some hooks summarize a dependency path
  longer than one declared-resource edge.
- `Database(read_only=True)` is the existing non-migrating database boundary. It rejects stale,
  newer, malformed, busy, and unreadable state rather than repairing it.
- `manifests.reference.reference_for` and `manifests.describe.render_reference` are already the
  config-free explanation service and renderer. The command rename must keep that path intact.
- `machine_output.py` owns the closed JSON v1 command enumeration and envelope. Command services own
  their closed data records and ordering.
- The guide-value deletion wave owns removing runtime resource topics and the generic guide
  projection they use. Graph does not depend on `GuideView` or preserve a guide adapter that is
  scheduled to disappear.

## Architectural decisions

### A1. Extend the frozen graph's read API; do not rebuild or mutate it

`DependencyGraph` gains one declared-edge query:

```text
incoming_edges_of(kind, name) -> tuple[ResourceReference, ...]
```

The builder seats this tuple directly from the full target-keyed references it already holds. The
existing `dependents_of` projection and its `ReferenceEntry` contract remain available to unchanged
callers, including `secret describe`; graph query uses the full incoming edge instead. This keeps
the semantic relationship and provenance authoritative in both directions without:

- widening the producer-facing or legacy `ReferenceEntry` record;
- joining on usage prose at query time;
- calling a resource's `dependencies()` again; or
- exposing or mutating the graph's private node map.

The graph builder stores the full inbound tuple alongside the reduced compatibility projection. The
modest duplicate reference storage is preferable to a second derivation path and keeps every query a
pure read over frozen tuples.

Declared traversal crosses an explicit allowlist containing `uses` and `inherits`. A later
relationship enum value does not silently join graph traversal: its producer and graph semantics
must be decided together. In addition to the existing enum-coverage test, graph owns a test
requiring every relationship to receive an explicit traverse-or-exclude decision.

### A2. A dedicated query service owns traversal and fact assembly

A new service beside the resource graph accepts only explicit dependencies:

```text
show_graph(
    registry,
    focus,
    direction,
    depth_limit,
    live_source,
) -> GraphResult
```

The CLI parses argv, loads config and a finalized request registry, constructs the lazy live source,
calls the service once, then selects a renderer. The service owns identity validation, traversal,
deduplication, ordering, and source failure framing. It emits no output.

The registry for this command is built with host-readiness probing disabled. Finalization and
reference validation still run, but graph does not ask capability implementations to inspect local
host readiness that the result never displays. Registry construction must not resolve secrets,
prompt, activate resources, or call provider or remote APIs.

The focus parser splits on the first `/` only. The kind is structural; the complete remainder is the
name passed unchanged to registry lookup. Dots, colons, and legacy double hyphens therefore remain
ordinary stored identity characters. A missing slash or empty side is a usage error; an unknown kind
or name is a typed not-found error from the service boundary.

### A3. The result is a closed, renderer-independent graph snapshot

The service returns frozen records with no resource, handler, implementation, database, or config
objects attached.

```text
ResourceIdentity
  kind: string
  name: string

GraphIdentity
  node_type: resource | live-instance
  kind: string
  name: string

GraphQuery
  focus: ResourceIdentity
  direction: dependencies | dependents | both
  depth_limit: positive integer | None       # None means all

GraphNode
  node_type: resource | live-instance
  kind: string
  name: string
  distance: non-negative integer

GraphEdge
  edge_type: declared | live-usage
  source: GraphIdentity
  target: GraphIdentity
  relationship: uses | inherits
  usage: string | None
  declared_by: ResourceIdentity | None

GraphResult
  query: GraphQuery
  nodes: tuple[GraphNode, ...]
  edges: tuple[GraphEdge, ...]
```

Declared edges retain their intrinsic `resource source -> resource target` orientation and carry the
producer's relationship, usage, and explicit `declared_by` value. A missing `declared_by` remains
null rather than being replaced with the source.

A live edge has intrinsic `live instance -> resource` orientation because the instance depends on
the resource. Its `edge_type` is `live-usage`, its relationship is `uses`, and usage and
`declared_by` are null. The separate edge type says where the fact came from without inventing a
capability-facet taxonomy. Graph never infers a facet from endpoint kinds, usage prose, or the
consuming resource's config.

Every projected live edge counts as exactly one graph hop, even when the kind hook summarized a
longer config dependency. That is the only distance the projection can substantiate; the declared
edges remain present for an operator who wants to inspect the underlying resource path separately.

### A4. Breadth-first traversal defines distance, bounds, and mixed direction

Traversal is breadth-first so each node's distance is the shortest eligible hop count from the focus
and human grouping follows directly from the result. The queue contains resource nodes only;
live-instance nodes are recorded and never expanded.

For each resource node whose distance is below the finite limit, or for every reached resource under
`all`:

- `dependencies` inspects full outbound declared edges;
- `dependents` inspects full incoming declared edges and, when available for the kind, live-usage
  edges; and
- `both` inspects both arms at that node. A path may therefore alternate directions at successive
  expansions.

This mixed-direction rule is intentional even though a complete `both` query can reach the focus's
entire weakly connected component. The implementation contains that complexity in this one service;
neither the CLI nor either renderer implements traversal branches.

A visited-node map is keyed by `(node_type, kind, name)` and records first discovery distance. It
prevents cycles and repeated expansion. Encountering a known node still records a distinct edge, so
diamonds, cycles, parallel semantic relations, and cross edges are not turned into a discovery tree.

Direction selects node reachability. After reachability settles, the service takes a pure in-memory
pass over the frozen declared graph and includes every allowlisted declared edge whose two resource
endpoints are in the result. The returned declared edges therefore form the induced declared
subgraph of the returned resource nodes, including boundary-to-boundary cross edges. This pass does
not discover more nodes and cannot demand the database. Live edges are included only when collected
at a dependents-capable expansion with remaining depth; the service never opens live state merely to
complete an induced edge set at the boundary.

Exact duplicate edges collapse by the complete fact key: edge type, both typed endpoint identities,
relationship, usage, and declaration provenance. Parallel edges that differ in any field remain.
Nodes are ordered by distance, then an explicit node-type rank (`resource` before `live-instance`),
kind, and name. Edges are assigned to the greater of their endpoint distances and ordered by that
distance, then typed source, typed target, edge type, relationship, usage, and declaration
provenance. Neighbor consideration uses the same canonical identity order, so equal-length path
choices never inherit registry insertion order.

All sort keys are total tuples over primitive scalars. A nullable scalar is keyed as `(0, "")` when
absent and `(1, value)` when present; nullable declaration provenance uses `(0, "", "")` or
`(1, kind, name)`. No comparison depends on Python object ordering or uses a sentinel that can
collide with stored identity or prose.

### A5. Live state is a lazy, request-scoped source

The query service receives a small, context-managed live-source object rather than a `Database`
opened by the CLI. It has four internal states: unopened, absent, open, and closed. It is
request-scoped and single-use; no operation is valid after close. On the first resource expansion
that:

1. includes the dependents arm;
2. has remaining depth; and
3. belongs to a kind with an `instances` hook,

the source checks the process's canonical `agentworks.db.DB_PATH` with error-preserving filesystem
operations. Only a definite not-found result becomes an empty live source; permission,
path-component, and other inspection failures remain source errors. There is no persisted instance
history to project when the path is absent. A present database is opened once through
`Database(read_only=True)`, reused for the request, and closed in `finally` on success or failure.
Merely discovering a hook-owning resource at the depth bound does not inspect the path or open the
database.

Opening a present source also enters one explicit database read-transaction boundary before the
first instance hook. The first read establishes the SQLite snapshot, and every hook query for that
graph request executes inside the same transaction. The boundary exits only after all live facts
have been copied or after failure, then the connection closes. The implementation adds a narrow
read-only transaction API to `Database`; it does not reuse the write-oriented transaction helper or
reach through the database's private connection. This makes `GraphResult` one registry snapshot plus
one coherent persisted-state snapshot rather than a mix of independently timed reads.

The read-only connection performs no migration, directory creation, or logical write. Like the
repository's other SQLite read-only consumers, it may participate in SQLite's WAL coordination; that
driver bookkeeping is not persisted Agentworks state and does not grant application write authority.

If a demanded present database is stale, newer, malformed, busy, or unreadable, the query fails as a
whole with a source-specific typed error and no partial human or JSON result. The command cannot
truthfully label a partial closure complete. A query that never demands live projection is
independent of every database condition.

An `instances` hook receives only the finalized registry, its exact resource row, and this read-only
database. A hook result is copied immediately into `GraphNode` and `GraphEdge` facts; no database
row or resource object escapes the source boundary.

### A6. Human and JSON output are two projections of the same result

Human output is flat, not a tree drawing. It prints the focus and effective query, then distance
groups. Each group lists nodes first and the edges first surfaced at that distance second; a cross
edge is assigned to the greater endpoint distance and appears once. Arrows always show intrinsic
edge orientation even when the query reached an edge in reverse. Declared relationship verbs are
visible; live edges are identified as current-config live usage. Usage and declaration provenance
are details on the edge, never replacements for its typed relationship.

The exact terminal typography belongs in the graph-query LLD and renderer tests. The architectural
constraint is that indentation never encodes traversal ancestry and no renderer chooses or drops
facts.

JSON uses the existing version-1 envelope with command `graph.show`. Its closed `data` shape is:

```json
{
  "query": {
    "focus": { "kind": "vm-platform", "name": "azure-vm" },
    "direction": "both",
    "depth_limit": 2
  },
  "nodes": [{ "node_type": "resource", "kind": "vm-platform", "name": "azure-vm", "distance": 0 }],
  "edges": [
    {
      "edge_type": "declared",
      "source": { "node_type": "resource", "kind": "vm-site", "name": "production" },
      "target": { "node_type": "resource", "kind": "vm-platform", "name": "azure-vm" },
      "relationship": "uses",
      "usage": "the platform selected by vm-site:production",
      "declared_by": null
    }
  ]
}
```

`depth_limit` is null for `--depth all`; it is never the string `"all"` in machine output. Every
node and edge record carries the complete fixed field set shown by its type. The projector copies
only these explicit safe scalars, encodes fully before writing stdout, and never reflects resource
objects, database rows, config blocks, secret values, provider metadata, or arbitrary attributes.

### A7. Explanation stays config-free and unchanged below the command name

`resource explain TARGET` continues to call `reference_for(TARGET)` and `render_reference`. It does
not share the graph focus parser or resolver: identical `KIND/NAME` spelling describes schema space
here and registry instance space under graph.

The command registration, help, hints, completion mapping, documentation, and test identity change
from `resource.describe-kind` to `resource.explain`; field coverage, output, errors, ordering, and
availability do not. No capability currently offers multiple configuration models, so this effort
does not invent a facet descriptor or synthetic runtime path. It preserves the stable targets that
the future extension composes beneath: the implementation target will render every offered facet in
separate groups by default, and the capability-kind target will teach their shared vocabulary. The
harness-integration capability effort owns introducing that descriptor shape with its first real
multi-faceted implementation.

### A8. The remaining CLI corrections cut over atomically

`resource describe` is deleted together with its CLI registration, `resource.describe` command ID,
closed JSON fixture, completions, tests, presentation records, and renderer. Before deleting its
service, `resource edit` stops using `describe_resource` as a lookup shortcut and performs the same
validated registry lookup through a small identity resolver that returns only the row and origin it
needs. The correction must not preserve the old card-shaped service under another name.

`secret describe` continues to call its existing service and keeps its human and `secret.describe`
machine contracts. It may continue using `dependents_of`; adding `incoming_edges_of` is additive and
does not force an unrelated migration.

The resource schema CLI replaces only the fixed-destination boolean mode with `--install`. Its
existing schema-set writer and canonical destination remain authoritative. Path-valued sample
writing and stdout schema forms do not change.

The command registry and JSON command enum change in one coherent cut:

- remove `resource.describe`;
- add `graph.show`;
- rename the CLI/completion identity `resource.describe-kind` to `resource.explain`; and
- replace schema `--write` with `--install` while leaving sample `--write PATH` intact.

No alias, warning, dual command ID, fallback dispatcher, or deprecated record remains.

### A9. Completions and documentation follow command ownership

Graph focus completion reuses the config-backed resource-reference candidate source so completion
offers exact `KIND/NAME` registry identities. Direction has the three closed values; depth suggests
positive common values and `all` without trying to enumerate an unbounded integer grammar. Explain
keeps the config-free kind and capability-implementation completer under its new command identity.

Bash, zsh, and PowerShell generation consume the same introspected command tree and dynamic mapping.
No shell receives a hand-maintained grammar variant.

The survey-approved guide-value deletion landed in PR #556, and this branch's code basis includes
it. Every implementation rebase must preserve that post-deletion surface. The rewrite updates only
surviving guide teaching and actions, never restores runtime resource topics, schema blocks, or
generic guide projection machinery removed by that effort. The active command reference, CLI README,
sample config, surviving domain READMEs, upgrade guide, hints, and generated completions move with
their owning behavior. Resource-group help is updated explicitly so its current
`Cross-kind inspection` description does not survive the generic inspector's removal.

## End-to-end control flow

```text
agw graph show vm-platform/azure-vm --direction both --depth 2
  -> CLI parses focus/direction/depth/output
  -> load config and finalized registry without host-readiness probing
  -> construct unopened live source
  -> graph query BFS over full outgoing/incoming declared adjacency
       distance 0 platform
       distance 1 dependent vm-sites
       expansion of each distance-1 site demands one shared read-only DB
       distance 2 live VMs; live nodes stop
  -> collect induced declared edges among returned resource nodes
  -> freeze and canonically order GraphResult
  -> close live source
  -> human renderer OR graph.show JSON projector
```

The platform-to-site-to-VM example is also the source-demand proof: depth 1 reaches sites without
opening the database because they are at the bound; depth 2 expands them and opens it once.

## Failure and absence semantics

| Condition                                    | Result                                                                   |
| -------------------------------------------- | ------------------------------------------------------------------------ |
| malformed focus or invalid direction/depth   | CLI usage error before registry or database work                         |
| unknown kind or absent registry name         | typed not-found error; no database work                                  |
| invalid config or manifest/reference graph   | existing config/finalization error; no graph result                      |
| database not demanded                        | database is not inspected, regardless of its state                       |
| demanded database absent                     | live source is empty; declared traversal succeeds                        |
| demanded database stale/newer/malformed/busy | whole query fails through the existing typed read-only database error    |
| demanded hook raises on persisted data       | whole query fails with hook kind/resource context and the cause retained |
| no declared or live neighbors                | success containing only the distance-zero focus node                     |

No failure path activates a resource, repairs a PID, migrates state, resolves a secret, probes a
provider or remote host, prompts, or returns partial JSON.

## Verification boundaries

The implementation plan should separate service facts from CLI presentation and prove:

- default, dependencies, dependents, and mixed-direction `both` traversal at finite depths and
  `all`, including direction changes, cycles, diamonds, parallel edges, induced cross edges, and
  stable shortest distances;
- the explicit `uses`/`inherits` traversal allowlist and verb preservation from both adjacency
  directions;
- exact duplicate collapse while distinct parallel facts remain;
- a projected live edge is one hop, live nodes are terminal, and the platform/site/VM path demands
  the database only when a site is expanded;
- absent database as an empty source, demanded read-only failures as whole-query failures, one open
  and one explicit read transaction per request, close-on-error, consistent concurrent-writer
  visibility, and no migration or logical write;
- identical service facts feeding deterministic flat human output and the exact closed `graph.show`
  JSON shape, with no secret or raw-object reflection;
- first-slash focus parsing and legacy names containing double hyphens;
- rename-only config-free explanation, unchanged `secret describe`, schema-install parity, and
  complete removal of the generic resource card and old machine ID; and
- migration of every resource-describe test that was serving as a convenient fact assertion to its
  surviving owner, rather than blanket deletion with the presentation tests; and
- generated completion parity across bash, zsh, and PowerShell with broken or absent config on the
  config-free explanation path.

Representative broad registries and repeated hook-owning resources also receive service-level scale
tests. The graph-query LLD records the expected traversal, induced-edge, and per-kind projection
complexity so accepted closure breadth does not hide accidental repeated full scans.

Tests assert records, ordering, types, source demand, exit behavior, and JSON shape. They do not pin
authored human prose.

## Delivery and coordination

By explicit operator direction, the existing draft artifact PR remains the single delivery vehicle
through implementation rather than merging ahead under the active-saga default. Its public artifact
handoff supplies the coordination surface, and its implementation plan uses responsibility-aligned,
always-green commits. Internal graph storage, query primitives, database read-transaction support,
and their tests may be introduced in earlier commits without changing the CLI. The command
registration, old command and machine-ID removal, generated completions, active documentation,
resource-group help, hints, and cutover tests move together in one collateral-complete commit. No
commit exposes new grammar with stale ownership or active teaching. The PR remains draft with no
merge intent until the operator says otherwise.

Implementation ordering inherited from the saga is:

1. preserve the landed post-cut guide surface and do not recreate deleted guide machinery; and
2. keep the complete grammar correction as a 0.14.0 release gate.

The PR review thread adds two coordination steps to reconfirm at implementation kickoff: publish the
dying command/test list so the simplification sweep can avoid ownership collisions, and coordinate
cutover timing with the harness-integration descriptor deletion work. These are coordination notes,
not architectural dependencies or authority to widen this effort.

## LLDs to produce in the plan

- **`graph-query-lld.md`**: exact frozen record types, incoming-edge storage, BFS and induced-edge
  pseudocode, canonical keys/order, lazy database source and read-transaction state machine, error
  wrapping, complexity bounds, human grouping, and the closed `graph.show` JSON schema.
- **`cli-cutover-lld.md`**: command registration and identity changes, resource-edit lookup
  extraction, deletion inventory, schema-install flag behavior, completion mappings and generated
  artifacts, active-document ownership sweep, and always-green commit boundaries within the draft
  implementation PR.

## Risks and containment

- **Mixed-direction closure is broader than the named platform query.** This is accepted product
  scope. The containment is one focus-rooted BFS with explicit depth, visited nodes, relationship
  allowlist, terminal live nodes, and no focus-optional whole-graph mode.
- **Live projection can make a structural query depend on persisted state.** Lazy expansion-time
  acquisition, an absent-as-empty rule, read-only open, and whole-result failure prevent unrelated
  queries from paying that cost or receiving falsely complete data.
- **Inbound semantic verbs could drift from outbound truth.** Full incoming references are seated
  from the builder's authoritative outbound objects; query code never infers or joins by prose.
- **Two renderers could diverge.** Both consume one closed `GraphResult`; collection and ordering
  complete before renderer selection.
- **The cutover could preserve the old presentation service accidentally.** The deletion inventory
  includes its DTO, projector, renderer, command ID, and tests, while `resource edit` first moves to
  a fact-minimal identity resolver.
- **Guide churn can create duplicate or resurrected ownership.** Graph and explain depend only on
  registry/schema services, and implementation treats the survey-approved cut surface as absent.
