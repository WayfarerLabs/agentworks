# Simplification Pass: Plan

Finding IDs reference [findings.md](findings.md); requirements reference [frd.md](frd.md). Wave 0
merges before any wave 1 PR; wave 1 PRs are independent and unordered; the reassessment closes the
effort.

## Wave 0: bare minimum rule adjustment (R1)

- [ ] Amend `development-principles` with the trust-boundary doctrine (the four boundaries, interior
      trust, validator-names-its-boundary; ~10 lines) and `no-prose-policing-tests` with the
      authored-artifacts generalization (~3 sentences), one PR. Done when: merged, and the wave 1
      items below cite the amendments in their delegation charters.
- [ ] File the rule-delivery gap as its own tracked issue, with the probe evidence from findings.md
      (paths-scoped rules never reach out-of-tree checkouts; harness-worktree and Codex behavior
      unverified). Done when: the issue exists and findings.md links it; nothing else in this pass
      depends on it.

## Wave 1: deletion (R2)

Each item is one PR: subtraction judged locally against the doctrines, full suite green, R2.1
provenance gate applied, no new production types or contract changes (R2.2).

- [ ] Delete the `phase7` corpus and `validate_interaction_policy` with its 152 call sites (S1).
      Keep `test_resolution_timeout_cleanup.py` (trim its two wording pins); rename kept fixtures
      off the `phase7` name. Done when: suite green, no `phase7` path or
      `validate_interaction_policy` reference remains.
- [ ] Replace `website/tests/test_pages_workflows.py` (W1): delete the hand-rolled YAML parser and
      every verbatim pin; rewrite the real policy invariants (least-privilege permissions,
      credential non-persistence, main-only deploy, source-SHA/artifact binding, double-build diff)
      as focused checks over a proper YAML load. Done when: suite green, no hardcoded workflow text
      beyond the keys each check reads.
- [ ] Prose/form-policing sweep across the estate (absorbed survey list plus G12, C10, D4, P6, W4,
      W6, and the #470 manifesto pin): keep behavioral, structural, and security tests; delete the
      rest; convert to structural form only where a real invariant would lose its only guard.
      Sentence-only observables are decided case by case, mostly by deletion (R2.4). May land as
      several PRs. Done when: the survey's pure-policing class is empty at HEAD.
- [ ] Delete guide dead surface and interior re-validation (G8, G2); fix the vacuous monkeypatch
      test and add the persisted-enum parity test (G11, R2.3). Done when: suite green,
      `parse_topic_contribution` accepts only decoded data, the parity test fails on a synthetic new
      member.
- [ ] Delete inert descriptor generality (C1, C5): `RegistryPolicy`, `kind_strategy`,
      `contract_version` plumbing, unreachable fallbacks, their pinning tests. Done when: suite
      green, four descriptors construct without the deleted fields.
- [ ] Delete `machine_output` defensive surface (G6): assert-guards on frozen dataclasses, double
      projections, identity comprehensions, the stdout retry loop; `schema_version` becomes a named
      constant. Done when: suite green, JSON output byte-identical for a fixture corpus captured
      before the change.
- [ ] Delete clearly-interior secrets validation (per-call type checks on in-repo backend returns
      and the annotation-equality plus forbidden-override halves of conformance, S2; lookalike and
      re-scrub checks on our own parsers' outputs, S7), keeping the constructibility and call-shape
      checks at registration with their boundary named. Done when: suite green, every surviving
      check's docstring names its boundary.
- [ ] Contained test dedup and trims: shared fixture adoption in `test_lander_404.py` (W5), gcp
      shared test fixture module (P5), exported status constant (W6), threshold-not-exact contrast
      assertions (W4), drop the Chromium duplicate (W8). Done when: suite green without Chromium
      installed, the gcp operation fake is defined once.

## Wave 2: process and rule subtraction (R3)

- [ ] Rules: delete the three principle-absorbed rules folding their concrete phrasings into the
      principles (PR4); merge the five collateral-sync rules into one (PR5); collapse the
      review-authority statement to its canonical home with pointers (PR3). Done when: net deletion,
      always-on rule bytes reported and reduced.
- [ ] Skills: consolidate the testing trio's diverged and contradictory copies to one authoritative
      home, keeping deliberate cross-perspective reinforcement (PR1, PR10, operator caution
      2026-08-12); trim journey narration and register across the process tree (PR7, PR8, PR9),
      leaving the exercised label and handoff conventions untouched (PR2 as corrected). Done when:
      net deletion, and a top-tier consistency review over the changed tree per
      `agentic-dev-process` reports no new contradictions.

## Reassess (R4)

- [ ] Write the reassessment: what became simpler in concepts, paths, and contracts; the
      retrospective numbers (lines, test counts, suite wall time, always-on bytes); the surviving
      findings; and a per-subsystem proposal or an explicit drop for each. Done when: delivered to
      the operator.
- [ ] Flag the saga candidates through the operator: the 0.14 contract-truth package (S5, C3, C4,
      C7, the migration-notes convention) with [migration-strategy.md](migration-strategy.md) as
      seed, and the absorbed prose-test-purge ledger note. Done when: flagged; routing is the saga
      lead's.
- [ ] Write `locked.md` once waves 1 and 2 are complete, the reassessment is delivered, and the
      flags are routed. Remaining candidates live in the reassessment, not in this plan.
