# FRD: The CLI Grammar Rework (seed)

- Status: Seed for the effort lead; the saga lead reviews, the operator merges
- Date: 2026-08-10
- Saga: `docs/sdd/2026-08-04-next-steps/`
- Delivery: one cohesive child SDD (operator ruling, 2026-08-10: "this deserves to be done in one
  cohesive whole") with a study-and-design cycle before implementation; ships inside the 0.14
  breaking window or waits for the next one, never just after a cut

## Purpose

The CLI surface grew group by group and its conventions drifted: verbs mean different things under
different nouns, flags spell the same concept differently, three exit-code philosophies coexist, and
load-bearing distinctions live in the operator's head rather than in artifacts. This effort fixes
everything that does not align with a settled verb vocabulary, in one deliberate breaking release,
and writes the vocabulary down so it cannot drift silently again.

`cli-surface-study.md` in this directory is the input inventory and proposal set. Its inventory is
code-verified fact; its vocabulary judgments are proposals only, one of which has already been
withdrawn against operator intent (see the study's status header). That asymmetry defines this
effort's process requirements below.

## Requirements

- R1. **The verb contract is the first reviewed artifact.** Before any HLA or implementation, the
  effort produces the normative verb vocabulary as its own document: every verb with a one-line
  contract, the cross-cutting rules (flag semantics, exit codes, confirmation, output formats, the
  `KIND/NAME` node grammar), and the noun-plane model. It builds on the study but is not the study:
  every vocabulary or structure change is presented to the operator as a discrete decision, never
  batched inside prose. Mechanical drift fixes (flag renames within one meaning, missing filters,
  json coverage, exit-code conformance) may batch.
- R2. **Settled rulings the contract starts from** (operator, 2026-08-09/10):
  - `resource describe-kind` becomes `resource explain` (type documentation; answers on a broken
    config; grammar leaves room for field-path drill-down without building it).
  - `describe` is the kind-aware card for one specific resource: `KIND/NAME` where the command is
    not kind-locked, bare `NAME` where the group implies the kind; kind-locked spellings are thin
    sugar over the same renderer with a test pinning identical output. No relationship sections; one
    pointer line to `graph`.
  - A new top-level `agw graph` owns all relational views across declared resources and live
    instances under the one node grammar.
  - **`reinit` and `repair` are distinct verbs and both stay**: `reinit` = the resource supports
    full idempotent re-initialization; `repair` = partial idempotent reconciliation of what can be
    safely converged, where full re-initialization would destroy live work. The contract records
    both definitions verbatim and every command adopts the verb its safety semantics earn.
  - `--force` keeps exactly one meaning (dependency override on delete); the broken-session PID-kill
    becomes its own flag. `--write` always takes a path. Destructive operations confirm and accept
    `--yes` uniformly. Exit codes: 0/1/2 for the CLI's own semantics; child passthrough only for
    run-a-child commands.
- R3. **Open vocabulary decisions go to the operator individually** during the contract review, each
  with the study's recommendation and pricing: the top-level `describe` unification, the `env`
  group's shape, the `logs` ruling, the console repair/rebuild design under the corrected
  reinit/repair taxonomy, the `session create` flag reshape, `resource kinds` versus bare `explain`,
  and any further judgment the contract work surfaces. None is pre-approved.
- R4. **The conventions get a permanent documented home.** The blessed verb contract lands as
  permanent documentation (the cli-conventions rule and/or a reference doc the command surface
  cites), written so a contributor adding a command can find the verb, flag, exit-code, and
  confirmation rules without reading this SDD. The SDD remains deletable per the sdd skill.
- R5. **In-code documentation that teaches wrong conventions is fixed in the same effort.** The
  named example: `workspaces/manager` prose calling `repair` "the workspace analog of the vm reinit
  / agent reinit convergence" conflates the two verbs and misled this effort's own study. The
  implementation sweeps command docstrings and help text against the blessed contract so the code
  teaches the same vocabulary the docs define.
- R6. **Everything that does not align gets fixed or given an explicit disposition.** The study's
  deviation worklist (as ratified through R1/R3) is implemented whole; declined symmetries and
  deliberate exceptions are recorded with reasons in the permanent conventions doc, so the next
  reader distinguishes a decision from an accident.
- R7. **Breaking posture** per the saga's compatibility rulings: no compat aliases, no warn window;
  removed spellings fail as unknown commands; renamed/reshaped commands and any JSON envelope
  changes are documented in `docs/guides/upgrading-to-0.14.md` and the guide topics in the same PR
  that makes them true. Completions are first-class work (the completion tree changes shape); sample
  config and command reference ride along.
- R8. **Coordination**: the safer-migrations effort's `agw database backup/restore` and the
  installer-plugins effort's `resource enable/disable` land on their own schedules; the contract
  reserves their spellings. The #462 operational-JSON contract governs any envelope changes.

## Acceptance

- AC1. The blessed verb contract exists as permanent documentation, and every command's verb, flags,
  exit codes, confirmation behavior, and output formats conform to it or carry a recorded exception.
- AC2. Each R3 decision has a dated operator ruling recorded in this SDD before its implementation
  lands.
- AC3. A contributor-facing test or check pins the cheap-to-pin conventions (kind-locked describe
  sugar equals the generic card byte-for-byte; `--names-only`/`--output json` exclusivity; exit
  codes on representative commands), so conformance outlives this effort.
- AC4. The upgrade guide walks an operator from every removed or renamed spelling to its
  replacement; completions reflect the final tree on every supported shell.
- AC5. Full gates green; the misleading in-code prose named in R5 reads correctly at HEAD.

## Constraints and non-goals

- Scope discipline per `target-state.md`'s "Requirements are priced like code": the per-kind detail
  renderer ships with exactly its day-one consumers; every graph format and option needs a named
  consumer; no mechanism without one.
- Not this effort: `vm restore` (ledger), the credential-application rename deferred to the
  harness-scope wave, guide's protocol shape (blessed as-is), and anything the R3 review declines.

-- agw-next-steps (saga lead session)
