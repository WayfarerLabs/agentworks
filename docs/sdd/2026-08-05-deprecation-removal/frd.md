# FRD: 0.14 Deprecation Removal

- Status: Draft
- Start date: 2026-08-05
- Target release: 0.14.0
- Roadmap: `docs/sdd/2026-08-04-next-steps` (this effort is wave 1 of that program; its
  deprecation-removal perspective is the source material for this FRD)
- Related SDDs: `docs/sdd/2026-08-03-harness-integration`, `docs/sdd/2026-08-04-session-resume`,
  `docs/sdd/2026-07-31-declarative-schema`

## Summary

Agentworks 0.13.0 intentionally carried several one-release compatibility paths. This effort removes
them in 0.14.0, together with a set of older aliases and dead compatibility modules, and completes
the administrative closeout of the SDDs whose removal phases it discharges.

The effort is deletion, not redesign. The generic deprecation framework survives; resource modeling
is untouched; everything coupled to the declarative-schema effort stays with that effort. 0.14.0
ships this cleanup alongside the declarative-schema phase 1 TOML sunset as one coherent
breaking-cleanup release (operator ruling, 2026-08-05).

Removing these shapes before declarative-schema phase 2 begins is a program-level constraint, not a
convenience: phase 2's per-kind modeling explicitly sequences around the legacy harness selector and
`restart_command` shims, and modeling shapes that are about to be deleted is the waste the phase
gate exists to avoid.

## Functional requirements

- **R1 (session restart command).** `agw session restart` MUST be removed: the command, its
  duplicated option signature, its per-command deprecation warning, its completion-tree entries, and
  the parity and suppression tests that exist only for the alias. Invoking the old command MUST
  produce the CLI's ordinary unknown-command error. `agw session resume` behavior MUST be unchanged.
- **R2 (restart_command field).** `restart_command` MUST no longer be recognized in session
  templates: normalization to `resume_command`, warning aggregation and template provenance used
  only by that normalization, inheritance conflict rules whose only purpose is distinguishing the
  two spellings, migration rewrites, and fixtures that exist only for the alias. A declared
  `restart_command` MUST fail as an unknown or unsupported field at the existing validation
  boundary. `resume_command` behavior MUST be unchanged.
- **R3 (harness selectors).** The pre-0.13 session-template selectors `harness` and `harness_config`
  MUST be removed from manifest loading: old-selector normalization, mixed old-and-new conflict
  branches, aggregated old-selector facts and request warnings, doctor reporting specific to the old
  selector, and old-selector migration rewrites and compatibility fixtures. Old selectors MUST fail
  as unknown fields. The canonical `harness_integration` tagged-table selector MUST be unchanged.
  This is a deletion from the current decoder, not a redesign of resource modeling.
- **R4 (older configuration aliases).** The following compatibility spellings MUST be removed:
  `[defaults].platform` (canonical: `site`), the top-level `[user]` section (canonical:
  `[operator]`), `[paths].code_workspaces` (canonical: `vscode_workspaces`), and
  `agw vm shell --provisioner` (canonical: `--platform`). Retired settings MUST fail clearly rather
  than silently falling back to defaults; in particular, an old `code_workspaces` key MUST NOT be
  ignored in a way that writes VS Code workspace files to the default directory. Where the loader
  can do so cheaply, the failure SHOULD name the canonical replacement. Canonical settings MUST keep
  their current behavior and defaults.
- **R5 (vm console).** `agw vm console` MUST be removed together with its dedicated legacy
  implementation. Reconnaissance established that it does not share code with the canonical
  `agw console` family: it is a standalone VM-wide console module, so removal retires that module
  after verifying no canonical caller depends on it. The canonical console command family MUST be
  unchanged.
- **R6 (dead Python surfaces).** The unused `UserConfig` alias, the deprecated `output.phase()`
  wrapper (zero call sites), and `env_compat.py` (no production callers; retained only by its own
  tests) MUST be removed along with their exports and alias-only tests.
- **R7 (deprecation framework narrowing).** The generic deprecation mechanism MUST remain usable:
  aggregated configuration notices, one warning per command, `--no-deprecations` suppression, doctor
  reporting of deprecation health, and the separation of deprecation notices from correctness and
  readiness issues. Expired producers and rename-specific bookkeeping (for example,
  `deprecated_harness_selectors` fields) MUST be removed. Every framework field that remains MUST
  have a current consumer.
- **R8 (docs, samples, completions).** Current operator docs, contributor docs, CLI help, sample
  config, sample manifests, and shell completions MUST teach only canonical inputs, updated in the
  same commits as the behavior changes they describe. Historical changelog entries and locked SDDs
  MUST remain unchanged.
- **R9 (upgrade path).** Release notes MUST present one coherent 0.14.0 breaking-cleanup story
  covering this effort and the phase 1 TOML sunset, MUST explicitly call out the retired surfaces
  that never warned (`code_workspaces` and `--provisioner`), and MUST direct operators to resolve
  deprecation warnings and run migration tooling on 0.13.0 before upgrading. 0.14.0 rejects retired
  inputs; it does not migrate them.
- **R10 (SDD closeouts).** The release-spanning SDDs MUST be completed and locked as part of this
  effort: `2026-08-04-session-resume` (its 0.14.0 removal and closeout phases) and
  `2026-08-03-harness-integration` (its 0.14.0 removal and closeout phases), with load-bearing
  content promoted to permanent documentation before locking. Additionally,
  `2026-03-29-proxmox-provider` and `2026-05-03-session-enhancements` MUST be verified against their
  complete checklists and locked, and `2026-03-26-mise-integration` MUST have its stale unchecked
  plan reconciled with evidence (items checked only when current code, tests, or recorded
  verification establishes delivery; deviations recorded) and then locked.
- **R11 (residual sweep).** A final residual sweep over the source tree and the installed CLI MUST
  confirm that no in-scope retired token is accepted, completed, or taught, and that every remaining
  deprecation-framework field has a live consumer.

## Personas and stories

- As an operator upgrading from 0.13.0, I resolved my deprecation warnings on 0.13.0 and my
  configuration loads unchanged on 0.14.0.
- As an operator who skipped the 0.13.0 warnings, my first 0.14.0 command fails with an error that
  tells me which key or command to change, rather than silently changing behavior.
- As an operator who never saw a warning for `code_workspaces` or `--provisioner`, the release notes
  tell me exactly what changed and what to use instead.
- As a contributor, I read session-template loading and the manifest decoder without stepping over
  compatibility branches for spellings no release accepts.
- As the roadmap maintainer, the SDD ledger shows the five affected efforts closed and locked, and
  the deprecation framework contains no residue from completed renames.

## Non-goals

- Removing TOML resource declarations or `agw resource migrate`. The declarative-schema effort owns
  the TOML sunset (phase 1, shipping in the same release) and the migrator's future (phase 2).
- Removing the generic capability discriminator compatibility (scalar `platform` plus
  `platform_config` and siblings). That shape's removal changes the shared manifest modeling
  boundary and lands with declarative-schema phase 2's tagged-union hardening.
- Deleting historical database migrations. They are upgrade contracts, not deprecated input
  surfaces.
- Removing the pre-namespaced harness-state hoist or legacy tmux-socket recovery. Those can affect
  live session rows and external processes and require their own operational cutoff decision.
- Removing or redesigning the generic deprecation framework, `--no-deprecations`, or doctor's
  deprecation reporting.
- Adding new deprecation warnings ahead of removal. The never-warned aliases are removed directly
  with clear failures and release-note coverage (see D3).

## Acceptance criteria

1. Every in-scope retired CLI token and configuration field is rejected with a clear error, and none
   appears in completions, help, docs, or samples.
2. Canonical replacements behave exactly as they did in 0.13.0, verified by the existing coverage
   for `session resume`, `resume_command`, `harness_integration`, `[defaults].site`, `[operator]`,
   `vscode_workspaces`, `--platform`, and the canonical console family.
3. An old `code_workspaces` key fails loading; no VS Code workspace file is written to the default
   directory as a silent fallback.
4. `agw doctor` no longer references removed selectors yet still reports remaining deprecations
   (TOML resource sections until phase 1 merges ordering resolves them, and the generic
   discriminator shape), and `--no-deprecations` still suppresses ambient notices.
5. The full gate passes with the alias-only tests removed and the surviving fixtures converted to
   canonical spellings.
6. The five SDDs named in R10 are locked, with promoted permanent docs verified.
7. The residual sweep (R11) is recorded in the plan with its findings.

## Decisions

- **D1 (bundling).** This cleanup and the phase 1 TOML sunset ship together in 0.14.0 as one
  breaking-cleanup release. Operator ruling, 2026-08-05.
- **D2 (sequencing).** Implementation builds on `main` after the phase 1 merge (PR #316), because
  the two efforts overlap in session-template loading, manifest decode, and migration planning code,
  and phase 1 relocated the legacy TOML loaders into the migrator.
- **D3 (no warn-first for old aliases).** The never-warned aliases (`code_workspaces`,
  `--provisioner`) are removed directly rather than given a warning release first. They predate the
  0.13 transition by long enough that a further compatibility release buys little; the mitigation is
  clear failure messages and explicit release notes (R4, R9).
- **D4 (rejection over migration).** 0.14.0 rejects retired inputs rather than auto-migrating them.
  The supported jump path for stale configurations is through 0.13.0's warnings and tooling.
- **D5 (fixture budget).** The plan MUST budget fixture conversion as first-class work rather than
  discovering it, and SHOULD prefer shared fixture helpers over per-file edits. This is phase 1's
  chief lesson.

## Open questions

- Exact error mechanism for retired TOML keys: does the config loader gain a small retired-key table
  that names replacements (nicer errors), or is generic unknown-key rejection acceptable everywhere
  the canonical validation boundary already fires? (HLA's call; R4 sets the floor.)
- Does the harness-integration SDD's residual `harness` resource-kind vocabulary need any doc-only
  cleanup beyond R8, given the kind slug itself was never an alias?
- Whether the mise-integration reconciliation (R10) surfaces gaps that convert closeout work into
  small follow-up fixes, and if so whether they ride this effort or the roadmap ledger.
