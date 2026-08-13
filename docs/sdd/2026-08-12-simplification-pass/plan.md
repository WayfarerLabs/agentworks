# Simplification Pass: Plan

Finding IDs reference [findings.md](findings.md). Requirements reference [frd.md](frd.md). Delivery
vehicle per [hla.md](hla.md): coordinated independent PRs; phase 0 merges before any other phase's
first PR.

## Phase 0: guidance and delivery (R1, R7.1)

- [ ] Probe whether harness-created worktrees (`isolation: "worktree"`) receive `paths:`-scoped
      rules; record the answer in findings.md and in the delegation section of
      `agentic-dev-process`. Done when: probe transcript summarized in findings.md, skill text
      states the verified behavior.
- [ ] Author the trust-boundary rule (R1.1) as a frontmatter-less rule under 25 lines, naming the
      three current boundaries, the interior-trust principle, the validator-names-its- boundary
      convention, and `secrets/line_safety.py` as exemplar. Done when: merged and confirmed present
      in a no-tool subagent probe.
- [ ] Author the test-value rule (R1.2) as a frontmatter-less rule under 25 lines, generalizing
      `no-prose-policing-tests` to all authored artifacts and stating derivation-over- duplication.
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
- [ ] Delete `website/tests/test_pages_workflows.py` (W1); retain at most a minimal check that the
      pages workflow still runs the double-build diff. Done when: suite green, the retained check
      (if any) contains no hardcoded workflow text.
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
- [ ] Deliver the prose-policing findings (G12, C10, D4, P6, W4, W6, and the #470 manifesto pin) to
      the prose-test-purge effort as a message file per SDD conventions. Done when: message merged
      to main via this effort's PR, prose-test-purge lead notified through the operator.
- [ ] Website contained trims: shared fixture adoption in `test_lander_404.py` (W5), exported status
      constant (W6), threshold-not-exact contrast assertions (W4), drop the Chromium duplicate (W8),
      simplify atomic-install to write-temp-and-rename (W7). Done when: suite green without Chromium
      installed.

## Phase 2: consolidations (R4)

- [ ] Database: collapse the five schema-version readers onto `inspect_schema` (D1); derive
      `SCHEMA_SENTINELS` by replaying `MIGRATIONS` (D2); extract the doctor translation helper (D6);
      mock the real-sleep deadline test like its sibling (D3). Done when: suite green, exactly one
      function reads `schema_version`, BUSY classification covered by one test per entry path.
- [ ] Secrets: single post-boundary resolve path (S6) or one recorded exception statement;
      remediation derived from failure kind (S3, S4); protocol narrowed to what implementers use
      (S3); bridge relocated to the quarantine (S8); broker dispatch on a declared flag (S9); stale
      docstrings fixed (S6, S10). Done when: suite green, `resolve_for_command` gone or its survival
      documented in one place, no generic module names a builtin source in a string literal.
- [ ] Platforms: extract the shared catalog selector (P1); factor gcp insert-reconciliation (P2,
      carefully, against the existing suite); create the gcp shared test fixture module (P5); remove
      the pass-through wrapper and table-drive the guidance text (P3). Done when: suite green, the
      three platform config modules import one selector, gcp tests define the operation fake once.
- [ ] Guide interior trust (R3.1 remainder, R3.2, R3.3): remove the adversarial layer from
      first-party parsing, keep and document the boundary skeleton per hla.md doctrine 1, route the
      manifest-description string through the retained boundary check (G1, G3, G7), fix colocation
      so packages own their content without core edits (G9), split summaries or re-register them for
      human surfaces (G10). Done when: suite green, every surviving validator's docstring names its
      boundary, a manifest description containing markup renders escaped in the index.
- [ ] Process tree: delete the three absorbed rules folding phrasings into the principles (PR4);
      merge the five collateral rules into one (PR5); collapse the review-authority statement to
      section 7a with pointers (PR3); dedupe the testing trio to one home per claim (PR1, PR10);
      trim sections 6/6a to exercised content, parking `review-requested` per operator direction
      (PR2); register and journey pass (PR7, PR8, PR9). Done when: always-on rule bytes measurably
      reduced and recorded in the PR, consistency review (per `agentic-dev-process`) run at top tier
      over the changed tree with findings triaged.

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
