# Simplification Pass: Plan

Finding IDs reference [findings.md](findings.md); requirements reference [frd.md](frd.md). Wave 0
merges before any wave 1 PR; wave 1 PRs are independent and unordered; the reassessment closes the
effort.

## Wave 0: rule delivery, then the amendments (R1)

- [ ] Resolve rule delivery (R1.0, issue #511): probe fresh-session, no-file-tools, and
      isolated-worktree delivery per configured target (Claude, Codex, Copilot), confirm the
      Rulesync emission for a rule without `globs`, then drop the filter from the twelve broad
      always-on rules, keeping `cli-conventions.md` narrow. Done when: the probes are recorded on
      issue #511 and the twelve rules are delivered unconditionally. Escalation does not complete
      this item: if the probe shows this shape cannot work, wave 0 stays open and wave 1 does not
      start until the operator's disposition is recorded here, and that disposition must itself make
      the deletion criteria reach every affected lane (charter-carried criteria at minimum).
- [ ] Amend `development-principles` with the trust-boundary doctrine (the four boundaries, interior
      trust, validator-names-its-boundary; ~10 lines) plus the principle-3 test-quality
      counterweight (R1.1), and `no-prose-policing-tests` with the authored-artifacts generalization
      (~3 sentences), one PR. Done when: merged, and the wave 1 items below cite the amendments in
      their delegation charters.

## Wave 1: deletion (R2)

Eight work items, unordered by design: subtraction judged locally against the doctrines, full suite
green, R2.1 provenance gate applied, no new production types or contract changes (R2.2). How many
PRs they land as is the implementing lead's call; the saga lead's recommendation is to batch by
domain to about three (cli core; guide and machine output; website and test scaffolding) so operator
review rounds match the work rather than the item count. The website items coordinate with the
in-flight continuous-lander effort (PR #486) before starting, since they touch test files it is
actively changing.

- [ ] Delete the `phase7` corpus and `validate_interaction_policy` with its 152 call sites (S1).
      Keep `test_resolution_timeout_cleanup.py` (trim its two wording pins); rename kept fixtures
      off the `phase7` name. Done when: suite green, no `phase7` path or
      `validate_interaction_policy` reference remains.
- [ ] Replace `website/tests/test_pages_workflows.py` (W1): delete the hand-rolled YAML parser and
      every verbatim pin; rewrite the real policy invariants (least-privilege permissions,
      credential non-persistence, main-only deploy, source-SHA/artifact binding, double-build diff)
      as focused checks over a proper YAML load. Done when: suite green, no hardcoded workflow text
      beyond the keys each check reads.
- [ ] Prose/form-policing sweep across the estate (absorbed survey list plus G12, C10, D4, P6, and
      the #470 manifesto pin). Files owned by other items are excluded here: W1's workflow test,
      S1's corpus and wording-pin trims, and W4/W6 in contained trims. The sweep's first step
      commits an exact per-file decision inventory (delete, convert, or keep, with the file list)
      derived from the absorbed survey, and that inventory is the auditable baseline for completion.
      Keep behavioral, structural, and security tests; delete the rest; convert to structural form
      only where a real invariant would lose its only guard. Sentence-only observables are decided
      case by case, mostly by deletion (R2.4). May land as several PRs. Done when: every inventory
      row carries its decision and the delete rows are gone at HEAD.
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

The reassessment waits for waves 1 and 2 **and for the CLI grammar rewrite landing**: the saga's
`phasing.md` orders the spine wave 0, wave 1, grammar rewrite, reassessment, so this effort does not
close or lock while the rewrite is in flight. (The 0.14 contract-truth flagging that an earlier
revision scheduled here was discharged before this SDD merged: the package is dispatched as its own
task on `refactor/breaking-truth-0-14`, and the prose-test-purge absorption is recorded in the saga
ledger.)

- [ ] Write the reassessment: what became simpler in concepts, paths, and contracts; the
      retrospective numbers (lines, test counts, suite wall time, always-on bytes); the surviving
      findings; and a per-subsystem proposal or an explicit drop for each. Done when: delivered to
      the operator, after waves 1 and 2 are complete and the grammar rewrite has landed.
- [ ] Write `locked.md` once the reassessment is delivered. Remaining candidates live in the
      reassessment, not in this plan.
