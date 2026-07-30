# LLD (a): the retained `DependencyGraph`

Implements HLA [component 1](./hla.md) and the interface summary. Owns the graph data structure, its
query semantics, and how capability nodes carry their implementation. Governs FRD R1, R10, R11
(structure), and is consumed by every other LLD.

## Where it lives

A new module `resources/graph.py`. The `Registry` (`resources/registry.py`) gains a `_graph` slot,
populated at the end of `finalize` and returned by a `graph` property. `_resources` stays the row
store (the graph references rows by key, it does not own them).

## Node identity

Nodes are keyed by `(kind, name)`, the same identity the registry already uses. There is exactly one
node per published resource, including auto-declared and reserved-default rows, and including
capability rows (`vm-platform`, `harness`, `git-credential-provider`, `secret-backend`), which are
published resources today.

## Structure

```python
@dataclass(frozen=True)
class DependencyGraph:
    _nodes: Mapping[tuple[str, str], _Node]  # frozen after finalize

@dataclass(frozen=True)
class _Node:
    key: tuple[str, str]
    outbound: tuple[ResourceReference, ...]      # this node's declared edges (full refs)
    inbound: tuple[ReferenceEntry, ...]          # who references this node (source, usage)
    enablement: Enablement                       # enabled | disabled (R7)
    readiness: Readiness                          # verdict from the fold (LLD c); ready until phase 3
    impl: object | None                          # capability nodes only; see below
```

- **`outbound`** is today's `all_refs` (`registry.py:231`) re-keyed by **source** instead of target.
  Each `ResourceReference` already carries target `kind`/`name`, `usage`, and `source`, so no
  information is lost; the graph simply stops discarding it on finalize return.
- **`inbound`** is today's per-row `references` tuple (`ReferenceEntry(source, usage)`), moved off
  the resource dataclass (caller inventory section E) onto the node. It is derived from the same
  edge walk: for every outbound edge `A -> B`, a `ReferenceEntry(source=A, usage)` lands on `B`.
- **`enablement`** is `Enablement.enabled` for every node this effort produces; the enablement axis
  is modeled so the plugin rebuild can produce `disabled` without re-touching the core. A test
  fixture produces a `disabled` node (R7).
- **`readiness`** is filled by the fold (LLD c) in phase 3; before that (phases 2, 2b) every node is
  constructed ready. It is a `Readiness` verdict object, not `str | None` (R10, spares consumers the
  double negative).
- **`impl`** is the capability's implementation, carried so a dependent's readiness can call the
  capability's non-constructing `not_ready(config)` (LLD c) and resolution can call a backend (LLD
  d) **off the graph node** rather than the live capability registry (R11). It is `None` for
  non-capability nodes.

### The heterogeneous impl field

The four capability registries are not uniform: `vm-platform`, `harness`, and
`git-credential-provider` hold implementation **classes** (`not_ready` is a classmethod,
construction is separate), while `secret-backend` holds a constructed **instance**
(`SECRET_BACKEND_REGISTRY` maps name to a live `SecretBackend`, `secrets/backends.py:141`). The
`impl` field carries **whichever shape the kind uses**; it is typed `object | None` and each
consumer knows its kind's shape (the readiness fold calls a classmethod for
platform/harness/provider nodes; resolution calls instance methods on a secret-backend node). This
asymmetry is intrinsic to the current design and is not "fixed" here; the graph just stores what the
kind provides. The LLD-c fold and LLD-d resolution each document the exact call they make on their
kind's impl.

## Query API (the single access path, R11)

All five are pure reads over `_nodes`; none recomputes. They raise `KeyError` on an unknown key
(callers hold canonical keys from `iter_kind_items`), except `readiness_of`/`is_ready`, which the
projection surfaces call and which must tolerate a missing node gracefully (return a default-ready
verdict, matching today's "absent-on-class means never disabled" tolerance, `inspect.py:221`).

- **`edges_of(kind, name) -> tuple[ResourceReference, ...]`**: the node's `outbound`. The full
  candidate set for a secret (every would-attempt backend plus any dangling typo edge, per LLD b/d).
- **`dependents_of(kind, name) -> tuple[ReferenceEntry, ...]`**: the node's `inbound`. Replaces
  every `getattr(resource, "references", ())` reader (both inspect surfaces).
- **`reachable_from(kind, name) -> Iterable[tuple[str, str]]`**: the transitive closure of
  `outbound` from the node, excluding the start node, deduped, in first-encountered order. This is
  what `walk.collect_secrets_for` needs (a transitive reachability query, not immediate neighbors);
  it is a graph-owned DFS over the frozen `outbound` edges, so the caller stops hand-rolling one
  (`walk.py:71`). It must dedupe (a diamond reaches a node by two paths) and must not loop (the
  graph is acyclic post cycle-detection, but `reachable_from` is defensively cycle-safe via a
  visited set, since it can in principle be called on a graph built before the cycle pass in tests).
- **`readiness_of(kind, name) -> Readiness`**: the stored verdict. No recompute (R10).
- **`is_ready(kind, name) -> bool`**: `readiness_of(...).is_ready`, a convenience for the many call
  sites that only branch on the boolean (`select_site`, the use-time gate).

`reachable_from` returns keys, not rows; callers resolve rows via `registry.lookup`. This keeps the
graph a pure structural object that does not need to hold or age row references beyond their keys.

## Construction

The graph is built inside `finalize`, from the edge map the worklist loop already accumulates. LLD
(b) owns the pass ordering; this LLD owns the resulting structure. Construction is a pure function
of (the final `_resources`, the accumulated `all_refs`, the fold's readiness verdicts, the
enablement per node): build `_nodes` with `outbound` from `all_refs` re-keyed by source, `inbound`
from the same edges keyed by target, `enablement` and `readiness` from the fold.

**Populating `impl`.** The capability `Entry` rows do **not** carry their implementation today
(`VMPlatformEntry` holds `name`/`description`/`origin`/`references` only,
`vm_platform/__init__.py:75-79`), so the builder must obtain each capability node's impl from the
code registry (`VM_PLATFORM_REGISTRY[name]` and the three peers). This is the **whitelisted builder
exemption** (LLD b's guard grep #2 must not flag it): the graph builder reading `*_REGISTRY` to
stamp impls onto nodes during the build is the sanctioned path, distinct from a **consumer** probing
the live registry at op time. Equivalently, the capability publisher could stamp the impl onto the
`Entry` at publish time; either is acceptable, but the LLD picks **builder-reads-registry** (fewer
touched publishers, one clearly-exempt call site). The graph is frozen when `finalize` freezes.

**Outbound-edge ordering** preserves first-encountered order (the `Origin.auto_declared(source=...)`
rule depends on it, `registry.py:211-216`); the builder appends edges in walk order exactly as
`all_refs` does today.

## What moves onto the graph (and off the dataclasses)

Removing the `references` field from `DeclaredResource` (`declared_resource.py:53`) and the four
capability `Entry` types (caller inventory section E) is part of this LLD's phase-2 landing. The two
readers (`resources/inspect.py`, `secrets/inspect.py`) move to `dependents_of`. The
`_references_tuple` helper (`registry.py:462-478`) moves into the graph builder as the `inbound`
constructor. `dataclasses.replace(existing, references=...)` (`registry.py:257`) goes away; the row
no longer carries references, so the finalize "attach" pass writes them to the graph node instead of
replacing the row (except for the description-polish replace, which stays on the row).

## Acceptance

- `edges_of`/`dependents_of` on a fixture with a multi-hop chain and a diamond return exactly the
  edges the pre-refactor walk produced (a golden test against the removed `references` field).
- `reachable_from` returns the same set `collect_secrets_for` returned pre-refactor for a
  representative VM-site-with-secrets fixture (deduped, cycle-safe).
- The graph is frozen: mutating a returned tuple or re-finalizing raises.
- No consumer reads `outbound`/`inbound`/`readiness` except through the five query methods (enforced
  by the phase-6 guard, defined in LLD b).
