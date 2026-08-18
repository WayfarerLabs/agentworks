# Resource Show: Implementation Plan

- Status: In progress
- Date: 2026-08-17
- Requirements: `frd.md`
- Architecture: `hla.md`
- Code basis: `origin/main` at `217930fd`
- Implementation base: `8464b064`
- Delivery vehicle: one branch and PR, `feat/resource-show`

## Delivery posture

This successor is one compact PR because the service, CLI registration, machine contract,
completion, tests, and teaching collateral are one atomic user-visible command. The branch starts at
the exact remote main tip requested by the operator. The locked predecessor SDD remains historical;
this directory records the explicit successor ruling.

The PR begins as draft while implementation and review are active. It receives `saga:next-steps` so
the active saga can incorporate the successor direction without editing saga-owned artifacts here.
It becomes ready only after the full implementation, private review, live acceptance, and operator
merge intent are all present.

## Phase 1: contract and safe projection

- [x] Add the closed resource-show and readiness records in a focused resource module.
- [x] Implement shared identity resolution, category lookup, row description, stored enablement,
      stored readiness, and safe origin carry-through.
- [x] Implement the normalized declaration projector from `DeclaredResource`, `METADATA_FIELDS`,
      Pydantic JSON mode, and the canonical manifest API version.
- [x] Prove outer, metadata, and spec ordering; defaults; null omission; nested JSON conversion;
      framework-field exclusion; capability null declaration; and category/row invariants.
- [x] Prove representative secret declarations contain only lookup configuration and never resolved
      values.
- [x] Prove disabled rows project null readiness while enabled ready, not-ready, and unavailable
      verdicts retain their structural facts.

Definition of done: the presentation-free service returns one complete JSON-native fact record and
has no database, relationship, provider-operation, or secret-resolution dependency.

## Phase 2: projections and CLI wiring

- [x] Add the `resource.show` JSON v1 command identifier and exact data projector.
- [x] Add the terminal-safe human renderer, including deterministic normalized manifest YAML for
      declarable resources and a legible null declaration for capabilities.
- [x] Register `resource show REF --output human|json` in the existing resource group with lazy
      imports and no `get_db` path.
- [x] Add `resource.show/ref -> resource_refs` to the completion specification and verify all three
      shell generators.
- [x] Add service, renderer, CLI, machine-output, completion, help, malformed/unknown selector, and
      no-partial-output coverage, including silent loader flags, without prose-policing assertions.
- [x] Add structural line-injection and terminal-control tests for scalar fact lines and parseable
      sanitized YAML declaration output.

Definition of done: human and JSON commands project the same service record, completion offers the
same registry identities as edit and graph, and removed `resource describe` still does not dispatch.

## Phase 3: collateral-complete cutover

- [x] Update the machine-output schema and resource command table in `cli/command-reference.md`.
- [x] Update `cli/README.md`, `docs/guides/resources.md`, and the 0.14 upgrade map to distinguish
      show, explain, graph, doctor, edit, and secret describe.
- [x] Sweep active code, help, completions, tests, and permanent documentation for stale ownership
      claims. Leave historical ADR and locked SDD text intact.
- [x] Confirm no guide command or generated topic is added merely to duplicate the CLI command.

Definition of done: every active operator surface teaches one consistent ownership map and the
successor does not rewrite historical contracts.

## Phase 4: verification and delivery

- [x] Run focused resource-show, manifest, machine-output, completion, graph, explain, edit, and
      secret-describe tests.
- [x] Run `uv run pytest tests/ -m 'not integration'`, `uv run ruff check .`,
      `uv run ruff format --check .`, and `uv run mypy agentworks/ tests/` from `cli/`.
- [x] Run repository file lint, locked-SDD, Rulesync drift, and committed-diff guards.
- [x] Exercise real local human and JSON commands for declarable, capability, disabled/not-ready,
      malformed, and unknown selectors without live external backends.
- [ ] Obtain an equal-or-higher-tier project review and a fresh-eyes review of the exact committed
      implementation; fix every clear material finding and rerun affected gates.
- [ ] Mark the SDD complete with exact evidence, add `locked.md`, commit with the required session
      trailer, push, and update the draft PR. Move it to ready only under the process merge-intent
      rule.

## Coordination and escalation

- The active saga owns any update to its target-state ruling. This PR supplies the operator-approved
  successor artifact and uses the saga label for visibility rather than editing saga-owned files.
- The separate guide-command deletion effort owns redundant guide removal. This effort adds no guide
  route and does not restore a deleted one.
- Stop for operator direction if implementation requires source-exact manifest retention, capability
  facet projection, inheritance resolution, a database read, a provider call, secret resolution, or
  any restoration of the old card's relational fields.

## Research disposition

External prior-art research is skipped. This is a narrow successor inside an already reviewed local
grammar, and the deciding inputs are the repository's locked CLI grammar, current manifest model,
machine-output contract, and direct operator ruling. External command examples would not settle the
project-specific declaration, capability, or ownership boundaries.
