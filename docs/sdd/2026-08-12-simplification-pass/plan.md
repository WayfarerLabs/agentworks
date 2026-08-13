# Simplification Pass: Plan

Finding IDs reference [findings.md](findings.md). Requirements reference [frd.md](frd.md). Delivery
vehicle per [hla.md](hla.md): coordinated independent PRs; phase 0 merges before any other phase's
first PR.

## Phase 0: guidance and delivery (R1, R7.1)

- [ ] Probe whether harness-created worktrees (`isolation: "worktree"`) receive `paths:`-scoped
      rules, and how Codex delivery behaves for the same surfaces; record the answers in findings.md
      and in the delegation section of `agentic-dev-process`. Done when: probe transcripts
      summarized in findings.md, skill text states the verified behavior per harness.
- [ ] Author the trust-boundary rule (R1.1) as a frontmatter-less rule under 25 lines, naming the
      four boundaries (including persisted cross-execution state), the interior-trust principle, the
      validator-names-its-boundary convention, and `secrets/line_safety.py` plus `inspect_schema` as
      exemplars. Done when: merged and confirmed present in a no-tool subagent probe.
- [ ] Author the test-value rule (R1.2) as a frontmatter-less rule under 25 lines, generalizing
      `no-prose-policing-tests` to all authored artifacts and stating derivation over duplication.
      Done when: merged and confirmed present in a no-tool subagent probe; `no-prose-policing-tests`
      cross-references it rather than duplicating it.
- [ ] Add the persona self-heal preamble to `agentworks-dev` and `agentworks-reviewer` (R1.4) and
      the two proportionality checks to the reviewer checklist (R1.5). Done when: a
      worktree-isolated probe of each persona reports the rules in context after self-heal.

## Phase 1: deletions (R2, R5.5 dead surface, R3.1 quick wins)

Each item is one PR, pure subtraction plus named structural replacements, full suite green.

- [ ] Delete the `phase7` corpus and `validate_interaction_policy` with its 152 call sites (S1).
      Keep `test_resolution_timeout_cleanup.py` (trim its two wording pins). Rename kept fixtures
      off the `phase7` name. Done when: suite green, no `phase7` path or
      `validate_interaction_policy` reference remains, CI wall time reduction noted in the PR.
- [ ] Replace `website/tests/test_pages_workflows.py` (W1): delete the hand-rolled YAML parser and
      every verbatim script/shape pin, and rewrite the real policy invariants as focused checks over
      a proper YAML load: least-privilege workflow permissions, credential non-persistence, deploy
      triggered only from main, source-SHA/artifact binding, and the double-build diff. Done when:
      suite green, the replacement contains no hardcoded workflow text beyond the specific keys each
      policy check reads, and each check's docstring names the invariant it guards
      (integration-tester finding, 2026-08-13).
- [ ] Delete guide dead surface (G8) and the typed-to-dict re-parse round trip (G2); fix the vacuous
      monkeypatch test and add the persisted-enum parity test (G11). Done when: suite green,
      `parse_topic_contribution` accepts only decoded data, enum parity test fails on a synthetic
      new member.
- [ ] Delete inert descriptor generality (C1) and `config_for` (C5): `RegistryPolicy`,
      `kind_strategy`, `contract_version` plumbing, unreachable fallbacks, and their pinning tests.
      Done when: suite green, four descriptors construct without the deleted fields.
- [ ] Delete `machine_output` defensive surface (G6): assert-guards on frozen dataclasses, double
      projections, identity comprehensions, the stdout retry loop; name `schema_version` as a
      constant with one comment stating when it increments. Done when: suite green, JSON output
      byte-identical for a fixture corpus captured before the change.
- [ ] Absorb the prose-test-purge effort (FRD Absorption): supersession note and lockfile in
      `docs/sdd/2026-08-11-prose-test-purge/` ride the seeding PR; the saga ledger implication is
      flagged to the saga lead through the operator. Done when: the absorbed directory is locked
      with a pointer here and the operator has been asked to route the ledger note.
- [ ] Pure-deletion prose tier (R2.3, absorbed R2): verbatim content pins, required-phrase loops,
      and every wording blacklist across the estate (the absorbed survey's file list plus G12, C10,
      D4, P6, W4, W6, and the #470 manifesto pin), in PRs separate from production changes. Done
      when: the survey's pure-deletion class is empty at HEAD, collected-test count and wall time
      reported before and after.
- [ ] Riding-along prose trims (R2.3, absorbed R3): remove prose assertions riding inside legitimate
      behavioral tests, keeping the tests; cases whose only observable is a sentence route to the
      R2.5 item instead. Done when: `pytest.raises(..., match=)` sites matching authored sentences
      are eliminated or individually justified in this plan.
- [ ] Website contained trims: shared fixture adoption in `test_lander_404.py` (W5), exported status
      constant (W6), threshold-not-exact contrast assertions (W4), drop the Chromium duplicate (W8),
      simplify atomic-install to write-temp-and-rename (W7). Done when: suite green without Chromium
      installed.

## Phase 2: consolidations (R4)

- [ ] Real observables for sentence-only behaviors (R2.5, absorbed R4): manager result records for
      repair/grant/revoke/console-sync reporting, the structured schema-problem seam as a supported
      test target, stable error codes or narrowed subtypes for message-discriminated siblings, a
      stable doctor-check id. Each lands in the same PR as the deletions it enables, with mutation
      evidence that the replacement fails when the behavior breaks (R2.6). Done when: the four
      absorbed R4 items each have a landed PR or a written deferral.
- [ ] Write the sentinel-derivation LLD (D2): how sentinels derive from replaying `MIGRATIONS` when
      migrations are callables whose effects only exist after execution (the expected shape: replay
      against a scratch database and introspect, the mechanism the drift test already uses), and
      where backup qualification's boundary validation stays distinct from interior classification.
      Done when: LLD reviewed before the database consolidation PR opens.
- [ ] Database: consolidate schema-state classification onto `inspect_schema` as the single
      authority (D1), with entry paths (read-only open, writable open, migrate, prepare/open lock
      paths) translating its `SchemaState` rather than re-deriving it, and BUSY preserved at every
      path; backup validation of arbitrary operator-supplied files remains boundary work (HLA
      doctrine 1, boundary 4) and may share the low-level reader without sharing failure semantics.
      Derive `SCHEMA_SENTINELS` per the LLD (D2); extract the doctor translation helper (D6); mock
      the real-sleep deadline test like its sibling (D3). Done when: suite green, exactly one
      classification authority exists, BUSY covered by one test per entry path, sentinels have no
      hand-maintained table.
- [ ] Write the secrets-consolidation LLD: the narrowed client protocol (S3), the single resolve
      path (S6), which conformance checks survive as the call-shape gate per HLA doctrine 1
      (integration-tester finding, 2026-08-13), and the S7 deletions with each input's provenance
      classified. Done when: LLD reviewed before the secrets consolidation PR opens.
- [ ] Secrets: single post-boundary resolve path (S6) or one recorded exception statement;
      remediation derived from failure kind (S3, S4); protocol narrowed to what implementers use
      (S3); interior re-validation of our own parsers' outputs removed per the LLD's provenance
      classification (S7); bridge relocated to the quarantine (S8); broker dispatch on a declared
      flag (S9); stale docstrings fixed (S6, S10). Done when: suite green, `resolve_for_command`
      gone or its survival documented in one place, no generic module names a builtin source in a
      string literal, the call-shape registration check retained and its boundary named.
- [ ] Platforms: extract the shared catalog selector (P1); factor gcp insert-reconciliation (P2,
      carefully, against the existing suite); create the gcp shared test fixture module (P5); remove
      the pass-through wrapper and table-drive the guidance text (P3). Done when: suite green, the
      three platform config modules import one selector, gcp tests define the operation fake once.
- [ ] Write the guide boundary-surgery LLD: which contract checks survive as the future contribution
      boundary, where the manifest-description string enters validation, and the provenance
      classification for each deleted check (G1, G3, G7). Done when: LLD reviewed before the guide
      consolidation PR opens.
- [ ] Guide interior trust (R3.1 remainder, R3.2, R3.3): remove the adversarial layer from
      first-party parsing per the LLD, keep and document the boundary skeleton per hla.md doctrine
      1, route the manifest-description string through the retained boundary check (G1, G3, G7), fix
      colocation so packages own their content without core edits (G9), split summaries or
      re-register them for human surfaces (G10). Done when: suite green, every surviving validator's
      docstring names its boundary, a manifest description containing markup renders escaped in the
      index.
- [ ] Process tree: delete the three absorbed rules folding phrasings into the principles (PR4);
      merge the five collateral rules into one (PR5); collapse the review-authority statement to
      section 7a with pointers (PR3); consolidate the testing trio's diverged and contradictory
      copies to one authoritative home while keeping deliberate cross-perspective reinforcement
      (PR1, PR10, operator caution 2026-08-12); trim the stack section's narration, leaving the
      exercised `review-requested` convention untouched (PR2 as corrected); register and journey
      pass (PR7, PR8, PR9). Done when: always-on rule bytes measurably reduced and recorded in the
      PR, consistency review (per `agentic-dev-process`) run at top tier over the changed tree with
      findings triaged.

## Phase 3: 0.14 reshapes (R5, R6), each PR writing its migration-guidance footer

- [ ] Establish the migration-notes convention (R6.1, R6.2): a short section in `CONTRIBUTING.md` on
      writing breaking-change footers as operator-actionable guidance, and the guide migration topic
      updated to point at packaged release evidence. Done when: the convention text is on main
      before the first R5 PR merges.
- [ ] Rename `[secret_config].backends` to name sources (S5, R5.1), updating loader, JSON keys,
      error labels, sample config, and the 0.14 upgrade guide together. Done when: suite green, no
      reconciling it-says-backends-but-means-sources prose remains.
- [ ] Collapse `TokenAcquisition` to the concrete shape and remove the one-arm union machinery (C3,
      R5.2). Done when: suite green, one spelling per fact in the token config, shorthand mechanism
      deleted or reduced to its shipped uses.
- [ ] Remove `canonicalize_null_companions` and its schema widening (C4, R5.3). Done when: the
      emitted schema no longer advertises the retired spelling.
- [ ] Compat expiry sweep (C7, R5.4): every entry in and outside `retired_shapes.py` gets a recorded
      expiry or is deleted now; `TokenSourcedConfig` and the two uninventoried objects join the
      quarantine or die; `HOST_PROBING_CAPABILITY_KINDS` moves onto descriptors or its IOU becomes a
      tracked issue. Done when: grep for retired-shape mechanisms finds only quarantined,
      expiry-carrying entries.
- [ ] Delete dead `FieldShape` mirror halves with zero shipped uses (C2, R5.5), relying on
      registration-time refusal for unshipped shapes; split `_shape.py` under the size ceiling. Done
      when: suite green, every remaining shape attribute has a shipped consumer named in the PR.
- [ ] `ResolvedSessionTemplate` adopts the tagged-table representation (C8, R4.4), or the item is
      spawned to a sessions effort if it exceeds its budget (R8). Done when: either the ADR claim is
      true at HEAD or a seeded brief exists.

## Phase 4: promotion and closeout

- [ ] Record the external-plugin trust design (R3.4) in a permanent home (`docs/arch/` or a new
      ADR), promoted out of this SDD. Done when: the permanent doc stands alone with no SDD
      references.
- [ ] Correct permanent docs contradicting HEAD (C9): ADR 0020 and 0018 amendments, vm-platform
      README, stale docstrings. Done when: cited contradictions resolved.
- [ ] Deliver seed messages for spawned efforts (R8): JSON/human traversal design (G5),
      StructuralUnion product decision (C4), website validation philosophy and lander scope (W2, W3,
      W9, W10). Done when: message files merged, operator notified.
- [ ] Route the D5 judgment call (safer-migrations locked.md supersession note for #504) to the
      operator with both options stated. Done when: operator has decided; note written or explicitly
      declined.
- [ ] Post-pass measurement: re-run the window arithmetic (code/test/docs line deltas, always-on
      rule bytes, CI wall time) and record the before/after in this directory. Done when: the
      numbers are in the closeout summary.
- [ ] Write `locked.md` when current state matches this plan and all spawned seeds are delivered.

## Explicitly deferred (R8)

Recorded here so their absence from the phases above reads as a decision, not an omission: the
JSON/human single-traversal redesign (G5), FieldShape recursion (C2 beyond dead-shape deletion),
StructuralUnion removal decision (C4 beyond the compat flag), website DOM-validation philosophy and
the lander game's scope (W2, W3, W7 beyond the contained trim, W9, W10), gcp error-taxonomy collapse
(P4, pending the operator's dispatch-consumer answer), and the two-phase lock protocol itself (D3
beyond the test-cost trim).
