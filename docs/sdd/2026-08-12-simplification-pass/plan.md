# Simplification Pass: Plan

Finding IDs reference [findings.md](findings.md); requirements reference [frd.md](frd.md). Wave 0
merges before any wave 1 PR; wave 1 PRs are independent and unordered; the reassessment closes the
effort. The pre-wave-1 measurements the reassessment compares against are in
[baseline.md](baseline.md).

## Wave 0: rule delivery, then the amendments (R1)

- [x] Resolve rule delivery (R1.0, issue #511): probe fresh-session, no-file-tools, and
      isolated-worktree delivery per configured target (Claude, Codex, Copilot), confirm the
      Rulesync emission for a rule without `globs`, then drop the filter from the twelve broad
      always-on rules, keeping `cli-conventions.md` narrow. Done when, either branch (FRD R1.3): (a)
      the probes are recorded on issue #511 and the twelve rules are delivered unconditionally, or
      (b) the probe shows this shape cannot work and the operator's recorded disposition places the
      full criteria text into every affected lane (charter-carried at minimum). Escalation alone
      completes nothing: until one branch holds, wave 0 stays open and wave 1 does not start.
      **Closed on branch (a), 2026-08-14**: the effort lead's own session, the first started after
      #515 merged, carried all twelve rules with full text at launch before any tool use, and
      correctly omitted `cli-conventions.md`; a worktree-isolated subagent then carried the same
      twelve the same way. That answers the isolated-worktree sub-question, since delivery is
      session-start `claudeMd` rather than path-triggered injection and worktree location decides
      nothing. Issue #511 is closed on both observations, so R1.3's gate on wave 1 is satisfied.
- [x] Amend `development-principles` with the trust-boundary doctrine (the four boundaries, interior
      trust, validator-names-its-boundary; ~10 lines) plus the principle-3 test-quality
      counterweight (R1.1), and `no-prose-policing-tests` with the authored-artifacts generalization
      (~3 sentences), one PR. Done when: merged, and the wave 1 items below cite the amendments in
      their delegation charters. **Merged in PR #515**; wave 1 charters carry both criteria in full
      rather than by citation, so a dev that loses the rule channel still has them.

## Wave 1: deletion (R2)

Nine work items, unordered by design: subtraction judged locally against the doctrines, full suite
green, R2.1 provenance gate applied, no new production types or contract changes (R2.2). How many
PRs they land as is the implementing lead's call; the saga lead's recommendation is to batch by
domain to about three (cli core; guide and machine output; website and test scaffolding) so operator
review rounds match the work rather than the item count. The website items coordinate with the
in-flight continuous-lander effort (PR #486) before starting, since they touch test files it is
actively changing.

**Sequencing (effort lead, 2026-08-14).** Several facts constrain the "unordered by design" freedom,
so the items run in four groups rather than all at once:

1. **The `phase7` item goes first and alone on `cli/`.** `validate_interaction_policy` has 175
   references across 45 files in 13 directories, so it is a repo-wide sweep that collides with every
   other cli item. It lands before the contained items start.
2. **Then the contained cli items run in parallel**, each in its own worktree, file-disjoint:
   descriptor generality (C1, C5) in `schema/`; guide dead surface with `machine_output` (G8, G2,
   G11, G6); and interior secrets validation (S2, S7).
3. **The prose-and-form sweep runs after those**, because its exclusion rule is defined against
   files the other items own; running it after they land means it inventories a settled tree. Its
   read-only decision inventory is produced up front and does not wait.
4. **The gcp fixture extraction (P5) runs after the sweep.** The sweep deletes gcp prose pins (P6)
   in the same files P5 factors, and deletions rebase cleanly under a later extraction while an
   extraction does not rebase cleanly under later deletions of what it extracted.

The sweep's decision inventory came in at **539 rows: 294 delete, 38 convert, 207 keep**, covering
about 700 assertion sites and 3,900 to 4,100 lines. It recommends cutting the sweep into five PRs by
**shape** rather than by domain, plus a sixth for the source-guard family that `hla.md` now settles:
the mechanical `match=` narrowing first, then guide and migration topics, then report lines and
hints (the judgment-heavy batch), then schema, manifests, capabilities and platforms, then
authored-artifact form policing. Batching by domain instead would put a no-judgment mechanical
change into the same review as the sweep's riskiest deletions.

**Website work is deferred** to a final wave 1 PR after #486 merges (operator ruling 8): W1, W4, W5,
W6, W8 and the sweep's website rows. Wave 1 does not close until that PR lands. The gcp fixture
extraction (P5) does not wait on #486, since it shares no files with the website work; it waits on
the sweep instead, per group 4 above.

- [x] Delete the `phase7` corpus and `validate_interaction_policy` with its 152 call sites (S1).
      Keep `test_resolution_timeout_cleanup.py` (trim its two wording pins); rename kept fixtures
      off the `phase7` name. Done when: suite green, no `phase7` path or
      `validate_interaction_policy` reference remains. **Done**: 9 files and 3,186 lines gone, 621
      cases, suite green. No kept fixture carried the `phase7` name, so no rename was owed. Both
      names survive only in SDD prose (this plan, `findings.md`, and the supersession note the work
      required on the locked `2026-08-07-secret-sources` lock, whose LLD specified the deleted
      validator as normative).
- [ ] Replace `website/tests/test_pages_workflows.py` (W1): delete the hand-rolled YAML parser and
      every verbatim pin; rewrite the real policy invariants (least-privilege permissions,
      credential non-persistence, main-only deploy, source-SHA/artifact binding, double-build diff)
      as focused checks over a proper YAML load. Done when: suite green, no hardcoded workflow text
      beyond the keys each check reads.
- [ ] Prose/form-policing sweep across the estate (absorbed survey list plus G12, C10, D4, P6, and
      the #470 manifesto pin). **This enumeration is the exclusion list and nothing else is
      excluded**: W1's workflow test, S1's corpus and wording-pin trims, W4/W6 in the contained
      website trims, and the guide item's files `cli/tests/guide/test_contract_catalog.py` and
      `cli/tests/guide/test_assessment.py`, whose prose pins belong to that item so each file has
      one owner. Any other overlap the inventory turns up is an ordering question for the lead, not
      an ownership one: a general rule keyed on what another item names or edits excluded the gcp
      files, `test_schema_adapter.py`, and `test_view.py`, which `findings.md` names _for_ the
      sweep. The sweep records each overlap it finds, keeps the file, and raises the ordering. The
      sweep's first step commits an exact decision inventory derived from the absorbed survey, one
      row per test or assertion group (a file mixing wholly-policing tests with embedded prose
      assertions gets multiple rows), each row marked delete, convert, or keep. Keep behavioral,
      structural, and security tests; delete the rest; convert to structural form only where a real
      invariant would lose its only guard. Sentence-only observables are decided case by case,
      mostly by deletion (R2.4). May land as several PRs. Done when: delete rows are gone at HEAD,
      convert rows point at the landed structural replacement, and keep rows name the invariant that
      earns the assertion.
- [ ] Delete guide dead surface and interior re-validation (G8's guide-module members and G2;
      `JsonScalar` and `VMIssueCode` live in `machine_output.py` and belong to the G6 item below);
      fix the vacuous monkeypatch test and add the persisted-enum parity test (G11, R2.3). This item
      owns `cli/tests/guide/test_contract_catalog.py` and `cli/tests/guide/test_assessment.py` in
      full, their prose pins included (both excluded from the sweep above; `test_assessment.py`
      directly tests the G8 surfaces this item deletes). Done when: suite green,
      `parse_topic_contribution` accepts only decoded data, the parity test fails on a synthetic new
      member, no reference to this item's deleted G8 members remains at HEAD, and the two owned
      files' prose pins carry the same delete/convert/justified-keep outcomes the sweep requires.
- [ ] Delete inert descriptor generality (C1, C5): `RegistryPolicy`, `kind_strategy`,
      `contract_version` plumbing, unreachable fallbacks, their pinning tests. Done when: suite
      green, four descriptors construct without the deleted fields.
- [ ] Delete `machine_output` defensive surface (G6): assert-guards on frozen dataclasses, double
      projections, identity comprehensions, the stdout retry loop; `schema_version` becomes a named
      constant. This item owns `machine_output.py` wholesale, so G8's `JsonScalar` and `VMIssueCode`
      deletions land here. Done when: suite green, JSON output byte-identical for a fixture corpus
      captured before the change, and no reference to the two deleted types remains at HEAD.
- [ ] Delete clearly-interior secrets validation (per-call type checks on in-repo backend returns
      and the annotation-equality plus forbidden-override halves of conformance, S2; lookalike and
      re-scrub checks on our own parsers' outputs, S7), keeping the constructibility and call-shape
      checks at registration with their boundary named. Done when: suite green, every surviving
      check's docstring names its boundary.
- [ ] Contained gcp test dedup (P5), after the sweep lands: a shared gcp test fixture module. Done
      when: suite green, the extended-operation fake and `_api_error` each defined once.
- [ ] Contained website test trims (W4, W5, W6, W8), after PR #486 merges (ruling 8): shared fixture
      adoption in `test_lander_404.py`, exported status constant, threshold-not-exact contrast
      assertions, drop the Chromium duplicate. Done when: suite green without Chromium installed.

## Wave 2: process and rule subtraction (R3)

Owned by the same lead as wave 1 (operator ruling 7) and running in parallel with it, in a separate
worktree, disjoint by file: `.rulesync/` and the generated rule and skill trees here, `cli/` and
`website/` there. The pre-change always-on rule byte count is 33,863 ([baseline.md](baseline.md));
R3.2 requires the after number to be lower.

- [x] Rules: delete the three principle-absorbed rules folding their concrete phrasings into the
      principles (PR4); merge the five collateral-sync rules into one (PR5); collapse the
      review-authority statement to its canonical home with pointers (PR3). Done when: net deletion,
      always-on rule bytes reported and reduced. **PR #521**: seventeen rule files to ten, 33,863
      always-on bytes to 32,215 (-1,648). The two testing-trio restatements `findings.md` PR3 counts
      ride the skills item below, which owns those files.
- [ ] Skills: consolidate the testing trio's diverged and contradictory copies to one authoritative
      home, keeping deliberate cross-perspective reinforcement (PR1, PR10, operator caution
      2026-08-12); trim journey narration and register across the process tree (PR7, PR8, PR9),
      leaving the exercised label and handoff conventions untouched (PR2 as corrected). This item
      also owns the two review-authority restatements PR3 left behind, in `integration-testing` and
      `agw-test-env`, since it owns those files. Done when: net deletion, those two restatements
      point at section 7a, and a top-tier consistency review over the changed tree per
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
