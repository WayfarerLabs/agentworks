# Focused CLI Grammar Study

- Status: Revised input for HLA
- Date: 2026-08-15
- Code basis: `origin/main` at `ced59c0a`
- Scope authority: the next-steps saga plus the operator's 2026-08-15 command-ownership and
  compatibility rulings

## Outcome

This effort makes four related corrections rather than redesigning the whole CLI:

1. Rename `agw resource describe-kind TARGET` to `agw resource explain TARGET` without expanding the
   command.
2. Add `agw graph show KIND/NAME` as the generic terminal and machine-readable relationship view.
3. Remove `agw resource describe KIND/NAME`; existing commands cover its non-relational facts.
4. Replace the fixed-destination `agw resource schema --write` mode with `--install`, leaving
   path-valued `resource sample --write PATH` unchanged.

The rebased CLI has 73 leaf command endpoints. No other endpoint is in implementation scope except
for directly affected help, completion, machine-output, test, and documentation contracts.

## Current surface and disposition

| Current command                     | Current responsibility                                                   | Disposition                                     |
| ----------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------- |
| `resource describe-kind TARGET`     | Config-independent field and capability documentation                    | Rename to `resource explain`; preserve behavior |
| `resource describe KIND/NAME`       | Resource header, inbound declaration references, and live-instance usage | Remove                                          |
| `resource sample KIND --write PATH` | Write or append an inert sample at an operator-selected path             | Keep                                            |
| `resource schema [KIND] --write`    | Install the complete schema set at a fixed path                          | Rename the mode to `--install`                  |

No top-level `graph` group exists today.

### Why `resource describe` should disappear

The command has no remaining question of its own after responsibilities are assigned explicitly:

| Current fact                      | Surviving owner                                                                                                              |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Identity, description, and origin | `resource list`, `resource kinds`, `resource edit`, and kind-specific commands                                               |
| Readiness                         | `resource list` keeps the marker and JSON reason; `doctor` owns diagnostic explanation                                       |
| Enablement                        | `resource list --include-disabled` and `doctor` own status; the describe-only sentence is derived prose, not a distinct fact |
| Inbound declared dependents       | `graph show`                                                                                                                 |
| Live instances using a resource   | `graph show`                                                                                                                 |

Outbound declared dependencies were not part of `resource describe`. `graph show` adds them from the
existing frozen resource graph to complete the default both-directions neighborhood; they are a new
projection, not a migrated card fact.

Keeping a reduced card would recreate a generic inspector with no distinct operator job.

Two current overlaps need explicit treatment. `agw guide KIND/NAME` renders resource relationships,
but a separate guide-cleanup effort is removing guide routes that add nothing beyond CLI commands.
This effort does not preserve or recreate that route. `secret describe` is deliberately different:
it has unique backend-mapping and resolution-preview value, so its contextual relationship sections
and existing JSON contract remain unchanged. `graph.show` becomes the canonical generic relationship
contract, not an exclusive ban on useful kind-specific context.

## Launch grammar

```text
agw graph show KIND/NAME
    [--direction dependencies|dependents|both]
    [--depth N|all]
    [--output human|json]
```

Launch requires exactly one focal registry resource. The default view is one hop in both directions.
It contains:

- outbound declared-resource references from the focal resource;
- inbound declared-resource references, preserving `source`, `usage`, and optional `declared_by`;
- live-instance usage from the focal kind's existing `instances` hook.

`--direction dependencies` follows source-to-target references. `--direction dependents` follows
references in reverse and resource-to-live-instance usage projections. `both` is the default.
`--depth` counts edges from the focus: it accepts a positive integer and defaults to `1`; `all`
returns the complete reachable closure in the selected direction. Traversal is finite and
cycle-safe, and preserves each edge's intrinsic orientation regardless of the query direction. At
depths greater than one, `both` permits either direction at every resource expansion; it is not the
union of two monotonic traversals from the focus.

The greater-depth use is concrete rather than speculative. Starting at `vm-platform/azure-vm`, a
dependents query first reaches configured `vm-site` resources and then reaches the live VMs using
those sites. The fixed one-hop view could not answer that operator question. Launch still has no
kind filter, focus-optional whole-graph mode, or two-node path query.

Declared edges preserve the model's semantic relationship verb, currently `uses` or `inherits`, in
addition to `usage` prose and declaration provenance. Live-instance usage is a typed edge, not a
resource-graph mutation. Launch does not add capability-facet relationship labels: a future
structured qualifier must originate with the reference producer and graph may only project it, never
infer it from source kinds or prose.

### Read and safety boundary

`graph show` is config-backed: `KIND/NAME` resolves through the finalized request registry. That is
not the same resolver as config-free `resource explain KIND/NAME`, even though the token looks the
same.

The command must not create or migrate a database, repair stored runtime state, activate a resource,
resolve a secret value, make provider or remote probes, or prompt. Source acquisition is
demand-driven: the database is needed only when graph expands a resource with remaining depth and
the selected direction includes dependents for a kind with a live-instance projection. Merely
discovering that resource at the depth bound does not open the database. Live-instance nodes are
terminal and are never expanded. When an expansion does need live usage, the database is opened
through an explicitly read-only boundary with source-specific failure behavior settled in HLA.

Human output is a deterministic selected neighborhood. JSON uses the repository's versioned envelope
and typed node and edge facts.

## Explain rename boundary

`resource explain TARGET` retains the current declarable `KIND`, capability `KIND`, and capability
implementation `KIND/NAME` forms. It reads no config, builds no registry, and opens no database, so
it continues to work during configuration recovery and for an installed but disabled plugin.

The named form is deliberately limited to capability implementations: an implementation name selects
a distinct offered schema, while a declarable resource name does not. Accepting an arbitrary
concrete `KIND/NAME` would either ignore the name or turn explanation into config-backed object
inspection.

Field-level selection is future work. Because implementation names may contain dots, a future field
selector should use an option such as `--field PATH` rather than an ambiguous dotted operand. When
the first multi-facet capability arrives, its implementation explanation should render every offered
configuration facet in separate, labeled sections by default. The capability-kind explanation owns
the general facet vocabulary. Facet models must never be flattened into one schema, and config
presence must not be presented as an operational support claim. That is future capability work; this
correction preserves today's single-model output.

## Writer semantics

Only two CLI options are currently named `--write`. `resource sample --write PATH` takes an
operator-selected relative YAML path; `resource schema --write` is a boolean that installs the full
set at fixed `resources/.schema/`.

After this effort, `--write` always takes a path. `resource schema --install` names the fixed,
idempotent installation action and takes no kind. The stdout schema forms remain unchanged.

## Cutover scope

The cutover includes command registration, help, errors, active hints, three-shell completions,
machine-output IDs and fixtures, current permanent documentation, and behavioral tests. It
coordinates with the guide-cleanup effort so it neither edits a route that is being deleted nor
reintroduces that route through generated teaching.

The FRD is the single authority for implementation exclusions. Historical ADRs and completed SDDs
remain historical unless they are still active operator instructions.

## Questions handed to HLA

1. What typed node and edge kinds represent declared and live usage relations without losing
   relationship verbs or current provenance?
2. What breadth-first traversal, cycle, repeated-node, cross-edge, and deterministic ordering rules
   govern bounded and complete-closure queries?
3. How do direction and depth affect source demand, especially when a deeper frontier first reaches
   a kind with a live-instance projection?
4. How do absent, stale, malformed, and unreadable live state fail without affecting queries that do
   not demand it?
5. What stable fields form the initial `graph.show` JSON v1 contract?
