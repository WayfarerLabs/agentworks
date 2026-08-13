# Simplification Pass: Functional Requirements

Effort start: 2026-08-12. Evidence base: [findings.md](findings.md), the consolidated inventory from
a seven-lane review of the 2026-08-06..12 merge window.

## Background

Six days of high-velocity agentic development merged 68 PRs (~130k added lines). A structured study
found the core models sound (the source/backend split, the declarative schema arc, the guide's
action-record model, the GCP platform shape) but identified a recurring scaffolding tax around them:
adversarial validation of first-party content, tests that police form rather than behavior,
per-effort duplication, speculative generality, and prose register drift. The study also uncovered a
rule-delivery gap: worktree-isolated subagents never receive the `paths:`-scoped rules, including
`development-principles`, so the guidance meant to prevent these patterns structurally cannot reach
the agents doing the building.

This effort removes the accumulated debt and installs the guidance that prevents its regeneration,
in that order of dependency but the reverse order of execution: guidance first.

## Operator rulings (2026-08-12)

These are settled inputs to this effort, recorded here so the requirements below are traceable:

1. **External plugins are a real, near-term goal** (order of weeks). Mechanisms appropriate for a
   genuine external-plugin boundary are kept and cleaned; mechanisms not fit for that purpose even
   if fixed are cut.
2. **0.14 is a large breaking release and the break window is open.** Contract-truth fixes ride it
   rather than accumulating compat.
3. **Migration guidance should reach assistant agents through release artifacts**, not through
   long-lived backward-compat code. The existing pipeline (release-please changelog, packaged into
   the wheel, rendered by `agw guide concept-release-notes`) is the delivery mechanism.
4. **Trust-boundary and test-value guidance land in agent artifacts before any other work in this
   pass**, so the guidance shapes the pass itself and everything after it.

## Personas

- **Contributors (human and agent)** who read main as a pattern book and copy what they find.
- **Delegated implementer and reviewer subagents**, including worktree-isolated ones, who must
  receive the project's standards through surfaces that actually load into their context.
- **Operators** running `agw`, who pay for complexity in reading time, CI time, and upgrade
  friction.
- **Future external plugin authors**, whose (real) trust boundary must be defended at the right
  places once it exists.
- **Assistant agents** (per the always-available assistance feature) navigating operators across
  breaking releases using packaged release evidence.

## Requirements

### R1: Guidance lands first, on surfaces that load

- R1.1: A compact trust-boundary rule exists and states: validation belongs at real boundaries
  (operator-authored config and manifests; external processes and their outputs; network input and
  content from outside the repository; persisted state and filesystem artifacts that cross
  executions, such as database files, backups, and restore inputs, which can be old-version,
  truncated, or modified); first-party typed values produced and consumed within one execution under
  mypy strict are trusted; every validator names the boundary it defends. `secrets/line_safety.py`
  and `db/backup.py:inspect_schema` are cited as in-repo exemplars.
- R1.2: A compact test-value rule exists and generalizes the prose-test principle to all authored
  artifacts: tests assert behavior and structure, never the form of repo-authored content (prose,
  config files, CSS tokens, workflow files, or the spelling of our own source code). Derivation
  beats duplication: when two artifacts must agree, derive one from the other and test the
  derivation.
- R1.3: Both rules are delivered through surfaces that demonstrably load for every audience,
  including worktree-isolated subagents (see R7.1). Compactness is a requirement, not a preference:
  these are unconditional context.
- R1.4: The `agentworks-dev` and `agentworks-reviewer` personas self-check for the presence of the
  always-on rules and read them from the checkout when absent, so isolation cannot silently strip
  the standards again.
- R1.5: The reviewer persona's checklist gains two questions: does this change duplicate a sibling
  subsystem's shape, and is the machinery proportionate to the payload it serves.

### R2: Form-policing and prose-policing test debt is removed

- R2.1: The secrets `phase7` enforcement corpus and the `validate_interaction_policy` runtime
  validator it exists to enforce are removed together (findings S1). Real behavioral tests in the
  corpus (the resolution-timeout suite) survive.
- R2.2: The website workflow-pinning test file is replaced (W1): its hand-rolled YAML parser and
  verbatim pins are deleted, while the real policy invariants it guarded (least-privilege
  permissions, credential non-persistence, main-only deployment, source-SHA/artifact binding, the
  double-build diff) are rewritten as focused structural checks over a proper YAML load.
- R2.3: This effort owns the full prose-assertion sweep, having absorbed the prose-test-purge effort
  (see Absorption). Every assertion on authored wording across the estate (the absorbed survey plus
  G12, C10, D4, P6, W4, W6) is deleted, replaced by a structural observable, or justified in writing
  against the rule's stated exception; pure deletions land first and in separate PRs from production
  changes.
- R2.4: Real coverage gaps found beneath the ceremony are closed in the same sweep: the vacuous
  monkeypatch test is fixed or deleted and an enum-parity test is added so a new persisted-enum
  member cannot silently render as unknown (G11).
- R2.5: Behaviors whose only observable is a sentence get a real observable (carried from the
  absorbed FRD): manager result records for repair/grant/revoke/console-sync report lines, the
  structured schema-problem seam as a supported test target, stable error codes or narrowed subtypes
  where siblings are discriminated by message, and a stable doctor-check id distinct from display
  text. Each is a small production change landing with the deletion it enables.
- R2.6: The absorbed guardrails hold: injection and redaction defenses, synthetic-fixture formatting
  tests, derived-copy parity checks, path-existence conversions, and narrow pins on external prose
  are not prose policing and are named in the plan so nobody finishes the job onto them.
  Replacements are shown to fail when behavior breaks (mutation evidence); collected-test count and
  wall time are reported before and after.

### R3: First-party code is trusted; the future plugin boundary is kept and named

- R3.1: Runtime validation of first-party content and same-execution first-party values is removed:
  the guide contract's adversarial layer as applied to in-repo content (G1), the typed-to-dict
  re-parse round trip (G2), per-call paranoia around in-repo backend return values (S2),
  annotation-equality and forbidden-override policing that duplicates or over-tightens the static
  checks (S2, C1, C5), and adversarial validation of our own parsers' outputs (S7, G6). Before any
  validator is deleted, its input's actual provenance is classified against the R1.1 boundary list;
  validators guarding persisted state or operator-supplied files stay.
- R3.2: Mechanisms genuinely fit for a future external-plugin boundary are kept, cleaned, and
  documented as that boundary: the trusted/untrusted catalog split and its fail-soft issue handling,
  registration-time constructibility checking, runtime call-shape compatibility checking (keyword
  names and kinds the framework actually calls with, which static checking cannot cover for
  dynamically loaded third-party code), and the inert-action token discipline. Each surviving
  mechanism states in its docstring which boundary it defends and that the boundary activates when
  external plugin loading lands.
- R3.3: Validation moves to where untrusted data actually flows today: the operator-authored
  manifest strings that currently bypass the guide contract (G3) are covered by the retained
  boundary checks.
- R3.4: The external-plugin trust design is recorded (what will be validated at load time, what at
  contribution time) so the future effort inherits a stated boundary rather than re-deriving one.

### R4: Duplication and half-migrations are consolidated

- R4.1: One schema-state reader: the database version/classification logic collapses onto
  `inspect_schema` (D1); `SCHEMA_SENTINELS` is derived by replaying migrations rather than
  hand-maintained (D2).
- R4.2: One post-boundary resolve path (S6), one remediation derivation (S3, S4), and the
  transitional bridge relocated into the retired-shapes quarantine (S8).
- R4.3: Cross-platform and intra-platform duplication is extracted where three or more instances
  exist: catalog selection (P1), gcp insert-reconciliation (P2), gcp test fixtures (P5), the doctor
  translation helper (D6), website test fixtures (W5).
- R4.4: Known half-migrations complete: `ResolvedSessionTemplate` adopts the tagged-table
  representation its ADR already claims (C8); permanent docs contradicting HEAD are corrected (C9);
  stale docstrings citing removed symbols are fixed (S6, S10).

### R5: 0.14 contract-truth fixes ride the open break window

- R5.1: The `[secret_config].backends` key is renamed to name what it holds (sources), with the JSON
  contract, error labels, sample config, and upgrade guide updated together (S5).
- R5.2: `TokenAcquisition` collapses to the concrete stored-token shape; the one-arm union and its
  shorthand machinery go with it (C3).
- R5.3: `canonicalize_null_companions` and its schema-widening are removed before the retired
  spelling ships as advertised-legal (C4).
- R5.4: Every compat layer carries a recorded expiry (a version or a tracked issue), or is deleted
  now; the two uninventoried compat objects join the quarantine or die (C7).
- R5.5: Speculative shape surface with zero shipped uses is removed (C1, C2, C5, S3, G8), with
  registration-time refusal (which already exists) as the designed answer for unshipped shapes.

### R6: Breaking changes publish their own migration guidance

The strategy is captured in [migration-strategy.md](migration-strategy.md): notes over shims,
delivered through the release pipeline and the self-documenting features.

- R6.1: A convention is established: every breaking change writes operator-actionable migration
  guidance into its conventional-commit breaking-change footer, so release-please accumulates it
  into the changelog that ships in the wheel and renders through the guide's release-notes topics.
- R6.2: The guide's migration topic teaches consuming that guidance (pointing agents at the packaged
  per-version evidence) rather than depending on in-code compatibility shims.
- R6.3: The 0.14 changes made by this effort (R5) are the first users of the convention.

### R7: The process tree is consolidated and delivered where it loads

- R7.1: The rule-delivery findings are addressed: load-bearing always-on guidance moves to surfaces
  that reach all audiences (frontmatter-less rules or CLAUDE.md for the compact core), personas
  self-heal per R1.4, and the harness-worktree question (findings, rule-delivery section) is
  answered and documented.
- R7.2: Redundant rules collapse: the three rules absorbed by the principles document are deleted
  with their concrete phrasings folded in (PR4); the five collateral-sync rules become one (PR5);
  the published-review-authority statement collapses to one canonical home with pointers (PR3).
- R7.3: The testing trio is deduplicated to one home per claim (PR1, PR10).
- R7.4: The stack section's narration is trimmed to operative content (PR2, PR8). The
  `review-requested` label convention is exercised (see the corrected PR2 finding) and stays;
  process machinery is trimmed only on event-history evidence of non-use, never on current-state
  queries.
- R7.5: A register and journey pass removes narration and definitional prose from operating
  instructions (PR7, PR8, PR9), holding the principles document to its own shortest-text standard.

### R8: Design-scale items spawn as their own efforts, deliberately

The following are recorded as out of scope for this pass and expected to become their own efforts,
each with the study's findings as seed input: the JSON/human single-traversal design (G5), the
`FieldShape` recursion-versus-mirror decision beyond the dead-shape removal in R5.5 (C2), the
`StructuralUnion` product decision (C4 beyond R5.3), the website validation-philosophy revision and
lander-game scope call (W2, W3, W7, W9, W10), and any sessions-owned completion of C8 if it proves
larger than R4.4 assumes.

## Non-goals

- No new user-facing features. The only operator-visible behavior changes are the sanctioned 0.14
  breaks (R5) and their migration notes (R6).
- No SDD-directory cleanup beyond the directed prose-test-purge supersession (stale-SDD deletion is
  its own deliberate process).
- No harness changes; rule-delivery fixes use in-repo surfaces only.

## Absorption of the prose-test-purge effort

By operator direction (2026-08-12 review on the seeding PR), this pass absorbs the seeded
prose-test-purge effort (`docs/sdd/2026-08-11-prose-test-purge/`, never started) rather than
coordinating with it: the estates overlap file by file, and a message handoff would leave ownership
and completion ambiguous. What carries over into this FRD: the survey basis and concentration
numbers, the three-way resolution rule (delete, replace with a structural observable, or justify in
writing) now in R2.3, the pure-deletions-first tiering (R2.3), the real-observables production
changes (R2.5), the not-prose-policing guardrails and demonstration requirements (R2.6), and the
rule-ordering requirement (satisfied: `no-prose-policing-tests` merged in #493). The absorbed SDD is
superseded in place: a supersession note and lockfile land with this PR, its FRD preserved at the
recorded SHA. The `phase7` corpus (form-policing of source shape, a distinct pathology) was always
this pass's, and is now unambiguously so. The parent saga's ledger entry for the absorbed child is
the saga lead's to record; this FRD flags it rather than editing saga artifacts.

## Acceptance

- The guidance of R1 is on main before any R2..R7 PR merges, and a probe like the one in findings.md
  confirms the rules reach a worktree-isolated subagent.
- Each requirement group lands as reviewed PRs with the standard gates; the net diff of the pass is
  strongly deletion-dominant (target: five figures of removed lines against four figures added).
- No capability regression: the full test suite passes at every merge, with removed tests replaced
  by structural equivalents only where findings name a real invariant.
- 0.14 ships with the R5 breaks, the R6 migration notes, and no new compat layers. If the release
  window closes before an R5 item lands, that item escalates to the operator for replanning and an
  FRD amendment; no compat layer is introduced as an automatic fallback.
