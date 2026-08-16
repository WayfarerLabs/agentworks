# CLI Grammar Correction, Functional Requirements

- Status: Approved by the operator for HLA
- Date: 2026-08-15
- Parent saga: `docs/sdd/2026-08-04-next-steps/`
- Depends on: `cli-surface-study.md`, `prior-art-research.md`, and `future-directions.md`

## Purpose

Make type explanation, generic relationship inspection, and fixed-destination schema installation
say what they do. The change is intentionally narrow and gates 0.14.0.

## Settled product decisions

1. `resource describe-kind` becomes `resource explain` without new behavior.
2. `graph show KIND/NAME` defaults to one hop in both directions and supports explicit direction and
   depth traversal.
3. `resource describe` is removed with no replacement card, alias, or compatibility shim.
4. `secret describe` keeps its contextual relationship sections and JSON fields unchanged.
5. `--write` is path-valued; the schema set's fixed-destination action is `--install`.
6. The 0.14 upgrade guide carries the breaking command map. The operator explicitly waived a
   deprecation release for the `resource describe` command shipped in 0.13.
7. No capability currently publishes multiple configuration facets, so this effort does not invent
   that descriptor shape. The command grammar leaves one stable extension seam: when the capability
   framework adds named facets, the implementation target explains all of its facets together by
   default, while the capability-kind target owns their shared vocabulary. The planned first
   consumer is harness integration.

## Functional requirements

### Explanation

- **FR1.** `agw resource explain TARGET` shall replace `agw resource describe-kind TARGET` at the
  same resource-group level.
- **FR2.** `TARGET` shall retain the current forms: any declarable or capability `KIND`, or one
  capability implementation as `KIND/NAME`. A named declarable resource is not an explanation
  target: only a capability implementation name selects a distinct schema.
- **FR3.** Explain shall render from the existing schema and field-documentation service. Its field
  coverage, capability listing behavior, ordering, errors, and human output semantics shall not be
  broadened as part of the rename. This preserves the current single-model contract without
  foreclosing settled decision 7; facet descriptor and rendering work begins with the capability
  effort that introduces the first multi-faceted implementation.
- **FR4.** Explain shall use its current config-free schema and capability resolver. It shall build
  no resource registry, open no database, and work when config is missing or invalid and when a
  contributing plugin is installed but disabled.
- **FR5.** `resource kinds`, `resource sample`, and `resource schema` retain their existing
  responsibilities. Field selection and machine output for explain are outside this effort.

### Graph

- **FR6.** The CLI shall add a top-level `agw graph` namespace with `agw graph show KIND/NAME` as
  its initial subcommand.
- **FR7.** Graph show shall require exactly one focal resource identity. It shall resolve
  `KIND/NAME` through the config-backed finalized request registry, independently of explain's
  config-free resolver. Zero or multiple operands are usage errors.
- **FR8.** Graph shall represent outbound declared references, inbound declared references, and
  live-instance usage as traversable typed edges. Declared edges shall preserve their semantic
  relationship verb, currently `uses` or `inherits`, plus `source`, `usage`, and optional
  `declared_by`; live facts shall preserve instance kind and name. Graph shall not infer a
  capability-facet relationship label from kinds or prose.
- **FR9.** The default view shall be one hop in both directions. `--direction` shall accept
  `dependencies`, `dependents`, or `both` and default to `both`. `--depth` shall accept a positive
  integer or `all` and default to `1`; `all` means the complete reachable closure in the selected
  direction. For `both`, every resource expansion may follow either incident direction; it is not
  the union of separate monotonic dependency and dependent traversals. Launch shall not include a
  kind filter or focus-optional whole-graph query. Traversal, duplicate edges, and output ordering
  shall have deterministic, cycle-safe semantics settled in HLA.
- **FR10.** Human output shall be terminal-readable. Machine output shall use a new command ID in
  the versioned JSON envelope and closed, typed node and edge records. Both renderers shall project
  the same safe fact service.
- **FR11.** Graph shall never disclose secret values. A secret resource may appear by identity and
  safe metadata only, under the same no-reflection boundary as current machine output.
- **FR12.** Graph shall be operationally read-only. It shall not create or migrate database state,
  repair stored process state, activate resources, make provider or remote probes, resolve secret
  values, or prompt.
- **FR13.** Source acquisition shall be demand-driven by the selected direction and traversal
  frontier. The database shall be demanded only when expanding a resource with remaining depth, the
  direction includes dependents, and that resource kind has a live-instance projection. A resource
  merely discovered at the depth bound shall not demand it. Live-instance nodes shall be terminal.
  HLA shall specify a read-only database open and source-specific errors for demanded projections.
- **FR14.** The implementation shall reuse the frozen resource graph and existing per-kind instance
  projections without inserting database rows into `Registry`, mutating `Registry.graph`, or
  presenting an orchestration plan as complete inventory.

### Removal of resource describe

- **FR15.** `agw resource describe KIND/NAME` shall be removed in the same cutover that adds graph
  show.
- **FR16.** The `resource.describe` machine-output command and dynamic completion entries shall be
  removed. `graph.show` receives its own schema rather than preserving the card-shaped payload.
- **FR17.** The removed non-relational facts shall keep explicit owners: resource inventory and
  kind-specific commands own identity, description, and origin; resource inventory retains readiness
  and enablement state; doctor owns diagnostic explanation; edit owns declaration location. The
  describe-only disabled sentence is derived prose, not a separate fact to migrate.
- **FR18.** `secret describe` shall retain its backend, resolution, relationship, and machine-output
  contract unchanged. Its relationships are contextual; `graph.show` is the canonical generic
  relationship contract.

### Writer semantics

- **FR19.** Every option named `--write` in the resulting CLI shall take an explicit path.
- **FR20.** `agw resource sample KIND --write PATH` shall retain its existing create, fill, append,
  validation, inert-document, and schema-association behavior.
- **FR21.** `agw resource schema --install` shall replace `resource schema --write`. It shall write
  the whole schema set to the existing fixed `resources/.schema/` destination, take no `KIND`, and
  retain current idempotent overwrite and reporting behavior.
- **FR22.** Stdout forms `resource schema` and `resource schema KIND` shall remain unchanged.

### Complete cutover

- **FR23.** Command help, examples, errors, hints, completions, command IDs, tests, command
  reference, surviving guides, and the 0.14 upgrade guide shall use the new grammar in the same
  implementation series.
- **FR24.** Bash, zsh, and PowerShell completion generation and dynamic completion shall continue to
  work with broken or missing config wherever the current explanation path does.
- **FR25.** The implementation shall coordinate with the separate guide-cleanup effort. It shall not
  preserve, update, or regenerate a guide route that effort removes as redundant.
- **FR26.** Touched resource-group help shall describe its resulting inventory, explanation,
  authoring, and editing responsibilities rather than the removed generic inspector.

## Quality requirements

- **QR1.** Graph collection and rendering have service-level tests in addition to CLI tests.
- **QR2.** Tests assert behavior, facts, schemas, and stable identifiers, not preferred prose.
- **QR3.** Command modules stay thin and preserve lazy imports and fast help and completion paths.
- **QR4.** New graph code has no network dependency and no ambient write side effects.
- **QR5.** Changed permanent documentation and completions ship with the behavior they describe.

## Acceptance criteria

1. The old `resource describe-kind`, `resource describe`, and `resource schema --write` spellings
   fail as unknown or invalid usage. The shipped `resource describe` break appears in the 0.14
   upgrade map; the two unreleased spellings are replaced silently throughout active 0.14 guidance.
2. `resource explain` passes the current `describe-kind` behavior suite under its new command and
   identity.
3. `graph show KIND/NAME` defaults to the deterministic one-hop dependencies, dependents, and live
   usage facts in human and JSON form; direction and finite or complete-closure depth controls
   return the corresponding deterministic traversal without a kind filter.
4. Graph is secret-safe and read-only, and a declaration-only query does not demand live state.
5. `resource schema --install` writes exactly the fixed schema set that the old boolean writer did,
   while every remaining `--write` requires a path.
6. `secret describe` retains its existing human relationship sections and `secret.describe` JSON
   fields.
7. No active reference, completion entry, machine-output fixture, or operator-facing hint points to
   a removed spelling except the upgrade guide and clearly historical records.
8. Repository gates and scoped implementation, completion, documentation, and live CLI tests pass
   before merge intent.

## Out of scope

- Generic concrete-object cards across declaration and live-instance kinds.
- Kind filtering, whole-graph and path queries, new relation providers, and capability-facet edge
  labels.
- Field-level explain, explain JSON, DOT, Mermaid, graph watch, and graph mutation.
- Renames or flag cleanup outside the four settled corrections.
- Compatibility aliases, warning shims, or a staged deprecation release.

HLA may settle the architecture questions in `cli-surface-study.md` without reopening these
functional boundaries.
