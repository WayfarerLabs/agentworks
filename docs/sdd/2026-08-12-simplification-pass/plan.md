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

**Website work is deferred** until #486 merges (operator ruling 8). It lands as two PRs rather than
one (effort lead, 2026-08-16): the website items themselves (W1, W4, W5, W6), and the sweep's
website rows, which ride the sweep so it stays batched by shape rather than splitting one mechanical
change across two reviews. Wave 1 does not close until both land. W8 has left this scope entirely,
for the reasons its item records. The gcp fixture extraction (P5) does not wait on #486, since it
shares no files with the website work; it waits on the sweep instead, per group 4 above.

- [x] Delete the `phase7` corpus and `validate_interaction_policy` with its 152 call sites (S1).
      Keep `test_resolution_timeout_cleanup.py` (trim its two wording pins); rename kept fixtures
      off the `phase7` name. Done when: suite green, no `phase7` path or
      `validate_interaction_policy` reference remains. **Done**: 9 files and 3,186 lines gone, 621
      cases, suite green; no kept fixture carried the `phase7` name, so no rename was owed. The
      interior deletion stands in full, but review found the value is not interior everywhere: the
      manifest services take `interaction` from callers outside our type checking, and a probe drove
      a non-enum `"refuse"` into real backend execution. That is the caller-supplied trust boundary
      the doctrine gained in #533, after the wave 0 amendment above merged with four. The item
      therefore also added `require_exact_interaction_policy`, called in two places for two reasons.
      `ResolutionPolicy.__post_init__` calls it, so **no policy can be constructed from an unchecked
      `interaction`**, audited by `grep -rn "ResolutionPolicy(" cli/agentworks/`, which is six
      constructions in six functions today and covers every path to `resolve_batch`. Eight call
      sites remain outside the constructor: five check on arrival at the published service functions
      and at `Resolver.__init__`, and three sit at entry points that do consequential work before
      reaching a resolver at all, which buys position rather than presence, because a rejected
      policy must leave nothing behind and so the check runs before any state change. Those three
      are `delete_vm` (whose resolver sits inside a best-effort span that swallowed the rejection
      and completed the delete with the backend delete skipped, the #329 orphaning), `reinit_agent`
      (which persists a template re-point first), and `rehome_workspace` (which opens SSH transports
      first). The deleted names survive in SDD prose only, and this is the whole list: both names in
      this plan, in `findings.md`, and in the supersession note the work required on the locked
      `2026-08-07-secret-sources` lock; `phase7` alone in `hla.md`; and
      `validate_interaction_policy` alone in that lock's `operator-surfaces-lld.md`, which the
      supersession note supersedes.
- [x] Replace `website/tests/test_pages_workflows.py` (W1): delete the hand-rolled YAML parser and
      every verbatim pin; rewrite the real policy invariants (least-privilege permissions,
      credential non-persistence, main-only deploy, source-SHA/artifact binding, double-build diff)
      as focused checks over a proper YAML load. Done when: suite green, no hardcoded workflow text
      beyond the keys each check reads. **Done**, after four rounds that went the wrong way and one
      that corrected them. The final file is 4 checks in 108 lines: write permissions confined to
      the deployment job, that job running only `actions/deploy-pages`, its dependency on the
      artifact producer and the `github-pages` environment, and `ci-success` consuming a real result
      for every job it requires. Each derives rather than pins.

      **The threat model is accidental regression in the workflow's shape, not adversarial
      tampering.** Stating it once is what stops the next reader rediscovering the open-ended class.
      These tests live in the same tree as the workflows and change in the same commits, so anything
      that can edit `pages.yml` can edit them beside it; they are not a security root against a
      hostile contributor. What the workflows guarantee, they guarantee by running: the website
      suites, the two-build determinism diff, and the source-state verifiers each fail the job on
      their own, and the `github-pages` environment's restrictions are configured on GitHub.

      **The item got this wrong for four rounds and the record should say so plainly.** It replaced
      the deleted file's verbatim pins with derivations, which fixed the form while preserving the
      fundamental mistake, then grew to 635 lines policing a 134-line workflow. Each round closed one
      expression of "repository-authored code can affect deployment" and the next round found
      another, because that class has no end. The integration tester's findings were **all valid
      under the guarantee the file claimed**; the guarantee was too broad. Removed with the rest: the
      env-key, build-command, and one-invocation allowlists, the shell and `BASH_ENV` checks, the
      step-key bans, credential non-persistence, first-party actions, the source-SHA and artifact
      binding, and every check that executed a script.

      **The one durable correction was the location.** A proper YAML load cannot happen in the
      website suite, which runs `python3 -m unittest` on a runner with no package installation step,
      so the file lives at `cli/tests/test_workflow_policy.py` beside
      `cli/tests/assistance/test_contract.py`, which reads repository-root artifacts for the same
      reason.

      Three method lessons survive, all about evidence rather than workflows. A deleted policing
      file's mutation corpus is recoverable from history on demand by parsing the old file at the
      merge base, and running it against the replacement before the deletion merges is what showed
      this branch briefly behind `main`. Author-selected mutations cannot establish coverage, since
      they only show each check bites what it was written to bite; two review lanes walked through
      policies no check owned. And a mitigation cited to justify leaving a gap has to be executed
      rather than asserted: the one offered here, that a drift verifier stood behind fail-fast, was
      false on inspection.

      Two further tester findings arrived after the narrowing, both deliberate-actor shaped
      (overwriting built output, replacing the gate script) and so outside the promise this file now
      makes. The accident-grade property behind the first was taken: the gate check claimed the
      results were consumed when it holds only that they are wired, and it is narrowed, since two
      claims that outran their checks were already corrected here. The second was not taken and is
      returned to the operator, because its premise does not hold: the pre-upload source verifier
      already sits between the determinism diff and the upload, so "nothing between them" is false at
      HEAD, and separating an inserted post-processing step from the verifier needs that step
      identified by its environment key, which is the machinery this round removed.

- [ ] Prose/form-policing sweep across the estate (absorbed survey list plus G12, C10, D4, P6, and
      the #470 manifesto pin). **This enumeration is the exclusion list and nothing else is
      excluded**: W1's workflow test, S1's corpus and wording-pin trims, W4/W6 in the contained
      website trims, the guide item's files `cli/tests/guide/test_contract_catalog.py` and
      `cli/tests/guide/test_assessment.py`, whose prose pins belong to that item so each file has
      one owner, and `cli/tests/test_workflow_policy.py`, which W1 wrote and owns despite sitting
      under `cli/tests/` (added 2026-08-16, since a path-keyed inventory would otherwise claim it).
      Any other overlap the inventory turns up is an ordering question for the lead, not an
      ownership one: a general rule keyed on what another item names or edits excluded the gcp
      files, `test_schema_adapter.py`, and `test_view.py`, which `findings.md` names _for_ the
      sweep. The sweep records each overlap it finds, keeps the file, and raises the ordering.

      **Three website overlaps raised 2026-08-16 by the contained-trims work, recorded here rather
      than in a PR body so the sweep's executor finds them.** First, W5 is not on the exclusion list
      above, which names W4 and W6 only, yet `test_lander_404.py` is W5's file and carries sweep
      rows of its own; the W5 change rewrote about 90 lines of it by deleting duplicated fixtures,
      so the sweep should re-read it rather than inventory it from the pre-#486 basis. Second,
      `website/tests/test_site_documents.py` has two owners: the W4 palette rows and the W8 browser
      row belong to the trims item, while the CSS declaration list and the four-word
      `fake_terminal` blacklist in `test_shared_css_pins_tokens_reflow_focus_and_terminal_cues` are
      sweep rows and were deliberately left untouched. That blacklist is the shape
      `no-prose-policing-tests` calls worse than useless, so it is a delete row, not a convert row.
      Third, this plan expected the sweep's website rows in the same PR as the website work; they
      are not in it, so they remain wholly the sweep's.

      The sweep's first step commits an exact decision inventory derived from the absorbed survey, one
      row per test or assertion group (a file mixing wholly-policing tests with embedded prose
      assertions gets multiple rows), each row marked delete, convert, or keep. Keep behavioral,
      structural, and security tests; delete the rest; convert to structural form only where a real
      invariant would lose its only guard. Sentence-only observables are decided case by case,
      mostly by deletion (R2.4). May land as several PRs. Done when: delete rows are gone at HEAD,
      convert rows point at the landed structural replacement, and keep rows name the invariant that
      earns the assertion.

      **Step one is done**: [sweep-inventory.md](sweep-inventory.md), 1,160 rows (642 delete, 191
      convert, 327 keep), batched into six PRs by shape, with group 3 cut into four sub-batches by
      subsystem because 377 rows is too big for one review round. That artifact is deleted when the
      sweep closes; its header says so.

      **Three corrections to this item's own enumeration, verified at HEAD 2026-08-16.** Two of the
      files it names for the sweep no longer exist: `test_schema_adapter.py` and
      `guide/test_view.py` were both deleted by PR `8043d438`, "remove command-owned fact views".
      The same commit retired **the #470 manifesto pin** that the first line of this item lists as
      part of the estate, replacing the verbatim block-text comparison with a structural link
      assertion, so that part of the estate was already gone before the sweep started. The
      exclusion list itself is unaffected and still exhaustive; what changed is the inventory's
      expected yield, and `findings.md` G12 carries the detail. The inventory found ten overlaps in
      all, including the three website ones above, all recorded in its own overlaps section rather
      than restated here.

- [x] Delete guide dead surface and interior re-validation (G8's guide-module members and G2;
      `JsonScalar` lives in `machine_output.py` and belongs to the G6 item below); fix the vacuous
      monkeypatch test and add the persisted-enum parity test (G11, R2.3). This item owns
      `cli/tests/guide/test_contract_catalog.py` and `cli/tests/guide/test_assessment.py` in full,
      their prose pins included (both excluded from the sweep above; `test_assessment.py` directly
      tests the G8 surfaces this item deletes). Done when: suite green, `parse_topic_contribution`
      accepts only decoded data, the parity test fails on a synthetic new member, no reference to
      this item's deleted G8 members remains at HEAD, and the two owned files' prose pins carry the
      same delete/convert/justified-keep outcomes the sweep requires. **Done** (PR #548): every G8
      member gone with no remaining references, both halves of the G2 round trip gone
      (`parse_topic_contribution` and `_action_record_value`), and G11's vacuous monkeypatch
      replaced by a parity check that walks the real enums and fails on a synthetic member.
      `VMIssueCode` was a plan correction: it lives at `vms/manager/inspect.py:56`, not in
      `machine_output.py`, and the deletion landed there scoped to that symbol alone because the
      file also carries G5, which is out of scope for this pass. **G3 is resolved here** and no
      later item owns it: the manifest resource `description` is operator-authored text, so boundary
      1, and the round trip being deleted was the only thing applying the summary byte cap and the
      framework-delimiter screen to it, so the check moved to where the text enters, matching what
      `_schema_topic` already did. G3 overstated the gap and so did this PR's first account of it:
      on `main` the check ran one step late, at `view.py:202` inside `build_guide_view`, and was
      absent only on the degraded `system_error` path that never builds a view. The consequence is
      ruled and deliberate: a description that fails the check now fails the whole `agw guide`
      invocation rather than degrading one target, which is the loud-refusal-at-entry posture
      boundary 1 asks for, recorded here so the reassessment reads it as a decision rather than an
      accident.
- [x] Delete inert descriptor generality (C1, C5): `RegistryPolicy`, `kind_strategy`, unreachable
      fallbacks, their pinning tests. **`contract_version` is not in this item and is not a deletion
      target** (operator ruling 12): it is required, it is checked at registration, and PR #546
      verified the check by mutation. Done when: suite green, four descriptors construct without the
      deleted fields, and `contract_version` still gates registration. **Done**: `RegistryPolicy`
      and both its consumer branches, `kind_strategy`, `manifest_section`'s optionality with the
      five narrowing guards it forced, `offered_model`'s two `getattr` fallbacks, and five more
      sites in the same family the inventory never named (`_declared_model`'s fallback, two
      conformance re-checks, and two of the four dead `config_schema.discriminator` branches), plus
      a reserved-field comment and two documentation claims that had outlived what they described.
      `contract_version` still refuses a mismatched impl, re-verified by the mutation PR #546 used.
      Collected tests 7361 to 7358 (`pytest --collect-only`; an earlier note in this item quoted the
      selected count, so the reassessment should cite the collected one). Every site was controlled
      individually before deletion, by removal or by an assertion proving the branch never entered.

      **C1 and C5 carry the corrections this item's classification produced**: on
      `discriminator`/`input_domain`, on `manifest_section`, and on the `config_for()` hook that is
      an R2.2 set-aside rather than a deletion. findings.md is their home and this item does not
      restate them.

      Three things are recorded here because they are decisions rather than corrections.
      `capability_class` keeps the unregistered-kind refusal it had been performing incidentally
      through `descriptor_for`, for sibling consistency with `rows_of` and explicitly **not** as an
      R2.3 replacement: removing it fails only the test this item added. Two of the four dead
      discriminator branches stay, because `capability_config_union`'s raise is the one deletion
      mypy rejects (`ConfigContract.discriminator` genuinely admits `None`, and `mapping_schema` is
      a live instance of it, so the raise enforces an invariant the type does not) and
      `selected_name`'s orphans a parameter in four signatures. And **deleting `offered_model`'s
      fallback was a reachable regression, found by the integration tester**: `register_plugin` is
      exported, it admitted a class whose `config_for` was not callable, and `agw resource sample
      vm-site` then died on a raw `TypeError`. Fixed at the registration seam, where the wave's
      charter puts call-shape checks, with the tester's reproduction as its test. The enumeration
      that missed it had walked the descriptor table exhaustively and never asked who supplies an
      impl class.

- [x] Delete `machine_output` defensive surface (G6): assert-guards on frozen dataclasses, double
      projections, identity comprehensions; `schema_version` becomes a named constant. This item
      owns `machine_output.py` wholesale, so G8's `JsonScalar` deletion lands here. Done when: suite
      green, JSON output byte-identical for a fixture corpus captured before the change, and no
      reference to the deleted type remains at HEAD. **Done** (PR #548): the named constant, the
      double projections, the identity comprehensions, and an unreachable trailing `raise` replaced
      by `assert_never`, byte-identical across the captured corpus. The item carries two corrections
      forward. **The stdout retry loop is struck from this item and from G6**, which named it as
      defensive surface on a false premise: `sys.stdout.buffer` is a `BufferedWriter` only when
      stdout is buffered, and under `python -u` or `PYTHONUNBUFFERED` it is a raw `FileIO` whose
      `write` returns a short count instead of raising, so a single unchecked write hands a machine
      consumer truncated JSON at exit code 0. It was deleted here on this item's instruction, held
      by three published review lanes, and restored byte-identical with the short-write test the
      invariant never had. No later item deletes it. Second, `project_origin`'s variant guards and
      two nested defensive-copy assertions were deleted and restored: removing the guards made
      `project_origin` the lone undefended consumer of `Origin`'s variant contract, and the real
      inconsistency (seven consumer files defending one contract three ways) is filed as #547 rather
      than fixed in passing.
- [x] Delete clearly-interior secrets validation (per-call type checks on in-repo backend returns
      and the annotation-equality plus forbidden-override halves of conformance, S2; lookalike and
      re-scrub checks on our own parsers' outputs, S7), keeping the constructibility and call-shape
      checks at registration with their boundary named. Done when: suite green, every surviving
      check's docstring names its boundary. **Done** (PR #546): suite green, and every surviving
      check names its boundary. The per-call type checks around in-repo backend returns are gone,
      with conformance's annotation-equality and return-annotation comparisons and the `@final` MRO
      walk; what stays at registration is constructibility, contract version, and call shape, named
      to the channel that actually exists (`plugins.register_plugin` is in the package's public API
      and seats whatever class it is handed) rather than to a future loader. The headline -739 is a
      test number and should not be reported alone: production grew +34 lines, 166 of the 305 added
      being the chartered boundary prose.

      **Both halves of S7 were overturned by executing them, so both checks stay**, and findings.md
      carries the corrections. The lookalike claim is false: `manifests/loader.py`'s `_StrictLoader`
      really does emit `datetime.date`, `bytes`, and `set` into a secret's `backend_mappings` from
      operator-authored YAML tags, and `require_exact_json_value` is the only thing that rejects
      them. The re-scrub claim is false one layer down: `validate_name("envvar\n")` returns cleanly,
      because `NAME_RE` anchors with `$` under `re.match`, so the source scrubs were deleted and
      restored with tests that fail when #542 retires the defect rather than passing silently. A
      pass finding disproved by running it is the evidence standard working against the pass itself.

      The classification also added two checks rather than removing them, because it found them
      missing: `preview_operation_resolution` and `predict_resolution` now check their own
      interaction policy, which makes #523's rule total instead of true by the ordering of unrelated
      call sites. Two undeclared drops are recorded here so the reassessment does not have to
      rediscover them: the deleted containment tests included redaction guards, whose invariant now
      holds because a backend exception escapes `resolve_batch` and ends the command instead of
      becoming a row (verified by execution); and dropping the returned-mapping check turns a
      wrong-name mapping from loud-and-terminal into silently soft-missing into the next source,
      which no producer can do today but is a fail-open direction change. The review round added the
      two R2.3 regression tests for the retained diagnostic-name screens on `ResolutionOutcome` and
      `ResolutionPreview`, which the integration tester showed nothing could detect the removal of.
      Deferred root causes are #542, #544, and #545.

- [ ] Contained gcp test dedup (P5), after the sweep lands: a shared gcp test fixture module. Done
      when: suite green, the extended-operation fake and `_api_error` each defined once.
- [x] Contained website test trims (W4, W5, W6), after PR #486 merges (ruling 8): shared fixture
      adoption in `test_lander_404.py`, exported status constant, threshold-not-exact contrast
      assertions. **W8 has left this item** and folds into W10's lander-scope decision for the
      reassessment (effort lead, 2026-08-16, accepting the evidence below): its premise did not
      survive execution and this item's done-condition was unreachable, so keeping it here would
      hold three finished trims behind a deletion that should not happen. Done when: W4, W5, and W6
      land with the suite green. **Done**: W5 removed the last website test file carrying its own
      builder loader, output manifest, HTML parser, and contrast helpers. W4 converted the seven
      three-decimal ratio comparisons into WCAG inequalities (4.5 text, 3.0 non-text) read out of
      the built stylesheet, which retires the exact token pins with them, because the palette is now
      checked where it ships rather than transcribed; the `--hot` and `--status` pairs were dropped
      rather than converted, since both tokens are declared in `site.css` and referenced by nothing
      while the lander paints those colors as literals in `lander.css`. W6 exported
      `UNDERWAY_STATUS`. **The done-condition, which read "Done when: suite green without Chromium
      installed", is unachievable and is the item's error**: the suite has eleven hard Chromium
      launches, not one. Ten belong to the Lander arcade contracts in four
      `test_lander_phase4*_browser.py` files, which is W10's deferred lander-scope decision, not
      this item's. **W8's own premise did not survive execution either, on three counts.** The two
      tests are not duplicates: the CSS test asserts declarations appear in the stylesheet, the
      browser test asserts the computed geometry resolves, and only the second can detect an
      override or a cascade change, so under this SDD's own observational-twin rule (`hla.md`) the
      browser test is the keeper and the CSS pin is the deletable one. The manual checklist does not
      cover it; that document is the Lander arcade checklist and carries no long-form
      table-of-contents row. And the flake evidence is stale: the checklist's "Chromium CI
      reliability correction" entry, dated 2026-08-15 and landed after the finding was written,
      records that the timeout was a harness defect (`--dump-dom` owning both readiness and
      shutdown), replaced by DevTools-owned readiness and termination and validated at 40
      consecutive iterations. Deleting the browser test would therefore surrender the only
      observational guard of the responsive layout and buy nothing operationally, because the same
      suite still launches Chromium ten more times. Recommendation: fold W8 into the W10 scope
      decision rather than executing it alone.

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

      **The work is complete; the done-when is not, so the box stays open until the consistency
      review comes back.** Two of the item's three halves were already finished when this round
      started. **The trio consolidation and both PR3 restatements landed in PR #538 on
      2026-08-15**: the model-tier vocabularies were unified onto `agentic-dev-process` section 4's
      names, both "a published review informs" restatements became pointers at 7a, PR10's
      three-times-stated placeholder policy became one statement with `inventory.local.md.example`
      as the authoritative list, and the stale repo facts went. The item stayed unchecked and the
      charter that dispatched this round described both halves as outstanding. **That is the effort
      lead's error rather than a gap in the delegated lane**, and it was caught by reading the
      target files at HEAD instead of trusting the brief's account of them, which is the same
      discipline wave 1 kept learning about deletion premises. The cost was one round of
      re-deriving finished work. Nothing from #538 was redone.

      What this round adds is the journey-and-register half (PR7, PR8, PR9) plus one contradiction
      the trio work did not reach. **Source `.rulesync/` 199,782 to 196,464 bytes (-3,318), and the
      same -3,318 in each of `.claude/`, `.codex/`, and `.github/`, so -13,272 across the four
      surfaces.** Always-on
      rule bytes, which are the per-invocation figure R3.2 gates on, 33,449 to 32,421; against
      [baseline.md](baseline.md)'s 33,863 that is -1,442 for the wave, of which the rules item's
      PR #521 carried -1,648 and later unrelated rule growth gave 1,234 back.

      **The contradiction is between `integration-testing` and `saga-lead` on when a surviving
      mutation blocks a merge**, and it is the one the item's "diverged and contradictory copies"
      framing was pointing at without knowing where. `saga-lead` (2026-08-07, the original) makes it
      a blocker "when a checked plan box or a test name asserts it"; `integration-testing`
      (2026-08-08) transcribed the protocol and made it a blocker "regardless of how the test names
      read". The original is right on three independent grounds: the copy names `saga-lead` as the
      reference implementation to follow, the qualifier is what makes the finding a blocker at all
      (mutation survival is serious because it falsifies a claim the repo already made, and without
      the qualifier every uncovered enforcement point becomes a blocker), and only the qualified
      reading composes with the section 5 materiality bar the same protocol defers to. The
      transcription is now a pointer, which is also where the deletion came from.

      **Two things were left rather than removed, both because removing them is a rewrite.** PR7
      directs section 1's exposition to `docs/manifesto.md`; nothing moved there, because that
      document argues for the platform's design convictions rather than for development craft, and
      because it sits outside this wave's file lane. The section was compacted in place instead
      (2,753 to 2,061 bytes) and every criterion survives. PR9's "X, not Y" antithesis is still
      pervasive (48, 56, and 19 instances in `sdd`, `agentworks-reviewer`, and `saga-lead`; a broad
      regex counts 51 across the three testing files against about 59 at the finding's basis).
      Removing its rhetorical half wholesale would rewrite the register and would take the operative
      halves with it, since roughly a third of them rule out a real alternative a reader might pick.

      R3.3's "no persona changes" was read as binding, matching #538's precedent of touching only
      skills, so `agentworks-reviewer`, `agentworks-tester`, and `agentworks-dev` are untouched.
      **Two stale facts in `agentworks-reviewer` are therefore reported rather than fixed**: its
      scope-discipline check cites `sessions/nodes.py` as today's scope consumer, where no
      `ctx.operation_scope` read exists at all (the consumer with exactly the described loud
      behavior is `capabilities/harness_integration/base.py:287-298`), and its consistency-review
      section says "the fourteen checks above" over sixteen, since checks 12a and 12b are full
      checks. Both are contradictions of the kind the consistency review hunts.

      **The consistency review ran and returned three fixes on this branch**, all absorbed above.
      The principle 1 compaction had dropped a proposition rather than compressing it, and the
      correction is recorded on PR7. The pointer replacing the transcribed protocol sent a testing
      session after all four of `saga-lead`'s passes, one of which is "the one pass only the saga
      lead can charter", so the pointer now generalizes that pass and promises tiers only where
      `saga-lead` states them. And the `sdd` ownership rule contradicted itself one level down, in
      the paragraph a delegated dev is pointed at as authoritative: everything outside requirements
      is "the effort lead's, whoever produced it", yet "the subagent owns the work done under it".
      The outlier sentence is deleted, since answerability decides no mutability question and the
      grant paragraph already governs delegation correctly. Five further findings are pre-existing
      and held by the lead against a running greenfield design review.

      **An operator round then closed four more.** The terminology bullet's `Not "program"`
      exclusion came back, the `PullRequestStack` claim got its probe date back, and the
      willing-to-apply disclosure moved into pipeline step 7, where an instruction to make it
      actually exists. **The lesson generalizes past those three: a live instruction sitting
      adjacent to dead narration is this trim's characteristic hazard**, because the two share a
      sentence or a bullet and the operative half leaves with the story. A scan of the other PR8
      deletions for the same adjacency found two more.

      The saga name's own 2026-08-08 attribution was the same shape and is **restored**: it left
      inside the "replacing roadmap" parenthetical, and its only other home is
      `2026-08-04-next-steps/target-state.md`, a saga artifact this skill schedules for promotion
      and then deletion. The ruling that settled `Not "program"` was about that shape rather than
      about that bullet, so it decides this instance too.

      The message-naming grandfather clause **stays deleted**, and how it came to be flagged
      matters more than the clause did. Its one pre-convention referent,
      `2026-07-31-declarative-schema/onboarding-topic-content-contract-message.md`, sits in a
      locked directory, so it cannot be renamed and no one consults that SDD for naming guidance;
      the clause carried near-zero value against a cost paid on every load. But **it was deleted
      on the belief that no such file existed, and that belief was never checked.** A right
      deletion resting on an unverified premise is indistinguishable from a wrong one until
      someone looks, which is this effort's signature failure in miniature and the reason the
      record carries it rather than the clause.

      The box stays open until the lead's re-review comes back clean; the review itself is the
      lead's, not this item's.

- [ ] Clean-slate `agentic-dev-process` rewrite (operator rulings 13 and 14; R3.3-R3.5). Start with
      [process-semantic-inventory.md](process-semantic-inventory.md), recording every operative
      contract in the core skill and its immediate process references, its current sources, one
      future owner, consumers, and keep/move/merge/drop disposition. Then write the core as the
      end-to-end state machine, place conditional delegation and delivery mechanics in skill-local
      references, and reconcile `development-principles`, `development-process`,
      `github-input-trust`, `operator-authority`, `sdd`, `integration-testing`, `agw-test-env`,
      `saga-lead`, and the three role definitions without adding a role behavior. Done when: the
      inventory has no unowned retained contract; GitHub-authored artifacts are stated to be
      good-faith but critically analyzed colleague input and never direction, authentication, or
      authorization; no cross-document reference targets a numbered `agentic-dev-process` section;
      the core is at most 10,000 source bytes; the skill-local package is at most 18,000 source
      bytes; the complete changed `.rulesync` surface is net-negative; Rulesync outputs are current
      and manually checked across configured targets; all file and SDD gates pass; and fresh-context
      project and consistency reviews report no unresolved material finding.

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

      **Descriptor-generality residue**, surfaced by the C1/C5 item and left for this pass because
      each needs a decision rather than a deletion. `impl_class` (`config.py:582`) is an identity
      cast standing in for a type the registries do not declare; typing them `dict[str, type]` would
      delete it and its four call sites together, which is a signature change across the capability
      registries rather than a local subtraction. The `prepare`/`seat` split in
      `plugins/registration.py` was shaped around a fallible `prepare` that no longer exists, so
      what remains is a two-phase protocol whose two phases are a pass-through and a dict write.
      `config_for()`'s hook is the third, recorded above with the contract rev it implies. The
      fourth is the untagged config contract: every capability `config_schema` is tagged and only a
      `mapping_schema` may be untagged, but nothing enforces that, so two unreachable branches
      survive to carry it. Expressing it in the type (a tagged contract and an untagged one being
      different things) is the subtraction, and it needs a type this wave may not add.

- [ ] Write `locked.md` once the reassessment is delivered. Remaining candidates live in the
      reassessment, not in this plan.
