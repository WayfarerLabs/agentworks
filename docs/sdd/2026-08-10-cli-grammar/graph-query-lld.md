# Graph Query Low-Level Design

<!-- cspell:words contextmanager deque popleft -->

- Status: Draft for final artifact checkpoint review
- Date: 2026-08-16
- Implements: `frd.md` FR6-FR14 and QR1-QR4
- Architecture: `hla.md` A1-A6
- Code basis: `origin/main` at `bcde4983`

## Scope and invariants

This design pins the graph fact service, its read-only live source, and its two projections. It does
not own command registration, focus-token parsing, the explain/schema cutover, or deletion of
`resource describe`; `cli-cutover-lld.md` owns those details.

The implementation keeps these invariants structural:

1. The finalized `Registry` and its frozen `DependencyGraph` are the only declared-resource node and
   edge authority.
2. Traversal is focus-rooted breadth-first search. `both` selects both incident arms independently
   at every expanded resource and may alternate direction along one path.
3. Every returned distance is the shortest eligible hop count. Resource nodes expand at most once;
   live-instance nodes never expand.
4. Returned declared edges are the allowlisted induced declared subgraph of reached resource nodes.
   Returned live edges are only facts collected while expanding an eligible frontier resource. This
   asymmetry is intentional: reaching a resource at the depth bound neither demands nor implies its
   live facts.
5. `live-usage` always uses relationship `uses` as an edge-type convention. It is not an inferred
   capability facet, a reconstructed declaration verb, or a claim about why a field exists.
6. Collection completes, the live source closes, and a closed `GraphResult` exists before either
   renderer receives control. Any source failure therefore fails the whole query with no partial
   human or JSON output.

## Current code anchors and file ownership

| File                                       | Current anchor                                                                      | Change owned by this design                                                           |
| ------------------------------------------ | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `cli/agentworks/resources/graph.py`        | `_Node` at line 177, `DependencyGraph` reads at line 208, `build_graph` at line 440 | Retain full incoming references and expose `incoming_edges_of`                        |
| `cli/agentworks/resources/graph_query.py`  | New                                                                                 | Enums, frozen fact records, allowlist, BFS, live source, grouping, and JSON projector |
| `cli/agentworks/resources/graph_render.py` | New                                                                                 | Flat human rendering over completed groups only                                       |
| `cli/agentworks/db/database.py`            | `Database` at line 54, `transaction` at line 133                                    | Record construction mode and add the explicit read transaction                        |
| `cli/agentworks/resources/__init__.py`     | Existing public resource exports                                                    | Export only graph-query types needed by the CLI and tests                             |
| `cli/agentworks/machine_output.py`         | command enum at line 38, whole-document encoder at line 59                          | The cutover adds `GRAPH_SHOW`; graph uses the existing encoder unchanged              |
| `cli/tests/resources/test_graph.py`        | Existing frozen graph tests                                                         | Full incoming-edge retention and compatibility coverage                               |
| `cli/tests/resources/test_graph_query.py`  | New                                                                                 | Traversal, source lifecycle, demand, ordering, grouping, scale, and JSON projection   |
| `cli/tests/db/test_read_transaction.py`    | New                                                                                 | Read transaction mode, snapshot, nesting, cleanup, and close behavior                 |
| `cli/tests/test_machine_output.py`         | Existing envelope tests                                                             | Exact `graph.show` envelope and terminal-control coverage                             |

`resources/graph_query.py` may inspect `KIND_REGISTRY` only to determine whether a kind has an
`instances` hook and to invoke that hook for the exact reached registry row. It must not inspect
resource attributes other than passing the row unchanged into its owning hook. The renderer modules
must not import `Registry`, `Database`, config, kind handlers, resource rows, or origins.

The CLI obtains a syntax-checked `ResourceIdentity` from the shared access primitive specified by
`cli-cutover-lld.md` and calls `show_graph`. `show_graph` imports that record and `resolve_resource`
from `resources.access`; it does not define a second identity type or duplicate the lookup. The
service rejects a non-finalized registry with `StateError`, then calls `resolve_resource` once and
retains its exact row for eligible live-instance projection. Unknown-kind and missing-name errors
therefore have the shared resolver's specified shape. No database operation precedes these checks.

## Frozen records and enums

All enums below are `StrEnum`. All records are `@dataclass(frozen=True, slots=True)` and contain
only enums, strings, integers, `None`, or tuples of these records.

```python
class GraphDirection(StrEnum):
    DEPENDENCIES = "dependencies"
    DEPENDENTS = "dependents"
    BOTH = "both"

class GraphNodeType(StrEnum):
    RESOURCE = "resource"
    LIVE_INSTANCE = "live-instance"

class GraphEdgeType(StrEnum):
    DECLARED = "declared"
    LIVE_USAGE = "live-usage"

class LiveSourceState(StrEnum):
    UNOPENED = "unopened"
    ABSENT = "absent"
    OPEN = "open"
    CLOSED = "closed"

@dataclass(frozen=True, slots=True)
class GraphIdentity:
    node_type: GraphNodeType
    kind: str
    name: str

@dataclass(frozen=True, slots=True)
class GraphQuery:
    focus: ResourceIdentity
    direction: GraphDirection
    depth_limit: int | None

@dataclass(frozen=True, slots=True)
class GraphNode:
    node_type: GraphNodeType
    kind: str
    name: str
    distance: int

@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_type: GraphEdgeType
    source: GraphIdentity
    target: GraphIdentity
    relationship: RefRelationship
    usage: str | None
    declared_by: ResourceIdentity | None

@dataclass(frozen=True, slots=True)
class GraphResult:
    query: GraphQuery
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

@dataclass(frozen=True, slots=True)
class GraphDistanceGroup:
    distance: int
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
```

`GraphQuery.__post_init__` rejects a non-`None` `depth_limit` unless `type(depth_limit) is int` and
it is positive. `GraphNode.__post_init__` rejects a negative distance. Its identity fields stay
direct so the closed record matches the machine contract without a flattening step.
`GraphEdge.__post_init__` enforces the variant relationship that Python's type system cannot
express:

- `declared` requires two resource endpoints, an allowlisted relationship, and a non-`None` usage;
- `live-usage` requires a live-instance source, resource target, relationship `uses`, and null
  `usage` and `declared_by`.

The graph-query records do not validate registry identity spelling again. `ResourceIdentity` is the
frozen, slotted record owned by `resources.access`; the CLI parser and the service's single shared
resolver call are the boundaries.

Helpers construct identities without reflection:

```python
def resource_graph_identity(value: ResourceIdentity) -> GraphIdentity:
    return GraphIdentity(GraphNodeType.RESOURCE, value.kind, value.name)

def live_graph_identity(ref: InstanceRef) -> GraphIdentity:
    return GraphIdentity(GraphNodeType.LIVE_INSTANCE, ref.instance_kind, ref.instance_name)
```

## Full incoming declared edges

Extend the private frozen graph node without changing the compatibility projection:

```python
@dataclass(frozen=True)
class _Node:
    key: tuple[str, str]
    outbound: tuple[ResourceReference, ...]
    incoming: tuple[ResourceReference, ...]       # new, full authoritative facts
    inbound: tuple[ReferenceEntry, ...]           # retained compatibility projection
    enablement: Enablement
    readiness: Readiness
    impl: object | None
```

`build_graph` seats `incoming=tuple(all_refs.get(key, ()))` directly. The objects are the same
frozen `ResourceReference` instances already stored in the source node's `outbound` tuple. It
continues to derive `inbound` from those references exactly as today, including order and
duplicates.

```python
def incoming_edges_of(self, kind: str, name: str) -> tuple[ResourceReference, ...]:
    return self._nodes[(kind, name)].incoming
```

The method has the same unknown-key `KeyError` contract as `edges_of` and `dependents_of`. Existing
closures and `dependents_of` remain byte-for-behavior unchanged. Tests prove source, target,
relationship, usage, optional `declared_by`, order, and parallel facts survive both directions.

Graph query owns a separate, explicit relationship decision:

```python
GRAPH_TRAVERSED_RELATIONSHIPS = frozenset(
    {RefRelationship.USES, RefRelationship.INHERITS}
)
GRAPH_EXCLUDED_RELATIONSHIPS: frozenset[RefRelationship] = frozenset()
```

`test_every_relationship_has_an_explicit_graph_query_decision` asserts the two sets are disjoint and
their union equals `set(RefRelationship)`. Traversal and the induced pass both test membership in
`GRAPH_TRAVERSED_RELATIONSHIPS`; neither uses a negative filter or the closure-specific sets.

## Canonical keys and exact deduplication

Every key is a tuple of primitive scalars:

```python
def node_type_rank(value: GraphNodeType) -> int:
    return 0 if value is GraphNodeType.RESOURCE else 1

def identity_key(value: GraphIdentity) -> tuple[int, str, str]:
    return (node_type_rank(value.node_type), value.kind, value.name)

def nullable_text_key(value: str | None) -> tuple[int, str]:
    return (0, "") if value is None else (1, value)

def nullable_resource_key(value: ResourceIdentity | None) -> tuple[int, str, str]:
    return (0, "", "") if value is None else (1, value.kind, value.name)

def edge_fact_key(edge: GraphEdge) -> tuple[object, ...]:
    return (
        edge.edge_type.value,
        identity_key(edge.source),
        identity_key(edge.target),
        edge.relationship.value,
        nullable_text_key(edge.usage),
        nullable_resource_key(edge.declared_by),
    )
```

`edge_fact_key` is the exact duplicate definition. Facts differing in edge type, typed endpoint,
relationship, usage, or declaration provenance remain parallel edges. Nodes deduplicate only by
`identity_key`.

Final node order is `(distance, node_type_rank(node_type), kind, name)`, so resources precede live
instances within a distance group. For an edge, let `edge_distance` be the maximum distance of its
endpoints. Final edge order is:

```text
edge_distance,
identity_key(source),
identity_key(target),
edge_type.value,
relationship.value,
nullable_text_key(usage),
nullable_resource_key(declared_by)
```

Neighbor candidates are ordered by `identity_key(neighbor), edge_fact_key(edge)` before discovery.
No key compares `None` with text, enum objects with strings, or arbitrary objects with each other.

## Traversal algorithm

`show_graph` has the exact service signature:

```python
def show_graph(
    registry: Registry,
    focus: ResourceIdentity,
    direction: GraphDirection,
    depth_limit: int | None,
    live_source: DatabaseLiveSource,
) -> GraphResult:
    ...
```

The CLI supplies a new request-scoped source even when the query will not demand it. Construction
does no filesystem or database work. `show_graph` owns the source context so every exit closes it.

The BFS is:

```text
with live_source:
    validate finalized registry
    resolved_focus = resolve_resource(registry, focus)
    query = GraphQuery(focus, direction, depth_limit)
    focus_id = resource_graph_identity(focus)
    distance = {focus_id: 0}
    queue = deque([focus_id])
    rows = {focus_id: resolved_focus.resource}
    edges_by_key = {}

    while queue:
        current = queue.popleft()
        current_distance = distance[current]
        if depth_limit is not None and current_distance >= depth_limit:
            continue

        # The queue contains resources only.
        row = rows.get(current)
        if row is None:
            row = registry.lookup(current.kind, current.name)
            rows[current] = row
        candidates = []

        if direction in {DEPENDENCIES, BOTH}:
            for ref in registry.graph.edges_of(current.kind, current.name):
                if ref.relationship in GRAPH_TRAVERSED_RELATIONSHIPS:
                    edge = declared_edge(ref)
                    candidates.append((edge.target, edge))

        if direction in {DEPENDENTS, BOTH}:
            for ref in registry.graph.incoming_edges_of(current.kind, current.name):
                if ref.relationship in GRAPH_TRAVERSED_RELATIONSHIPS:
                    edge = declared_edge(ref)
                    candidates.append((edge.source, edge))

            if live_source.supports(current.kind):
                for instance in live_source.instances_for(registry, current, row):
                    live = live_graph_identity(instance)
                    edge = live_usage_edge(live, current)
                    candidates.append((live, edge))

        for neighbor, edge in sorted(candidates, key=neighbor_candidate_key):
            edges_by_key.setdefault(edge_fact_key(edge), edge)
            if neighbor in distance:
                continue                    # edge stays; node does not requeue
            distance[neighbor] = current_distance + 1
            if neighbor.node_type is GraphNodeType.RESOURCE:
                queue.append(neighbor)      # live nodes are terminal

    reached_resources = {
        identity for identity in distance
        if identity.node_type is GraphNodeType.RESOURCE
    }
    for source in sorted(reached_resources, key=identity_key):
        for ref in registry.graph.edges_of(source.kind, source.name):
            if ref.relationship not in GRAPH_TRAVERSED_RELATIONSHIPS:
                continue
            edge = declared_edge(ref)
            if edge.target in reached_resources:
                edges_by_key.setdefault(edge_fact_key(edge), edge)

build and sort GraphNode records from distance
sort edges_by_key values using the completed distance map
return GraphResult(query, nodes, edges)
```

`declared_edge` always takes source from `ref.source` and target from `ref.kind/ref.name`; reverse
traversal never flips an edge. It converts `ref.declared_by` to `ResourceIdentity` only when
present.

The induced pass scans outgoing declared edges of reached resources. It never discovers a node,
calls `incoming_edges_of`, inspects `KIND_REGISTRY`, or touches the live source. Consequently a
declared boundary cross edge is included, while a live edge from that same boundary resource is not
invented. Live edges collected from expanded resources remain even when their live node was already
known through another expanded resource.

Finite and `all` use the same loop. `None` means `all`; there is no sentinel distance. A resource is
expanded once at its shortest distance. FIFO layers plus canonical neighbor order make every
shortest distance independent of registry insertion order. In `both`, the two candidate arms are
combined at each expansion before ordering, so a path such as incoming, outgoing, incoming is valid.

## Live source lifecycle and failure framing

`DatabaseLiveSource` is a single-use context manager constructed with an explicit `database_path`.
It stores `state`, `_db`, and `_read_transaction_context`. Its state transitions are:

| Current        | Operation                                                   | Next       | Effect                                               |
| -------------- | ----------------------------------------------------------- | ---------- | ---------------------------------------------------- |
| `unopened`     | `supports(kind)`                                            | `unopened` | Pure kind-handler inspection only                    |
| `unopened`     | first `instances_for` and `stat` raises `FileNotFoundError` | `absent`   | Return empty tuple                                   |
| `unopened`     | first `instances_for` and path is present                   | `open`     | Open one read-only DB and enter one read transaction |
| `absent`       | later `instances_for`                                       | `absent`   | Return empty tuple without another `stat`            |
| `open`         | later `instances_for`                                       | `open`     | Reuse the same DB and transaction                    |
| any live state | context exit                                                | `closed`   | Exit transaction first, then close DB                |
| `closed`       | any operation or second context entry                       | error      | Raise `StateError`                                   |

`supports(kind)` returns whether `KIND_REGISTRY.get(kind)` has a callable `instances` attribute. It
must not inspect the path. The exact database-demand predicate is therefore:

```text
current is a resource
and current distance has remaining depth
and direction includes dependents
and supports(current.kind)
```

The BFS depth guard runs before `supports`, and the dependencies-only arm never calls it.

Source opening uses `database_path.stat()` exactly once. `FileNotFoundError` (`ENOENT`) is definite
absence. `PermissionError`, `NotADirectoryError`, and every other `OSError` become a
`StateError(entity_kind="database")` with the original exception as `__cause__`. A path that was
successfully stated but disappears before open is an open failure, not absence. A directory at the
path is also an open failure.

For a present path, construct exactly one `Database(database_path, read_only=True)`, then enter
`db.read_transaction()` before invoking the first hook. State changes to `open` only after both
steps succeed. If transaction entry fails after construction, close the database before propagating
the failure. Existing `AgentworksError` subclasses from open, including stale, newer, malformed, and
busy errors, propagate unchanged. An untyped filesystem/driver open exception is framed as
`StateError(entity_kind="database")` with its cause.

`instances_for` invokes the exact kind handler with `(db, registry, row)` and consumes its iterable
inside the source boundary. Every `InstanceRef` is copied immediately into graph records by BFS. Any
`Exception` raised while calling or consuming the hook is wrapped in
`StateError(entity_kind=current.kind, entity_name=current.name)` with the original cause. No hook
output, DB row, generator, registry row, or database object escapes into `GraphResult`.

Context exit always attempts transaction exit before close and sets `closed` in `finally`. A close
or rollback failure fails `show_graph`; rendering has not started. `KeyboardInterrupt`,
`SystemExit`, and other `BaseException` control signals are not translated, but the context still
closes.

### Database read transaction

`Database.__init__` records `_read_only = read_only` and initializes `_read_tx_active = False` on
both construction paths. The existing `_tx_depth` remains the write-transaction nesting state.

Add this public context manager:

```python
@contextmanager
def read_transaction(self) -> Iterator[None]:
    if not self._read_only:
        raise StateError(
            "a read transaction requires a read-only database",
            entity_kind="database",
        )
    if self._tx_depth != 0 or self._read_tx_active:
        raise StateError("database transaction nesting is unsafe", entity_kind="database")

    self._read_tx_active = True
    try:
        self._conn.execute("BEGIN")
        try:
            yield
        finally:
            self._conn.rollback()
    finally:
        self._read_tx_active = False
```

`BEGIN` is deliberately deferred: the first hook read establishes the SQLite snapshot, and every
later hook read remains in it. `rollback()` ends the read transaction without expressing a logical
write commit.

The existing `transaction()` rejects `_read_only` instances with `StateError` before changing
`_tx_depth`. `read_transaction()` rejects writable instances and nested entry. No code shares or
increments `_tx_depth` for a read transaction. Existing writable nesting and per-method commit
behavior remain unchanged. `Database.close()` remains callable after either context exits; the live
source orders exit before close.

Tests prove that a writer committing after the read transaction's first query is invisible to a
second query in that transaction, then visible after exit. They also prove exception rollback,
read-after-exit, close-after-exit, nested-read rejection, read-on-writable rejection,
write-transaction-on-read-only rejection, and SQLite rejection of an attempted write.

## Complexity and scale contract

Let `R` be reached resource nodes, `L` reached live nodes, `A` the total allowlisted adjacency
entries inspected during eligible expansions, `I` the outgoing entries scanned by the induced pass,
`E` distinct returned edges, and `H` expanded resources whose kind owns an instance hook.

- Node bookkeeping is `O(R + L)` space and time outside sorting.
- Traversal inspection is `O(A)`; `both` may inspect the same declared fact once from each expanded
  endpoint, and exact deduplication keeps one returned fact.
- Per-frontier candidate sorting is `O(sum(d_v log d_v))` over expanded candidate counts.
- The induced pass is `O(I)` and scans each reached resource's outgoing tuple once.
- Final ordering is `O((R + L) log(R + L) + E log E)`.
- Live source acquisition is one `stat`, at most one database open, and at most one read transaction
  per query. Hook invocation count is exactly `H`, once per eligible expanded resource.
- Current hook implementations often scan one DB table per resource. Their accepted launch cost is
  the sum of hook costs, for example `O(R_vm_template * number_of_vms)` for repeated VM-template
  rows. This design does not hide that as constant or introduce a bulk-hook contract without a
  requirement.

Scale tests build the same graph in forward and reverse insertion order with at least 250 resources,
diamonds, cross edges, and parallel facts, then assert identical results and shortest distances. A
repeated-hook-kind fixture expands `N` resources with one instrumented list query per hook and
asserts: one source `stat`, one database construction, one read transaction, `N` hook calls, and `N`
observed list queries. The test records these counts in assertion values, not timing thresholds. No
benchmark wall time becomes a correctness gate.

## Renderer-independent grouping and human structure

`group_graph_result(result) -> tuple[GraphDistanceGroup, ...]` lives in `graph_query.py`, not the
human renderer. It builds an identity-to-distance map from `result.nodes`, assigns each edge to the
maximum endpoint distance, and returns ascending nonempty distance groups. Nodes and edges retain
their already-canonical result order. It raises `AssertionError` if an edge endpoint is absent,
which is an internal construction invariant rather than operator input.

`render_graph_result(result)` calls that grouping function and emits this flat structure:

```text
Graph: KIND/NAME
Direction: dependencies|dependents|both
Depth: N|all

Distance 0
  Nodes
    resource KIND/NAME

Distance N
  Nodes
    resource KIND/NAME
    live-instance KIND/NAME
  Edges
    resource KIND/NAME -uses|inherits-> resource KIND/NAME [declared]
      Usage: TEXT
      Declared by: KIND/NAME
    live-instance KIND/NAME -uses-> resource KIND/NAME [live-usage, current config]
```

`Declared by` is omitted when null. `Edges` is omitted from a group with none. Every arrow retains
intrinsic source-to-target orientation. Indentation denotes only fixed record sections and optional
edge details; it never denotes discovery ancestry. The renderer does not compute distance, choose a
parent, collapse a repeated node, discover an edge, or change ordering.

Tests assert `GraphDistanceGroup` membership, ordering, and one-time edge assignment structurally.
Human renderer tests feed unique resource identities, live identities, relationship values, usage,
and provenance through the completed groups, then prove every fact appears exactly once and in the
same canonical group/order. They also prove null provenance does not manufacture a value. These are
fact-projection assertions, not prose assertions: exact labels, whitespace, and authored explanatory
wording are reviewed directly and exercised in live acceptance rather than pinned or blacklisted by
unit tests.

## Exact `graph.show` JSON v1 projection

The cutover adds `MachineOutputCommand.GRAPH_SHOW = "graph.show"`. The projector is
`graph_result_data(result) -> JsonObject` and returns keys in the order below:

```json
{
  "query": {
    "focus": { "kind": "vm-platform", "name": "azure-vm" },
    "direction": "both",
    "depth_limit": 2
  },
  "nodes": [
    {
      "node_type": "resource",
      "kind": "vm-platform",
      "name": "azure-vm",
      "distance": 0
    }
  ],
  "edges": [
    {
      "edge_type": "declared",
      "source": {
        "node_type": "resource",
        "kind": "vm-site",
        "name": "production"
      },
      "target": {
        "node_type": "resource",
        "kind": "vm-platform",
        "name": "azure-vm"
      },
      "relationship": "uses",
      "usage": "the platform selected by vm-site:production",
      "declared_by": null
    }
  ]
}
```

Every node and edge has exactly the fields shown by its record type. `declared_by`, when present, is
`{"kind": STRING, "name": STRING}`. `usage` is null for `live-usage`. `depth_limit` is null for
`all`, never the string `"all"`. Empty results still contain the focus node and empty `edges`.

Projection is explicit field-by-field code. It may read record attributes and enum `.value` only. It
must not call `asdict`, `vars`, `getattr`, a general JSON default hook, resource projectors, or an
object serializer. `GraphResult` itself cannot carry origins, resource rows, config, handlers,
database rows, implementation classes, provider metadata, or secrets. The CLI passes the completed
object to `write_json_envelope(GRAPH_SHOW, graph_result_data(result), stream)`, which encodes the
entire document and escapes terminal controls before its first write.

## Test matrix

| File                                  | Structural case                                                                                                                        |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/resources/test_graph.py`       | Full incoming `uses` and `inherits` references preserve every field, order, duplicates, and object authority                           |
| `tests/resources/test_graph.py`       | `dependents_of`, closures, readiness, and secret describe compatibility remain unchanged                                               |
| `tests/resources/test_graph_query.py` | Relationship decision sets are disjoint and exhaustive over `RefRelationship`                                                          |
| `tests/resources/test_graph_query.py` | Defaults, each direction, finite 1/2/N, and `all`                                                                                      |
| `tests/resources/test_graph_query.py` | Mixed `both` path changes direction per expansion; monotonic-union implementation would fail                                           |
| `tests/resources/test_graph_query.py` | Cycles, diamonds, known-node edges, self-edge defense, and shortest distance                                                           |
| `tests/resources/test_graph_query.py` | Exact duplicate collapse and distinct relationship/usage/provenance parallel facts                                                     |
| `tests/resources/test_graph_query.py` | Induced declared cross edges between reached boundary nodes                                                                            |
| `tests/resources/test_graph_query.py` | Live edges are one hop, terminal, intrinsically oriented, and fixed to conventional `uses`                                             |
| `tests/resources/test_graph_query.py` | Boundary live asymmetry: reached but unexpanded resource yields no live edge and no demand                                             |
| `tests/resources/test_graph_query.py` | Dependencies-only and no-hook queries never inspect the path                                                                           |
| `tests/resources/test_graph_query.py` | Missing DB is empty; permission, `ENOTDIR`, removal race, directory, stale, newer, malformed, busy, and unreadable DB fail whole query |
| `tests/resources/test_graph_query.py` | Hook call and generator-iteration failures carry resource context, preserve cause, close, and return no result                         |
| `tests/resources/test_graph_query.py` | One source/open/read transaction across multiple kinds and resources                                                                   |
| `tests/resources/test_graph_query.py` | Platform to site to live VM proves depth 1 no demand and depth 2 demand                                                                |
| `tests/resources/test_graph_query.py` | Total node/neighbor/edge order is identical under reversed construction order and null keys                                            |
| `tests/resources/test_graph_query.py` | Grouping assigns every edge once at maximum endpoint distance without ancestry                                                         |
| `tests/resources/test_graph_query.py` | Broad registry and repeated-hook query-count scale contracts                                                                           |
| `tests/resources/test_graph_query.py` | JSON projector exact fields, nulls, enums, order, empty edges, legacy punctuation, and poison-object exclusion                         |
| `tests/db/test_read_transaction.py`   | Mode misuse, unsafe nesting, first-read snapshot, concurrent writer, exception rollback, reuse after exit, and close after exit        |
| `tests/test_machine_output.py`        | Envelope version/command, encode-before-write, short writes, and DEL/C1 escaping with graph data                                       |
| CLI tests owned by cutover            | Unknown focus, no-neighbor success, human/JSON parity, and no stdout on whole-query source failure                                     |

Service tests use closed records as their expected values. They do not parse human prose to recover
facts that were already available before rendering.

## Implementation stop conditions

Return to the effort lead instead of improvising if implementation requires any of the following:

- a relationship beyond the explicit `RefRelationship` decision sets;
- live nodes that expand, live facts synthesized during the induced pass, or live facts treated as
  complete inventory;
- a writable/migrating database, a second database or read transaction in one query, or degraded
  success for a demanded source failure;
- resource/config reflection in either projector;
- a bulk live-hook API or cache that changes existing hook semantics without a requirement;
- a non-BFS traversal, monotonic-only interpretation of `both`, or a JSON v1 field/version change.
