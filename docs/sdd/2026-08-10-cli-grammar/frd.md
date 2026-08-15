# CLI Grammar Correction, Functional Requirements

- Status: Draft for operator review
- Date: 2026-08-15
- Depends on: `cli-surface-study.md`, `prior-art-research.md`, and `future-directions.md`

## Purpose

Make type explanation, relationship inspection, and fixed-destination schema installation say what
they do. The change is intentionally narrow and gates 0.14.0.

## Settled product decisions

1. `resource describe-kind` is renamed to `resource explain`.
2. Relational inspection belongs under a new top-level `graph` namespace.
3. `resource describe` is removed. Its relationships move to `graph`; no generic replacement card is
   created because the remaining facts are covered by existing commands.
4. `--write` is path-valued. The schema set's fixed-destination action is named `--install`.
5. This is a pre-0.14 breaking cutover. Removed spellings do not receive aliases.

## Functional requirements

### Explanation

- **FR1.** `agw resource explain TARGET` shall replace `agw resource describe-kind TARGET` at the
  same resource-group level.
- **FR2.** `TARGET` shall retain the current forms: a declarable `KIND`, a capability `KIND`, or one
  capability implementation as `KIND/NAME`.
- **FR3.** Explain shall render from the existing schema and field-documentation service. Its field
  coverage, capability listing behavior, ordering, errors, and human output semantics shall not be
  broadened as part of the rename.
- **FR4.** Explain shall read no operator config, build no resource registry, open no database, and
  work when config is missing or invalid and when a contributing plugin is installed but disabled.
- **FR5.** Explain shall not add a bare kind-listing mode, field-path operand, or machine-output
  mode in this effort. `resource kinds`, `resource sample`, and `resource schema` retain their
  existing responsibilities.

### Graph

- **FR6.** The CLI shall add a top-level `agw graph` namespace with `agw graph show KIND/NAME` as
  its initial relational-view subcommand.
- **FR7.** The initial graph view shall require exactly one focal resource identity in the existing
  `KIND/NAME` grammar. Omitting the focus or supplying more than one is a usage error. The command
  shall expose the saga-required kind-filter, direction, depth, and output axes. The HLA shall pin
  defaults, validation, and interaction among those axes before planning.
- **FR8.** At minimum, the graph service shall represent every relationship currently unique to
  `resource describe`: inbound declared-resource references and live-instance usage from the kind's
  existing `instances` hook. Existing `source`, `usage`, optional `declared_by`, instance-kind, and
  instance-name facts shall not be silently discarded.
- **FR9.** Graph directions and depths shall have one documented orientation relative to the focal
  node. Direct neighbors, unbounded traversal, repeated nodes, cycles, filtering, and deterministic
  ordering shall have testable semantics.
- **FR10.** Human output shall be deterministic and terminal-readable. Machine output shall use a
  new command ID in the versioned JSON envelope and closed, typed node and edge records. Human and
  JSON renderers shall project the same safe fact service.
- **FR11.** Graph shall never disclose secret values. A secret resource may appear by identity and
  safe metadata only, under the same no-reflection boundary as current machine output.
- **FR12.** Graph shall be operationally read-only. It shall not create or migrate database state,
  repair stored process state, activate resources, make provider or remote probes, resolve secret
  values, or prompt.
- **FR13.** A query shall demand only the sources required by its selected relationships. The HLA
  shall specify database-open behavior and source-specific errors, including absent, stale,
  malformed, and unreadable live state, before implementation planning.
- **FR14.** The implementation shall reuse the frozen resource graph and existing per-kind instance
  projections without inserting database rows into `Registry`, mutating `Registry.graph`, or
  presenting an orchestration plan as complete inventory.

### Removal of resource describe

- **FR15.** `agw resource describe KIND/NAME` shall be removed in the same cutover that adds the
  graph replacement for its relationships.
- **FR16.** The `resource.describe` machine-output command and dynamic completion entries shall be
  removed. The new graph view shall receive its own command ID and schema rather than pretending to
  preserve the old card payload.
- **FR17.** No generic describe command or reduced resource card shall replace it. Documentation
  shall direct operators to `graph` for relationships and to existing inventory, diagnostics, edit,
  and kind-specific commands for all other questions.

### Writer semantics

- **FR18.** Every option named `--write` in the resulting CLI shall take an explicit path.
- **FR19.** `agw resource sample KIND --write PATH` shall retain its existing create, fill, append,
  validation, inert-document, and schema-association behavior.
- **FR20.** `agw resource schema --install` shall replace `resource schema --write`. It shall write
  the whole schema set to the existing fixed `resources/.schema/` destination, take no `KIND`, and
  retain current idempotent overwrite and reporting behavior.
- **FR21.** Stdout forms `resource schema` and `resource schema KIND` shall remain unchanged.

### Complete cutover

- **FR22.** Command help, examples, errors, hints, completions, command IDs, tests, command
  reference, current guides, embedded guide content, and the 0.14 upgrade guide shall use the new
  grammar in the same implementation series.
- **FR23.** Bash, zsh, and PowerShell completion generation and dynamic completion shall continue to
  work with broken or missing config wherever the current explanation path does.
- **FR24.** Touched resource-group help shall describe its resulting inventory, explanation,
  authoring, and editing responsibilities rather than the removed generic inspector.
- **FR25.** Directly encountered hygiene defects may ride with the change only when they concern a
  touched command contract and are cheap to fix. Unrelated CLI consistency work shall be recorded
  separately and shall not expand this SDD.

## Quality requirements

- **QR1.** Graph collection and rendering have service-level tests in addition to CLI tests.
- **QR2.** Tests assert behavior, facts, schemas, and stable identifiers. They do not assert the
  preferred wording of prose authored by this project.
- **QR3.** The implementation keeps command modules thin and preserves lazy imports and fast help
  and completion paths.
- **QR4.** The new graph code has no network dependency and no ambient write side effects.
- **QR5.** All changed documentation and completions ship with the code; no post-release repair is
  part of the definition of done.

## Acceptance criteria

1. The old `resource describe-kind`, `resource describe`, and `resource schema --write` spellings
   fail as unknown or invalid usage and are named in the 0.14 upgrade guide.
2. `resource explain` passes the current `describe-kind` behavior suite under its new command and
   identity.
3. The graph view can reproduce the inbound reference and live-instance usage facts formerly shown
   for a named resource, subject to its explicit direction and source-demand semantics.
4. Graph output is deterministic, secret-safe, read-only, and equivalent across human and JSON fact
   projections.
5. `resource schema --install` writes exactly the fixed schema set that the old boolean writer did,
   while every remaining `--write` requires a path.
6. No active reference, completion entry, machine-output fixture, or operator-facing hint points to
   a removed spelling except the upgrade guide's before-and-after explanation.
7. Repository gates and the scoped implementation, completion, documentation, and live CLI tests
   pass before the draft PR is presented for merge intent.

## Out of scope

- Generic concrete-object describe across declaration and live-instance kinds.
- New relation providers beyond the minimum graph contract approved in HLA.
- Field-level explain, explain JSON, graph path queries, DOT or Mermaid, and graph mutation.
- Renames or flag cleanup outside the four settled corrections.
- Compatibility aliases or a staged deprecation release.

## Required operator review

Approve or amend the functional boundary above. The remaining graph query choices listed in
`cli-surface-study.md` are HLA decisions; they do not reopen the settled command ownership or expand
the feature set.
