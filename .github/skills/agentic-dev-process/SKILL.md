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

The SDD skill runs its pre-implementation artifact review as a **draft PR** by design. That is the
one sanctioned exception to the non-draft default in section 6.

## 3. Large efforts: implement through delegation

Implement large efforts by delegating each step to `agentworks-dev` subagents rather than doing the
depth yourself. The lead (you) stays out of the weeds on purpose:

- **Keep the lead's context concise.** A lead buried in file-by-file edits loses the thread. Hand
  the implementation of each plan step to a dev subagent, read back its result and its hand-off
  notes, and keep your own context focused on the plan, the architecture, and what comes next.
- **Hold the overall picture.** The lead owns sequencing across steps, the cross-cutting invariants
  no single step sees, the plan checkboxes, and the decision of when to escalate (section 8). That
  is the job the delegation frees you to do well.
- Give each dev subagent a crisp, self-contained task: the plan step or SDD slice it owns, the
  relevant `file:line` anchors, and the definition of done. Let it build on the code at HEAD, not on
  your summary of it.

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

## 6. Commit, push, and PR

- **Commit and push at regular intervals.** Do not hoard work in a local branch; frequent, honest
  commits keep the work reviewable and recoverable. Follow the project's Conventional Commits
  convention (`CONTRIBUTING.md`) for message shape.
- **Open a PR when the work is close to merge-ready**, not before. A PR signals "this is ready for
  eyes," so open it when that is true.
- **Non-draft by default.** Avoid draft PRs unless specifically asked for one. The single routine
  exception is the SDD pre-implementation artifact review, which the `sdd` skill runs as a draft PR
  on purpose (section 2).

## 7. Mind the automated review

Copilot will often review new pushes to a **ready** (non-draft) PR automatically. Read those
comments. They are not always right and not always worth acting on, but there are frequently hidden
gems in them, so triage them rather than ignoring them, and apply the same finding stance as
section 5. This is one more reason the default is a ready PR, not a draft: a draft may not get the
automated pass.

## 8. Escalate the big stuff; otherwise keep moving

Throughout the effort, escalate to the operator for anything significant: a necessary redesign, a
requirement that turns out wrong, a blocking decision that is the operator's to make, a discovery
that changes the shape or scope of the work, or a smell you cannot resolve cleanly (the `push-back`
and `permission-to-fail` rules). Surface it early and plainly rather than papering over it or
guessing.

Short of that, keep pushing forward as long as the road is clear. The goal is steady, reviewed
progress that the operator can trust without having to drive every step, punctuated by clear
escalations at the moments that actually need a human call.
