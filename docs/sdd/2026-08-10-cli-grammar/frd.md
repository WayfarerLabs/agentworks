# FRD: The CLI Grammar Rework

- Status: Study-phase draft; requirements await verb-contract and operator review
- Owner: `agw-ns-cli-grammer`, SDD lead; this working tree remains a review vehicle until the
  operator rules on the verb contract
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

`cli-surface-study.md` is the synthesis and proposal set. `current-surface-audit.md` records the
code-verified 71-endpoint inventory and downstream contracts. `node-model-study.md` records the
declaration/live architecture boundary. `prior-art-research.md` tests the proposed vocabulary
against primary external references. Inventory statements are facts at the recorded basis;
vocabulary judgments remain proposals until individually ruled on.

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
  - `describe` is the kind-aware card for one specific node: `KIND/NAME` where the command is not
    kind-locked and bare `NAME` where a group implies the kind. It does not own relationship
    sections and points to `graph`. The top-level command home, retention of group commands, live
    fact sets, and JSON equality boundary remain R3 decisions.
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
  JSON migration, graph truth/traversal/output, verify cardinality and output, the destructive
  confirmation matrix, node metadata, config sync, VM rekey flag timing, and explain machine output.
  None is pre-approved. The contract gives every decision a stable ID so review can approve, reject,
  or amend it without implicitly accepting its neighbors. The inspection architecture boundary is
  separately resolved through R10's HLA review.
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
- R9. **Machine output is designed before renderers.** The HLA may not assume that generic describe
  makes current `*.describe` JSON byte-identical. It records the #462-compatible disposition for
  every existing v1 command ID and payload and defines stable IDs, schemas, ordering, enum policy,
  and interaction behavior for generic describe, graph, env, and verify. It distinguishes a new
  command with its own documented schema, retirement of an old command ID, and incompatible mutation
  of an existing payload. Any incompatible v2 has an explicit compatibility period unless #462 and
  the operator jointly rule otherwise.
- R10. **The HLA compares and selects the generic inspection read boundary.** It does not mutate the
  finalized declaration registry or present the incomplete orchestration graph as system inventory.
  The HLA compares a request-scoped immutable snapshot with a constrained facade over existing
  domain queries, including their current activation and repair side effects. The operator approves
  the selected boundary through HLA review before implementation planning. The design spans
  declaration resources and live VM, workspace, agent, session, and console instances and defines
  identity, live provenance, direct and derived relation kinds, edge metadata, consistency,
  traversal, ordering, cycles, side effects, and whether domain live probes are part of describe.
- R11. **Environment output remains secret-safe.** Environment is a projection over explicitly
  eligible live anchors, not a universal node kind. Generic operand parsing and completions reject
  ineligible declarations. Machine output never exposes resolved secret-backed values by default;
  the contract explicitly rules whether `--output json` and `--resolve` are incompatible or JSON
  remains redacted. Tests use sentinel values to prove the boundary.
- R12. **Output-selection vocabulary stays coherent.** New read commands use the established
  `--output human|json` choice. Graph's deterministic human encoding, direction, depth, multi-root,
  deduplication, and cycle behavior are specified. DOT, Mermaid, or another encoding ships only with
  a named day-one consumer and does not overload `--format` to mean JSON selection.
- R13. **Completion is part of the identity design.** The HLA specifies one cross-plane `KIND/NAME`
  parser and completion source across Bash, Zsh, and PowerShell, including config failure, database
  failure, deterministic ordering, and config-free kind discovery. Enumeration is prompt-free,
  secret-resolution-free, remote-probe-free, and read-only; it skips renderer and live readiness
  work. Static Click-tree discovery does not remove the requirement to update hand-written dynamic
  mappings.
- R14. **Safety semantics are enumerated, not inferred.** The verb contract includes a confirmation
  matrix for delete, membership edits, rehome, console recreation/reinitialization, schema and
  completion installation, and completion uninstall. `--yes` only bypasses a confirmation; `--force`
  only overrides dependency protection; verb-specific destructive modes use explicit names. Ctrl-C
  is 130, and all run-a-child commands, including editor launchers, preserve child status.

## Acceptance

- AC1. The blessed verb contract exists as permanent documentation, and every command's verb, flags,
  exit codes, confirmation behavior, and output formats conform to it or carry a recorded exception.
- AC2. Each R3 decision has a dated operator ruling recorded in this SDD before its implementation
  lands.
- AC3. A contributor-facing test or check pins the cheap-to-pin conventions (kind-locked describe
  adapters share the generic fact record where retained; `--names-only`/`--output json` exclusivity;
  exit codes on representative commands), so conformance outlives this effort. JSON byte equality is
  required only where the approved versioned envelope contract permits it.
- AC4. The upgrade guide walks an operator from every removed or renamed spelling to its
  replacement; completions reflect the final tree on every supported shell.
- AC5. Full gates green; the misleading in-code prose named in R5 reads correctly at HEAD.
- AC6. Every existing v1 machine-output command has an explicit preservation, migration, or removal
  disposition. New JSON payloads are contract-tested for identity, ordering, schema version,
  interaction, and secret redaction.
- AC7. Graph tests cover the approved node and edge taxonomy, direct versus derived provenance,
  direction, depth, multiple roots, cycles, and deterministic human/JSON output from the approved
  read boundary.
- AC8. Generic identity parsing and completion tests cover every declaration and live kind on Bash,
  Zsh, and PowerShell, including unavailable config/database sources. Sentinel tests prove
  completion cannot prompt, resolve secrets, probe remotes, mutate the database, or trigger
  readiness/render work.
- AC9. A confirmation-table test suite covers every destructive command and proves that `--yes`,
  `--force`, and verb-specific kill/rebuild controls retain exactly their approved meanings.

## Constraints and non-goals

- Scope discipline per `target-state.md`'s "Requirements are priced like code": the per-kind detail
  renderer ships with exactly its day-one consumers; every graph format and option needs a named
  consumer; no mechanism without one.
- Not this effort: `vm restore` (ledger), the credential-application rename deferred to the
  harness-scope wave, guide's protocol shape (blessed as-is), and anything the R3 review declines.
- A living mutable graph, a new orchestration scheduler, historical graph snapshots, and a general
  path-query language are not required. A future why-path query may consume the approved read model
  without being designed now.
