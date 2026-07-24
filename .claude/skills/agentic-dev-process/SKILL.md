---
name: agentic-dev-process
description: >-
  How we drive a development effort end to end: sizing the work, SDD for large
  efforts, delegated implementation, per-step review, and when to escalate
---
# Agentic Development Process

This is the top-level playbook for how a development effort runs, from a standing start to a
merge-ready PR. It ties three things together: the `sdd` skill (how we spec significant work), the
`agentworks-dev` subagent (who implements), and the `agentworks-reviewer` subagent (who checks). The
always-on rules in `.rulesync/rules/` and the repo's `CONTRIBUTING.md` cover the mechanics (code
style, linting, conventional commits); the `agentworks-dev` philosophy covers _how_ to write the
code. This skill covers the flow that sits above both.

The process scales with the work. A large effort walks every step below; a small change collapses
several of them, but review, regular commits, and escalation still apply. Hold the whole picture,
and delegate the depth.

## 1. Size up the work first

Before touching anything, decide how big this is, because the size picks the track.

- **Large or significant efforts** (new subsystems, contract or schema changes, anything spanning
  many files or hard to reverse): run the SDD process (section 2) and implement through delegation
  (section 3).
- **Small, simple changes** (localized, well-patterned, low-risk): skip SDD and implement directly.
  They still get reviewed (section 5), committed regularly (section 6), and escalated if they turn
  out bigger than they looked.
- When it is genuinely ambiguous which track fits, lean heavier for anything that reshapes a
  contract or is hard to undo, lighter for a localized change that follows an existing pattern. If
  still unsure, ask (the `ask-questions` rule).

## 2. Large efforts: spec with SDD

Drive significant work through the `sdd` skill: the FRD, HLA, plan, and any LLDs, in the feature
directory. The plan's checkboxes are the source of truth for what is done. Follow that skill's
phased-review guidance (FRD first, then HLA, then plan/LLDs) so concerns surface while they are
still cheap.

**The lead authors and owns the plan; do not delegate it.** Writing the plan is how the lead
pressure-tests the FRD and HLA: cutting the work into honest, always-green phases is what exposes
where the upstream docs are vague or wrong, and the author is the one positioned to feed those fixes
straight back upstream. Owning the plan also keeps the picture and the decision-making in one place,
so a dev who hits a plan problem raises it to the lead, who owns and revises it, rather than to an
ambiguous drafting author. You MAY delegate code _scouting_ to inform the plan (read these files,
report the anchors, shapes, and surprises: facts, not phasing), but the phasing and the plan
document are the lead's. The FRD and HLA are lead-authored for the same reason; the LLDs are not
(section 3).

The SDD skill runs its pre-implementation artifact review as a **draft PR** by design. That is the
one sanctioned exception to the non-draft default in section 6.

## 3. Large efforts: implement through delegation

Once the plan exists (section 2), implement large efforts by delegating the downstream work to
`agentworks-dev` subagents rather than doing the depth yourself. Delegatable: the **LLDs** (bounded,
downstream detail-pins for a single component, which fill in one box of the already-owned plan
rather than encoding the whole picture) and the **implementation** of each plan step. The lead (you)
stays out of the weeds on purpose:

- **Keep the lead's context concise.** A lead buried in file-by-file edits loses the thread. Hand
  the implementation of each plan step to a dev subagent, read back its result and its hand-off
  notes, and keep your own context focused on the plan, the architecture, and what comes next.
- **Hold the overall picture.** The lead owns sequencing across steps, the cross-cutting invariants
  no single step sees, the plan checkboxes, and the decision of when to escalate (section 8). That
  is the job the delegation frees you to do well.
- Give each dev subagent a crisp, self-contained task: the plan step or LLD it owns, the relevant
  `file:line` anchors, and the definition of done. Let it build on the code at HEAD, not on your
  summary of it.
- **A delegated subagent surfaces decisions to the LEAD, not "to the operator."** The lead is the
  filter: the dev raises a decision or a plan problem, the lead decides it, and only the genuinely
  operator-significant ones go up (section 8). Review LLDs closely; like the plan, they can surface
  an FRD/HLA gap, which the lead feeds upstream.

## 4. Choose the model deliberately for each delegation

When you launch a subagent, pick the model to match the task rather than defaulting blindly. Model
names change; match the current equivalent of each tier:

- **Standard reasoning tier (e.g. Claude Opus): the default for most well-defined development
  tasks.** If the step is specified and just needs to be built well, this is the right choice.
- **Lighter tier (e.g. Claude Sonnet): for simpler, mechanical, or low-ambiguity tasks** where the
  standard tier would be overkill.
- **Top tier (e.g. Claude Fable): reserved for the exceptionally tricky.** Reach for it only when a
  task genuinely needs the strongest reasoning; it is not the everyday default.
- **A reviewer must be at least as capable as the dev whose work it reviews.** Never review
  standard-tier work with a lighter-tier reviewer; match or exceed it.

## 5. Review every step

Every development step gets reviewed by the `agentworks-reviewer` subagent before you consider it
done, at the model tier from section 4 (reviewer >= dev). This holds for delegated steps and for
small changes you make directly.

The stance toward any finding, from the reviewer or from automated review (section 7), is the same:

- Push back on findings that are genuinely incorrect; a reviewer is not infallible, and a wrong
  finding followed blindly makes the code worse.
- Otherwise, err on the side of fixing anything valid, including the minor and the merely-nicer.
- Iterate until everyone is happy. Do not move on from a step with a live, unaddressed valid finding
  hanging over it.

Who applies the fixes follows ownership: findings on **code** loop back to the implementing dev
subagent (it keeps the context and the authorship, and the review-then-revise loop stays intact),
while findings on a lead-owned artifact (the plan, an LLD the lead is finalizing) are the lead's to
apply directly.

## 6. Commit, push, and PR

- **Commit and push at regular intervals.** Do not hoard work in a local branch; frequent, honest
  commits keep the work reviewable and recoverable. Follow the project's Conventional Commits
  convention (`CONTRIBUTING.md`) for message shape.
- **One PR per feature is the default.** Put the whole feature in a single PR, SDD artifacts
  included. Split into multiple PRs only when there is a good reason, the usual one being legitimate
  SDD phases that each carry independent, standalone value. A phase that only has value once a later
  phase lands is not a reason to split; it is a commit within the one PR. Always-green phased
  commits give reviewers a natural commit-by-commit reading order inside a single large PR.
- **Open a PR when the work is close to merge-ready**, not before. A PR signals "this is ready for
  eyes," so open it when that is true.
- **Non-draft by default.** Avoid draft PRs unless specifically asked for one. The single routine
  exception is the SDD pre-implementation artifact review, which the `sdd` skill runs as a draft PR
  on purpose (section 2).

## 7. Get a fresh-eyes pass: Copilot if available, else a generic review here

Alongside the `agentworks-reviewer` (which reviews against the project's own values and
conventions), a code-heavy change also wants a **fresh-eyes generic review**: a reviewer reading the
diff cold, with no house-style priors, hunting for plain correctness bugs, edge cases, and security
issues. The two lenses are complementary, not redundant, and the generic one earns its keep, in
practice it catches robustness gaps the values-checklist waves through (a malformed-input crash, a
swallowed error), while the project reviewer catches conventions and docs-sync the generic pass
misses.

- **Copilot when available.** It reviews new pushes to a **ready** (non-draft) PR automatically.
  Read those comments: not always right, but frequently hidden gems, so triage them rather than
  ignoring them. (One more reason the default is a ready PR, not a draft: a draft may not get the
  automated pass.)
- **When Copilot is unavailable** (quota exhausted, feature off, or you want the pass before
  pushing), substitute a **vanilla generic review right here**: a `general-purpose` subagent on a
  **lower model (e.g. Sonnet)**, prompted to review the diff as a senior engineer reading it cold,
  no project-specific checklist. Run it in parallel with the `agentworks-reviewer` and triage both
  together.

Either way, apply the same finding stance as section 5 (push back on the wrong, fix the valid).
Reserve this for **code-heavy** slices; a doc-only or closeout change has little for a fresh-eyes
pass to catch, so a lead review is enough there.

## 8. Escalate the big stuff; otherwise keep moving

Throughout the effort, escalate to the operator for anything significant: a necessary redesign, a
requirement that turns out wrong, a blocking decision that is the operator's to make, a discovery
that changes the shape or scope of the work, or a smell you cannot resolve cleanly (the `push-back`
and `permission-to-fail` rules). Surface it early and plainly rather than papering over it or
guessing.

Short of that, keep pushing forward as long as the road is clear. The goal is steady, reviewed
progress that the operator can trust without having to drive every step, punctuated by clear
escalations at the moments that actually need a human call.
