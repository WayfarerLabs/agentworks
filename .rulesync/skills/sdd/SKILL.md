---
name: sdd
description: "Spec-Driven Development workflow for significant development efforts"
targets: ["*"]
---

# Spec-Driven Development

For significant development efforts, we use spec-driven development (SDD) to guide the development
of the project.

Note that it is ok to skip SDD for small, simple changes.

## Feature Directory

The specs and related artifacts for each development effort are stored in a subdir of the
`/docs/sdd` directory called the "feature directory". The feature directory name should start with
`<YYYY>-<MM>-<DD>-` (representing the start date) to easily identify the feature age and order of
creation.

## Artifacts

Within the feature directory, we store the following artifacts:

- `frd.md` (Functional Requirements Document): A markdown file that contains the functional
  specification for the development effort. This should be focused on the functional requirements
  (business requirements, user stories, personas, etc.) and avoid technical details unless they are
  fundamental to the business requirements.
- `hla.md` (High-Level Architecture): A markdown file that describes the high-level architecture.
  This should specify overall topology, components, major interfaces, suggested/required tech stack
  choices, integration with the platform, etc. This generally shouldn't have low-level details such
  as code samples, specific versions, etc. unless they are critical to the architecture. Pseudocode
  for critical algorithms is allowed when appropriate.
- `plan.md`: A markdown file that detailed technical plan for the development effort complete with
  checkboxes for tracking the work as it is completed. Plan should include specific definitions of
  done that can be used to objectively determine completeness. If phasing is appropriate, the phases
  should be described here, either within a single plan file or as multiple plan files. This should
  provide checkboxes and the work should be tracked here.

Most efforts will involve one or more "low-level design" (LLD) documents that provide more detailed
technical specifications for specific components, algorithms, interfaces, etc. These should be
stored in the feature directory with descriptive names (e.g. `something-lld.md`). As a general rule,
the plan should include generating any missing low-level design documents as part of the work, and
they should be linked to from the plan.

Two additional artifact types come up often enough to call out by name:

- `prior-art-research.md`: Captures the background research, prior art, and external sources that
  informed the design. Considering prior art is part of nearly every non-trivial design effort, so
  default to including this artifact. Skip it only when the work is a pure internal refactor or
  follows already-established patterns where there is no meaningful external research to surface. A
  useful structure: a short executive summary, findings organized by research dimension with sources
  cited per finding, an explicit "refuted / do-not-rely-on" section for claims considered and
  rejected, open questions the research did not resolve, and a sources table grading each entry by
  quality and angle. Tie each finding to a design decision so the link from research to design is
  auditable.
- `migration-strategy.md`: Describes how the existing system moves to the target design. Use this
  when the work reshapes something already in use (data, schemas, pipelines, API surfaces, module
  layouts) rather than being net-new. A useful structure: an inventory of the current state
  (concrete numbers and a dated snapshot), target naming/shape with before/after examples,
  transition mechanics (additive-first vs. in-place, data movement, producer repointing,
  backward-compatibility shims), sequencing (per-item timelines, vertical-slice-first), a worked
  example for one representative case, and a risks-and-safeguards section.

Additional artifacts related to the development effort (e.g. UI concepts, API specifications, data
models, diagrams, etc.) may be stored in the feature directory as needed.

## Artifact Mutability

As a general rule, SDD artifacts are mutable until the lockfile is created. As issues are
encountered during implementation, the specs and plan should be updated to reflect the new
understanding and any changes to the requirements, architecture, or plan. The SDD artifacts are
living documents and should evolve with the work.

The one exception to that is that **completed plan checkboxes should not be modified in any way**
(unchecked, modified, moved, removed, ...). Once a checkbox is marked complete, it should be
considered an immutable record of what was done. If the plan changes such that a completed checkbox
is no longer relevant, it should be left as-is and new checkboxes should be added to reflect the new
plan. This preserves the historical record of what was actually done, even if the implemented
solution evolves.

## Lockfile

When work on the SDD is done, a `locked.md` file should be created in the feature directory. This
file should have a date and summarize the final state of everything. Once a lockfile is created, the
SDD artifacts are considered "locked" and should not be modified except in exceptional
circumstances. If changes are needed, the lockfile should be updated with a date and summary of the
changes.

**The lock takes effect when the SDD lands on `main`, not when `locked.md` is first written.** The
lockfile is created as part of closeout, which normally happens on the feature branch _before_ it
merges, so `locked.md` routinely exists on an open branch. While that branch is still unmerged the
SDD is not yet locked: last-minute pre-merge edits (addressing review, a refinement that lands
before merge) are ordinary in-flight changes and need no lockfile-update ceremony. Treat the lock as
binding only once the artifacts are on `main`; a post-merge change is the exceptional case the
paragraph above governs.

A locked SDD is immutable but not permanent. See [Deleting Stale SDDs](#deleting-stale-sdds) for the
post-lock lifecycle.

## SDDs Are Not Permanent

**Overarching rule:** no one should ever need to read anything under `docs/sdd/` to understand or
work with the current system. The whole directory should be deletable without losing anything the
project's contributors and operators rely on day-to-day. That is the load-bearing test for every
change made under this skill.

SDDs are time-bounded artifacts that document a single development effort. They live in
`docs/sdd/<YYYY>-<MM>-<DD>-<feature>/` and are not guaranteed to be present in the repo after the
work is completed. **Treat SDD paths as ephemeral.**

This has three implications:

1. **Do not anchor permanent artifacts to SDD paths.** Code comments, Terraform variable
   descriptions, output descriptions, READMEs in `docs/arch/`, ADRs, operator guides, and anything
   else that is meant to outlive the SDD must either (a) stand on its own, or (b) reference a stable
   doc (`docs/arch/*`, `docs/adrs/*`, a stable module README, etc.). A trailing "See
   docs/sdd/.../foo.md" in a code comment is a smell. It pins the comment to a doc that may not be
   there later.
2. **Promote load-bearing content out of the SDD before the SDD goes away.** As the implementation
   lands and the doc-level concepts that survived contact with reality emerge (output conventions,
   contract shapes, architectural patterns, operator-facing runbooks), update or create the
   permanent home for them. The plan's documentation phase should explicitly include these
   promotions. The SDD itself can keep its historical rationale, decision log, and tradeoff
   discussion. But the concepts the codebase relies on need to live elsewhere.
3. **Ship permanent doc changes alongside the code that backs them.** Permanent docs (architecture
   docs in `docs/arch/`, ADRs, operator guides, module READMEs, skills, rules) must reflect
   observable system reality at HEAD on `main`. The tiebreaker test runs in both directions:
   - A doc that claims a behavior not yet true at HEAD is **premature** -- don't merge it ahead of
     the code.
   - A doc that omits a behavior already true at HEAD is **stale** -- don't defer the update.

   So when a code change alters reality, the matching doc change rides in the same PR. In multi-PR
   efforts the doc update lands in the PR that _makes the doc claim true_ -- not the first PR in the
   sequence and not a "polish" PR at the end of the SDD. Edge cases will be fuzzy (upstream pin
   advances, multi-deployment rollouts, doc changes that need to wait for a tag to be cut in another
   repo); the principle is lockstep with the change that makes the doc factual, not deferred to the
   SDD's closeout.

When writing the SDD, picture the codebase six months after it merges and the SDD is no longer in
the repo. Any comment, description, or doc that would dangle in that future is broken at write-time.

### Per-SDD spell-check dictionaries

If your SDD introduces new vocabulary (third-party tool names, vendor or product names,
vendor-specific codes, domain jargon, and so on) that doesn't yet appear in any permanent code or
doc, scope the cspell additions to the SDD rather than adding them to the root `.cspell.json`. When
the SDD eventually goes away, its vocabulary goes with it.

Drop a `.cspell.json` in the SDD's feature directory that imports the root config:

<!-- cspell:ignore mkdocs linkml foobar -->

```jsonc
{
  "version": "0.2",
  "import": ["../../../.cspell.json"],
  "words": ["mkdocs", "linkml", "foobar", "..."],
}
```

cspell uses the first config it finds walking up from the file being checked and does not auto-merge
ancestor configs; the `import` field is what brings the root vocabulary in. Additions from the SDD
config and the imported root are combined into a single effective dictionary.

The promotion rule mirrors the broader SDD-not-permanent principle: when a word starts appearing in
permanent code, permanent docs (`docs/arch/*`, module READMEs, ADRs), or anywhere else outside
`docs/sdd/`, move it to the root dictionary so it survives the SDD's eventual deletion.

## Deleting Stale SDDs

Locked SDDs are immutable historical records, but they aren't permanent. Once a locked SDD has
become significantly out of date, the right move is to delete the SDD's contents and leave only the
`locked.md` behind as a tombstone. Full git history still preserves everything for anyone willing to
dig; removing the SDD from the live tree prevents semantic search and grep from surfacing outdated
content as if it were current.

"Significantly out of date" is operational, not calendar-based: an SDD is a candidate for deletion
when reading it today would mislead more than inform. The usual triggers are:

- The work it described has been substantively replaced or refactored away.
- The resulting system has diverged enough from the SDD's design that the SDD now describes a
  counterfactual.
- The SDD's content is duplicated in (or has been fully promoted into) permanent docs and the SDD is
  no longer the primary reference for anyone.

When deleting:

- Update `locked.md` to briefly say _why_ the artifacts were removed and record the SHA of the last
  commit where they existed (e.g. "see commit `abc123` for the original artifacts"). Semantic search
  hitting the leftover `locked.md` should land on useful pointer information, not a bare tombstone.
- Do the deletion as its own deliberate change (PR or commit), not as a side effect of other work.
  The deletion is the change.
- Active SDDs (no `locked.md` yet) are not candidates -- they're still load-bearing for in-progress
  work.

The balance to hold: lean toward deletion when reading the SDD might actively mislead, and lean
toward preservation when the SDD still meaningfully informs current work. When in doubt, ask whether
_not_ finding this SDD via a present-day file or grep search would be a loss; if the answer is no,
delete.

## Branching Model

Work driven via SDD should be done in one or more feature branches. The general pattern is:

1. Create an initial feature branch. This should generally relate to the naming of the feature
   directory, although additional info (e.g. phase) is allowed.
2. Create the SDD feature directory and artifacts in this branch.
3. If pre-implementation review is needed, publish a draft PR to allow others to review and provided
   feedback on the SDD artifacts.
4. The first push of work should use that existing branch.
5. SDD artifacts will naturally get merged with the work itself.
6. If additional work remains per the specs, it should be done in additional feature branches,
   tracking the work via the existing plan files. It is entirely permissible (encouraged) to modify
   the artifacts if the requirements, architecture, plan, etc. has changed.
7. Alternatively, if future work superseded unfinished work in an existing SDD feature directory,
   that future work should update the existing SDD specs to indicate that the remaining work is
   superseded.

## PR Review

Significant changes to SDD artifacts -- whether net-new specs or material revisions to existing ones
-- should go through a draft PR for review before the work is merged. The aim is to surface concerns
about requirements, architecture, or plan early, while changes are still cheap.

Consider phasing the review across multiple PRs rather than landing all the artifacts in one. A
common pattern is FRD first (to confirm we agree on what we're building), then HLA (to confirm the
design holds up), then plan and any LLDs. Each phased review is cheaper to consume than a single
sprawling PR, and it limits how far the work can drift down the wrong path before someone catches
it.
