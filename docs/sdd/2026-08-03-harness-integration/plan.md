# Plan: Harness Integration (capability rename)

- Status: Draft
- Builds on: `frd.md`, `hla.md`

A complete, standalone rename of `harness` to `harness-integration`, shippable in the next release
as one PR, no functional change. Definitions of done are objective and testable. Completed
checkboxes are an immutable record: once checked, do not edit, move, or uncheck them; if the plan
changes, add new checkboxes. LLD-level detail (`migration-strategy.md`) and per-step implementation
are delegated to `agentworks-dev`; the lead owns this plan. Every step is reviewed by
`agentworks-reviewer` before merge.

Gate for every phase:
`cd cli && uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest -q`,
plus `./scripts/lint-files.sh` and `./scripts/check-locked-sdds.sh`.

## Phase 0: Migration strategy (no code)

- [ ] Author `migration-strategy.md`: the current-state inventory (from the rename survey), the
      exact new DB migration number and `RENAME COLUMN` statement, the kind-slug alias mechanics
      (D1), the selector-shim design reusing the legacy-flat-field hoist, the `agw resource migrate`
      changes, the aggregated-warning wording, and the next-release removal checklist.
- DoD: `migration-strategy.md` exists and is reviewed; the alias-vs-cutover and selector-spelling
  decisions (FRD D1/D2) are recorded.

## Phase 1: The rename (one PR)

Landing this half-done has no value, so all sub-steps ship together.

- [ ] **1a Persisted state.** New numbered DB migration:
      `ALTER TABLE sessions RENAME COLUMN harness_state TO harness_integration_state`. Update
      `db/models.py:158`, the INSERT/UPDATE SQL in `db/database.py`, and `db/converters.py`. Do not
      edit migration v29. DoD: a migration test (analogous to `test_db_migration_harness_state.py`)
      proves an old-schema DB upgrades, session rows keep their unchanged blob values, and
      insert/read round-trips on the new column; `agw session create`/`restart` work on the migrated
      DB.
- [ ] **1b Kind slug + alias.** Set the kind to `harness-integration` in
      `capabilities/harness/kinds.py` and `plugins/adapters.py`, update the capability-kind set in
      `resources/graph.py:369`, and add the deprecated `harness` slug alias per D1. DoD:
      `agw resource list --kind harness-integration` works; `--kind harness` and `harness/<name>`
      resolve with a deprecation notice; registry/kind tests updated.
- [ ] **1c Selector field + manifest compatibility.** Rename the tagged-table selector key to
      `harness_integration` and the field pair to `harness_integration`/`harness_integration_config`
      (`sessions/templates.py`, `sessions/template.py`, `manifests/decode.py:76`,
      `config/loaders_sessions.py`). Both old `harness` shapes (tagged and flat) load with a single
      aggregated deprecation warning; teach `migrate/planning.py` to rewrite either to the new key;
      TOML continues to work as-is. DoD: the input matrix in HLA section 2c is covered by tests: new
      key validates clean; both old shapes load with exactly one aggregated warning;
      `agw resource migrate` rewrites them; TOML round-trips.
- [ ] **1d Identifier sweep.** Rename the package `capabilities/harness/` ->
      `capabilities/harness_integration/`, the plugin `harness.py` modules ->
      `harness_integration.py`, the classes and registry/accessors per HLA section 1, and the
      threaded variables/params/comments. DoD: `ruff`, `mypy`, and the full suite pass; no import of
      the old module path remains.
- [ ] **1e CLI-visible text.** Update the list column, the describe label
      (`sessions/manager/_queries.py:352,481`, `_env.py`), and the operator-facing error/hint
      strings (`capabilities/harness/__init__.py:74,105-109`) to the new name. DoD: list/describe
      tests assert the new header/label; no canonical output says "harness" for the mechanism.
- [ ] **1f Built-in manifests + samples.** Update the shipped claude/codex `session-templates.yaml`,
      the example `agent-templates.yaml`, and `manifests/samples/session-template.yaml` to the new
      `harness_integration:` selector. DoD: the six example session-templates load
      present-but-disabled and validate; `resource sample session-template` emits the new key; live
      parity test passes.
- [ ] **1g Docs + files/dirs.** Move and update the capability README under the new package dir;
      update `capabilities/README.md`, `cli/README.md`, `docs/guides/resources.md`, ADR 0020. Leave
      historical `CHANGELOG.md` entries; touch the root README only for mechanism-sense usages and
      the YAML example (the operator owns the target-state prose). DoD: `lint-files.sh` clean; docs
      describe reality at HEAD (renamed, no behavior change).
- [ ] **1h Residual sweep.** `rg -in harness` across the tree; justify every remaining hit
      (industry-sense prose, historical changelog, the ephemeral SDD dirs, the
      deprecation-shim/alias code). DoD: no live mechanism-sense `harness` remains outside the
      intentional compatibility shims and justified prose.
- DoD (phase, and release readiness): full gate green; a session round-trips on the renamed column
  and selector; `agw resource migrate` moves both old YAML shapes and TOML; `--kind harness` warns
  and resolves; canonical output uses the new name everywhere.

## Phase 2: Closeout

- [ ] Promote any load-bearing naming/decision content out of this SDD into the permanent capability
      README so the SDD stays deletable (SDD-not-permanent rule).
- [ ] Write `locked.md` summarizing the final state; land it with the last edits.
- DoD: full gate green; `locked.md` present; permanent docs reflect reality at HEAD.

## Next release (tracked, not in this effort)

- [ ] Remove the deprecation shims: the `harness` kind-slug alias and the `harness` selector-key
      acceptance (and the aggregated warning). This is scheduled for the release after the one that
      ships this rename; record the removal in the changelog when it lands.

## Deferred to a separate SDD (not this effort)

The multi-scope expansion (user-scope and workspace-scope provisioning hooks, the per-hook scope
contract, generalizing the Claude-specific user-provisioning debt, superseding
`harness-user-provisioner`) is its own follow-up SDD and PR, started after this rename ships. It is
listed here only so the boundary is explicit.

## Traceability (FRD -> plan)

- R1 (complete rename): Phases 1a-1g, verified by 1h.
- R2 (no functional change): the full suite passing after 1d, plus 1a/1c behavioral round-trips.
- R3 (session data migration): Phase 1a.
- R4 (TOML compatibility): Phase 1c.
- R5 (YAML compatibility + deprecation): Phase 1c.
- R6 (kind-slug compatibility + deprecation): Phase 1b.
- R7 (canonical output uses new name): Phases 1e, 1f, 1g.
- D1/D2 (deprecation mechanics, selector spelling): Phase 0 (`migration-strategy.md`).
