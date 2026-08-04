# Plan: Harness Integration (capability rename)

- Status: Draft
- Builds on: `frd.md`, `hla.md`

A complete, standalone rename of `harness` to `harness-integration`, introduced in 0.13.0 with its
input-compatibility removal in 0.14.0, with no functional change. The SDD stays open across both
releases. Definitions of done are objective and testable. Completed checkboxes are an immutable
record: once checked, do not edit, move, or uncheck them; if the plan changes, add new checkboxes.
LLD-level detail (`migration-strategy.md`) and per-step implementation are delegated to
`agentworks-dev`; the lead owns this plan. Every step is reviewed by `agentworks-reviewer` before
merge.

Gate for every phase:
`cd cli && uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest -q`,
plus `./scripts/lint-files.sh` and `./scripts/check-locked-sdds.sh`.

## Phase 0: Migration strategy (no code)

- [x] Author `migration-strategy.md`: the current-state inventory (from the rename survey), the
      exact new DB migration number and `RENAME COLUMN` statement, the selector shim for only
      previously valid inputs, hard errors for mixed old/new fields, the `agw resource migrate`
      changes, the aggregated-warning wording, and the 0.14.0 removal checklist.
- DoD: `migration-strategy.md` exists and is reviewed; the hard kind-slug cutover, settled selector
  spelling, valid-input matrix, and 0.14.0 removal are recorded.

## Phase 1: 0.13.0 rename (one PR)

Landing this half-done has no value, so all sub-steps ship together.

- [x] **1a Persisted state.** Add an idempotent DB migration v31 that inspects the schema and runs
      `ALTER TABLE sessions RENAME COLUMN harness_state TO harness_integration_state` only when the
      old column remains. Update `db/models.py:158`, the INSERT/UPDATE SQL and related methods in
      `db/database.py`, and `db/converters.py`. Do not edit migration v29. DoD: a migration test
      (analogous to `test_db_migration_harness_state.py`) proves an old-schema DB upgrades, an
      interrupted post-rename/pre-version-record state resumes, session rows keep their unchanged
      blob values, and insert/read round-trips on the new column; `agw session create`/`restart`
      work on the migrated DB.
- [x] **1b Kind slug.** Set the kind to `harness-integration` in `capabilities/harness/kinds.py` and
      `plugins/adapters.py`, update the capability-kind set in `resources/graph.py:369`, with no
      deprecated alias. DoD: `agw resource list --kind harness-integration` works; `--kind harness`
      and `harness/<name>` fail as unknown; `resource kinds` and shell completions expose only
      `harness-integration`; registry/kind and completion tests are updated.
- [x] **1c Selector field + manifest compatibility.** Rename the tagged-table selector key to
      `harness_integration` and the field pair to `harness_integration`/`harness_integration_config`
      (`sessions/templates.py`, `sessions/template.py`, `manifests/decode.py:76`,
      `config/loaders_sessions.py`). Both old `harness` shapes (tagged and flat) load with a single
      aggregated deprecation warning; teach `migrate/planning.py` to rewrite either to the new key;
      previously valid TOML continues to work with a warning. Mixed old/new selector or config
      fields hard-error. DoD: the complete input matrix in `migration-strategy.md` section 4 is
      covered by tests: the new key validates clean; every previously valid old form loads with
      exactly one aggregated warning; mixed forms fail; old and new declarations normalize before
      inheritance; `agw resource migrate` rewrites old YAML and TOML. Existing YAML migration
      requires a new atomic in-place rewrite path; preserve rollback behavior and registry
      equivalence.
- [ ] **1d Identifier sweep.** Rename the package `capabilities/harness/` ->
      `capabilities/harness_integration/`, the plugin `harness.py` modules ->
      `harness_integration.py`, the classes and registry/accessors per HLA section 1, and the
      threaded variables/params/comments. DoD: `ruff`, `mypy`, and the full suite pass; no import of
      the old module path remains.
- [ ] **1e CLI-visible text.** Update the list column, the describe label
      (`sessions/manager/_queries.py:352,481`, `_env.py`), and the operator-facing error/hint
      strings (`capabilities/harness/__init__.py:74,105-109`) to the new name. DoD: list/describe
      tests assert the new header/label; no canonical output says "harness" for the mechanism.
- [ ] **1f Built-in manifests + samples.** Update the shipped claude/codex `session-templates.yaml`
      and `manifests/samples/session-template.yaml` to the new `harness_integration:` selector;
      verify rather than edit agent-template manifests that contain no selector. DoD: the six
      example session-templates load present-but-disabled and validate;
      `resource sample session-template` emits the new key; live parity test passes.
- [ ] **1g Docs + files/dirs.** Move and update the capability README under the new package dir;
      update `capabilities/README.md`, `cli/README.md`, `sample-config.toml`,
      `docs/guides/resources.md`, ADR 0020, plugin-author docs, completions, and diagrams as needed.
      Leave historical `CHANGELOG.md` entries; touch the root README only for mechanism-sense usages
      and the YAML example (the operator owns the target-state prose). DoD: `lint-files.sh` clean;
      docs, samples, and generated surfaces describe reality at HEAD (renamed, no behavior change).
- [ ] **1h Residual sweep.** `rg -in harness` across the tree; justify every remaining hit
      (industry-sense prose, historical changelog, the ephemeral SDD dirs, the input-compatibility
      code). DoD: no live mechanism-sense `harness` remains outside the intentional input shims and
      justified prose.
- DoD (phase, and release readiness): full gate green; a session round-trips on the renamed column
  and selector; `agw resource migrate` moves both old YAML shapes and TOML; old kind tokens fail;
  canonical output uses the new name everywhere.

## Phase 2: 0.14.0 compatibility removal

This phase is blocked until 0.13.0 has shipped. It is not part of the 0.13.0 implementation PR. "Old
discriminator-pattern support" here means only the harness selector/config compatibility introduced
for this rename; removing generic compatibility used by unrelated capabilities is out of scope.

- [ ] Remove acceptance of the old `harness` selector, `harness_config` sibling, and TOML
      discriminator pair together with the old discriminator-pattern support. Remove their warning
      and migration-only compatibility paths where no longer needed; record the removal in the
      changelog. DoD: old inputs fail clearly, canonical 0.13.0 inputs remain unchanged, the full
      gate passes, and the residual sweep has no compatibility-code exceptions.

## Phase 3: Closeout

- [ ] Promote any load-bearing naming/decision content out of this SDD into the permanent capability
      README so the SDD stays deletable (SDD-not-permanent rule).
- [ ] Write `locked.md` summarizing the final state; land it with the last edits.
- DoD: full gate green; `locked.md` present; permanent docs reflect reality at HEAD.

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
- R5 (YAML compatibility + deprecation): Phase 1c; removal in Phase 2.
- R6 (kind-slug hard cutover): Phase 1b.
- R7 (canonical output uses new name): Phases 1e, 1f, 1g.
- D1/D2 (deprecation mechanics, selector spelling): Phase 0 (`migration-strategy.md`); removal in
  Phase 2.
