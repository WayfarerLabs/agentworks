---
name: sdd
description: Spec-Driven Development workflow for significant development efforts
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

Immutability protects _truthful_ records, which leaves one carve-out. A box checked prematurely or
in error may be corrected while it is still unmerged: fixing a bad entry is not rewriting a good
one, and an inaccurate checkbox is not a historical record of anything. The rule binds fully once
the checked box has merged to `main`, in step with the lock-at-merge paragraph under
[Lockfile](#lockfile). Key that to the box, not to the plan file: plans routinely merge mid-effort,
so the plan being on `main` says nothing about whether any particular box has landed there yet.

Supersession is not error (operator ruling, 2026-08-10). When a scope correction expunges work
before it ever merges, the truthful checked boxes recording that work remain in the plan: the work
happened, and "it never shipped" does not make a done-work record false. The clean-slate standard
applies to everything around those boxes instead (narration, correction framing, definitions of
done, and unchecked boxes of the abandoned direction all strip), with exactly one short note
recording the supersession, which is what the reviewer's exception keys on.

Mutability also follows ownership. An agent working an effort edits that effort's SDD artifacts and
no other's: do not update another SDD's content (a saga SDD's ledger, a sibling effort's plan)
unless specifically instructed to, and treat such an instruction as the exception, not standard
process. When work surfaces an inconsistency in another SDD (a stale claim, a checkbox that no
longer matches reality, a statement your change invalidates), flag it to the operator rather than
editing it yourself. If you also have a working communication channel to that SDD's owner, flag it
there too: for a transferred child artifact (FRD, HLA, plan, implementation) the owner is its effort
lead, and for saga artifacts and rulings it is the saga lead; until such channels exist, the
operator is the reliable route.

One sanctioned channel does exist: new-file message passing. Adding a NEW file to another SDD's
feature directory as a message is fine (a saga delivering seed notes into an adopted child's
directory is the standing example); the restriction is on modifying another effort's existing
artifacts. Name new message files `message-<YYYY>-<MM>-<DD>-<topic>.md`. The convention governs new
messages only: message files delivered before it keep the names they already have, so there is no
rename sweep to do and no inference to draw from an older name. A sender never overwrites an
existing message file, because overwriting is an edit to another effort's artifact and can destroy a
message the recipient has not read yet; a follow-up is always a new file. A delivered message file
belongs to the receiving effort once read: integrate it into your own artifacts, then keep or delete
it as you see fit.

Delivery semantics: messages deliver via `main`, never by committing to another effort's live
branch. A branch is mutable state under its owner's control (a rebase or force-push can silently
drop a foreign commit, so branch delivery can lose messages while looking delivered), and the
PR-to-main hop is the operator's review gate on inter-agent instructions. Pickup is cheap and needs
no branch changes: `git show origin/main:<path>` reads the message as delivered. To bring it
in-tree, cherry-pick the message commit or merge `main` in. To keep cherry-picking clean, a sender
delivers each message as a single commit touching only the message file (other changes ride separate
commits, even in the same PR).

Task briefs outside any SDD: some ruled work is too small for an SDD and belongs to no live effort
(a follow-on the operator ruled, a cross-cutting fix with a named owner). These are dispatched, not
messaged, and the difference matters: a message goes to a live effort and must survive its branch
churn, so it delivers via `main`; a brief CREATES the work vehicle, and the assignee owns it from
pickup, so it never needs to touch `main` at all. The current mechanism: the requester seeds a
branch, naming it with the repo's conventional branch prefix for the eventual change (`feat/<slug>`,
`fix/<slug>`, `chore/<slug>`, ...) since the seeded branch is the working branch the PR ships from,
whose first commit adds a single brief file (`brief-<YYYY>-<MM>-<DD>-<topic>.md`; the distinct
prefix keeps it from reading as the message channel above, whose semantics it does not share). It
sits at the repo root for pickup discoverability, which stays clean because briefs never reach
`main`. The brief carries the charter (what, why, the definition of done, where the authoritative
spec lives, who reviews) and its own disposition, usually "delete this file before the PR goes
ready" (keep-and-promote is the exception, and the brief says where the content goes). The assignee
takes over the branch, does the work there, and disposes of the brief per its own instructions. The
contract is mechanism-independent on purpose: a brief names its work, its reviewer, and its own
disposition, and delivery may later move to other channels (including Agentworks-native messaging)
without changing what a brief is.

A running effort only sees messages that existed at its branch point, so a message that lands after
the recipient's branch was cut needs two independent mechanisms. Neither side may assume the other
covers it:

- **Primary: the sender notifies the operator.** A sender whose message lands late tells the
  operator, carrying the message commit's sha, so the recipient gets nudged. This is the mechanism
  delivery actually relies on; the backstop below is not a substitute for it.
- **Backstop: the recipient looks.** Recipients on long-running branches glance at their feature
  directory on `origin/main` at natural checkpoints, so a mid-flight message still lands when the
  notification never arrives.

Messages MUST NOT be delivered into a locked feature directory. Once `locked.md` is on `main`, the
lockfile CI rejects every change under that directory except a `locked.md` update or a full wipe
(see [Lockfile](#lockfile)), so the message simply cannot merge. When the recipient is locked there
is no live effort to receive anything: send the message to the operator instead, and let the
operator decide whether it warrants reopening the topic elsewhere.

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

CI enforces this rule via `./scripts/check-locked-sdds.sh` (run on every PR and push to `main`).
Once a feature directory's `locked.md` is present on `main`, the check fails any change under that
directory except two: updating `locked.md` itself, or deleting the whole directory down to the
`locked.md` tombstone (a full wipe, not a partial deletion). It compares against the merge-base with
`main`, so a PR that introduces `locked.md` alongside the final SDD edits is fine; only a lockfile
that was _already_ on `main` freezes the directory.

Abandoned and superseded efforts lock like any other. The trigger for `locked.md` is that work on
the effort has _stopped_, not that it succeeded: write the lockfile, dated as usual, and record
honestly what shipped (possibly nothing, possibly one phase of four) and why the rest did not. This
is not bookkeeping for its own sake. [Deleting Stale SDDs](#deleting-stale-sdds) excludes anything
without a lockfile, so an abandoned SDD left unlocked is permanently ineligible for cleanup and sits
in the live tree misleading readers with a design nobody is building.

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

## Saga SDDs

Most SDDs cover one development effort. A saga SDD is the meta case: an SDD that coordinates a
family of related efforts, generating and tracking ordinary child SDDs rather than shipping an
implementation of its own. Use one when several efforts overlap enough that their ordering and
shared contracts need a single owner. The name is borrowed deliberately from the distributed-systems
saga (operator ruling, 2026-08-08, replacing the earlier "roadmap" term, which collided with
date-anchored planning vocabulary and over-promised timeline semantics): child efforts commit
independently and completely, intermediate states are visible on `main` rather than isolated, a lead
orchestrates instead of locking, and the whole either runs to its lock or is deliberately
compensated by unwinding.

Compensation is deliberately minimal, not a hidden lifecycle. Only the operator can abandon a saga
(in whole or in part), and the decision is a dated ruling recorded in target-state like any other.
Already-merged children stay merged: compensation is new forward work (reverting or removing
surfaces through ordinary reviewed PRs), never history rewriting, and the ledger records outcomes as
new entries without unchecking anything. The terminal artifact state is the same lockfile as
success, recording what completed and what was abandoned and why. Nothing else is promised; if a
real abandonment ever needs more protocol, it is designed then, as saga work.

The settled rules for the species:

- Its artifacts are not the standard set. The working set, established by the first saga SDD: an
  `inputs/` folder holding perspectives and other source material; `starting-state.md`, an immutable
  snapshot of where the system stood at saga start, frozen once underway so the full journey stays
  visible; `current-state.md`, a dated snapshot of where the system is, updated in place at wave
  boundaries (git history is the append-only record); `target-state.md`, where the system is going
  across this saga (not a forever vision) and the home of every settled design ruling; `phasing.md`,
  ordering only (dependency structure, waves, release mapping); and `child-sdds.md`, the inventory
  and checkbox tracker that plays plan.md's role, whose completed checkboxes are immutable per the
  standard rule. A saga SDD has no frd.md, hla.md, or plan.md of its own; those belong to the
  children. The saga locks when current state and target state agree and every child is locked.
  These forms are still young; refine them here as they prove out.
- A saga constrains only its own scope. Work outside the saga is not paused by it and can be picked
  off whenever bandwidth allows; the saga's target-state should say explicitly what is out of scope
  so that boundary stays crisp.
- It stays open until current state and target state agree and every child SDD is locked, then locks
  like any other SDD. Its ledger plays the role plan checkboxes play in an ordinary SDD, one level
  up.
- `target-state.md` stays mutable while children are still running, but revising it late reopens the
  current-equals-target gap by definition: whatever the revision adds is by construction not yet
  true of the current system, so the saga cannot lock until current state catches up to the revised
  target. That is a real cost, not a formality. Revise the target deliberately, and prefer a
  follow-on saga to a late expansion of this one.
- The settled design rulings `target-state.md` accumulates are exactly the load-bearing content
  [SDDs Are Not Permanent](#sdds-are-not-permanent) requires promoting into permanent homes
  (`docs/arch/`, ADRs) before the saga locks and is eventually deleted. As the artifact's owner, the
  saga lead owns those promotions.
- Saga state lives on `main`: every change (a new child SDD, a status change, a design revision) is
  a PR, and child SDDs reference their saga SDD so the coordination is discoverable from any effort.
- The saga lead seeds each child SDD with its FRD plus any constraints the saga has already settled,
  and reviews the child's PRs. A separately launched effort lead owns the child's HLA, plan, and
  implementation from the start, per the ordinary process. Seeding PRs are ready, not draft: their
  content is limited by design, but they are intended to merge as-is (see PR Review). Ownership of
  the seeded FRD transfers to the effort lead when the seeding PR merges: from then on it is the
  child's artifact like the rest of its SDD, the effort lead revises it when it turns out wrong
  (keeping the saga lead informed of material revisions), and the saga lead stops editing it like
  any other child artifact. Settled saga constraints recorded in the FRD still bind; the effort lead
  flags disagreement rather than reopening them unilaterally.
- The saga's artifacts, ledger included, are the saga lead's to maintain. Child effort leads do not
  update the saga SDD to mark their own progress; the saga lead tracks child status from merged PRs.
  Child leads flag inconsistencies they notice to the operator instead (see Artifact Mutability's
  ownership rule).
- Terminology: saga SDD, saga lead, child SDD, effort lead. Not "program".
- The `saga-lead` skill is the operating manual for the role itself: watching child efforts, the
  multi-pass review protocol for their PRs, and the lead's after-round duties. This section defines
  the artifact form; that skill defines how the lead runs it.

## Branching Model

Work driven via SDD should be done in one or more feature branches. The general pattern is:

1. Create an initial feature branch. This should generally relate to the naming of the feature
   directory, although additional info (e.g. phase) is allowed.
2. Create the SDD feature directory and artifacts in this branch.
3. If pre-implementation review is needed, publish a draft PR to allow others to review and provide
   feedback on the SDD artifacts. Draft is the right state here because there is no merge intent
   yet: the PR is a pure review vehicle while the artifacts churn. It is not draft because the
   content is partial. See [PR Review](#pr-review) for the merge-intent rule this follows from.
4. The first push of work should use that existing branch.
5. SDD artifacts merge with the work itself, or ahead of it when other efforts need visibility (see
   [Merge artifacts early](#merge-artifacts-early-when-other-efforts-need-visibility)).
6. If additional work remains per the specs, it should be done in additional feature branches,
   tracking the work via the existing plan files. It is entirely permissible (encouraged) to modify
   the artifacts if the requirements, architecture, plan, etc. has changed.
7. Alternatively, if future work superseded unfinished work in an existing SDD feature directory,
   that future work should update the existing SDD specs to indicate that the remaining work is
   superseded.

## Choosing the PR vehicle

An SDD effort picks its delivery vehicle deliberately (the dev-process skill defines the layering):
a **single PR** remains the default for small and medium efforts; a **PR stack** fits multi-phase
efforts whose phases are honest working increments, and the phased artifact review can be the
stack's base entry (FRD/HLA/plan at the bottom, implementation entries stacked on top, one visible
sequence instead of two review streams); **coordinated non-stacked PRs** fit genuinely independent
slices, and are the only form available across repositories in a poly-repo environment. Record the
choice in the plan.

## PR Review

Significant changes to SDD artifacts -- whether net-new specs or material revisions to existing ones
-- should go through a draft PR for review before the work is merged. The aim is to surface concerns
about requirements, architecture, or plan early, while changes are still cheap.

Ready versus draft is purely a merge-intent signal, and it should be set accordingly. The
pre-implementation review above uses a draft PR because there is genuinely no intent to merge at
that point: the PR exists as a pure review vehicle while the artifacts churn, until the review
converges and the PR is promoted (see Merge artifacts early below). By contrast, a PR whose content
is complete and intended to merge as-is should be ready no matter how small it is; limited content
is not draftness. A PR that seeds a new effort with only its FRD, for example, is ready to merge,
not a draft.

Consider phasing the review across multiple PRs rather than landing all the artifacts in one. A
common pattern is FRD first (to confirm we agree on what we're building), then HLA (to confirm the
design holds up), then plan and any LLDs. Each phased review is cheaper to consume than a single
sprawling PR, and it limits how far the work can drift down the wrong path before someone catches
it.

### Merge artifacts early when other efforts need visibility

A branch is private state; `main` is the coordination plane. That is why message passing delivers
via `main`, and the same logic applies to the artifacts themselves: a sibling effort designing
against your FRD, HLA, or plan can only see what has landed on `main`, and "read my feature branch"
is not a coordination mechanism (branches rebase, drift, and can vanish, and nothing notifies a
sibling when they do). So when another effort could build against your design (under an active saga,
assume one can), merge SDD artifacts ahead of the implementation. Instances of the pattern: a saga
child's seeding PR; the reviewed pre-implementation artifacts (once the draft review converges,
promote and merge rather than letting the artifacts ride the feature branch to the end; for a saga
child, the effort lead explicitly requests that draft review from the saga lead, whose ready-flip
watch covers merge-intent PRs, and the operator's direction on that review is what sanctions
promotion: a converged review recommends promotion, it does not authorize it); and material
in-flight DESIGN revisions, which keep flowing to `main` promptly as small PRs rather than
accumulating. After an early artifact merge, implementation simply continues on the same branch (or
a fresh one) and opens its own PR; the branching flow above is otherwise unchanged.

Two things never merge ahead of their work. Checkbox flips are completion claims, not design: a
checked box merges with or after the work that makes it true (an early-merged box would be an
immutable record of work that never shipped if the branch dies, and the reviewer rightly flags a box
checked in a PR that does not contain the work, unless the box is a truthful supersession survivor
per the artifact-mutability section, which the retained supersession note identifies). And
`locked.md` rides the effort's final PR, never an early artifact PR: once it lands on `main` the CI
guard freezes the directory against the still-open implementation and cuts off message delivery to a
live effort. Merged-early artifacts describe intent rather than shipped behavior, which is exactly
what SDD artifacts are for; permanent docs stay bound to behavior at HEAD per SDDs Are Not
Permanent. This is a sanctioned exception to the one-PR-per-feature default in the
`agentic-dev-process` skill, which cross-references it.
