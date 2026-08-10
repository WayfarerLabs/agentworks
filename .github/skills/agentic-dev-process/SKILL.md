---
name: agentic-dev-process
description: >-
  How we drive a development effort end to end: sizing the work, SDD for large
  efforts, delegated implementation, per-step review, and when to escalate
---
# Agentic Development Process

This is the top-level playbook for how a development effort runs, from a standing start to a
merge-ready PR. It is written for whoever is **driving** the effort; a delegated subagent gets its
lane from its persona and its invoking prompt, not from here. The playbook ties three things
together: the `sdd` skill (how we spec significant work), the `agentworks-dev` subagent (who
implements), and the `agentworks-reviewer` subagent (who checks). The always-on rules (already in
your context) and the repo's `CONTRIBUTING.md` cover the mechanics (code style, linting,
conventional commits); the `development-principles` rule covers _how_ to write the code. This skill
covers the flow that sits above both.

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
  out bigger than they looked. Implementing directly changes who writes the code, not the bar it is
  held to: the always-on `development-principles` rule applies to the lead directly, and skipping
  the delegation does not waive it. (The `agentworks-dev` persona is that rule embodied for
  delegated implementation, plus the delegation-specific lane.)
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
- **Isolate parallel subagents from each other.** Subagents launched from one session share the git
  checkout, so two writers collide by construction. Launch any subagent that mutates files with
  worktree isolation so it works on its own copy of the repo; this is mandatory when more than one
  file-mutating subagent runs at once, and cheap insurance even for a single one (the lead's own
  checkout stays quiet). Worktrees isolate only git: any shared temporary filesystem (a harness
  scratchpad, a shared temp or fixture directory) must be subdivided the same way. Charter each
  subagent to create and stay inside its own namespaced subdirectory (for example `<task-slug>/`
  under the shared root), and anything fixture-sensitive (like a tester) to create a fresh temp
  directory under that. Isolation changes where the dev's commits land: git refuses to check out a
  branch already checked out in another worktree, so an isolated dev commits on its own branch
  (usually the one its worktree starts on), pushes it so nothing is hoarded locally, and reports
  branch and head SHA; integrating that branch back onto the effort's branch is the lead's step, not
  the dev's, and doing it promptly keeps section 6's no-hoarding rule satisfied end to end.

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

**Pick reasoning effort with the model tier, where the harness exposes it.** Effort is a second
selection axis distinct from the tier, chosen per launch just as deliberately: low effort for
mechanical, well-charted tasks; higher effort reserved for genuinely hard reasoning. The
reviewer-at-least-as-capable rule spans both axes (tier and effort): do not review high-effort work
with a low-effort reviewer.

**Pass the tier explicitly on every launch.** The subagent definitions ship `model: inherit`, so a
launch that names no model runs the subagent at whatever tier the lead happens to be on. That is how
the reviewer >= dev rule breaks in practice: not by anyone choosing a weaker reviewer, but by nobody
choosing at all. Launch a dev at the top tier for a tricky step, leave the reviewer's model unset,
and the reviewer quietly runs at the lead's tier instead, which may be lower. Nothing in the output
says which tier ran, so the mismatch never announces itself. Name the model, and the effort where
exposed, on each launch, dev and reviewer alike, and inherit only when inheriting is the deliberate
choice.

## 5. Review every step

Every development step gets reviewed by the `agentworks-reviewer` subagent before you consider it
done, at the model tier from section 4 (reviewer >= dev). This holds for delegated steps and for
small changes you make directly. Give the reviewer what the diff cannot show it: who held which role
(you, or a delegated dev) and whether this PR is meant to merge as-is. Its SDD-process check turns
on both, and neither is recoverable from the changes themselves.

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

### Periodically review the process docs as a whole

Every review above is incremental: it validates one change against the tree as it stood. That is
precisely how contradictions _between_ process documents accumulate invisibly, because each change
can be locally correct and still quietly disagree with a document nobody reread. So periodically,
after a burst of process changes, before locking a saga-level effort, or whenever the operator asks,
run one comprehensive consistency review over the whole process tree: skills, rules, and subagent
definitions together, in a single pass.

Run it as an `agentworks-reviewer` subagent in its consistency-review mode (defined in that
subagent: six categories, composition failures chief among them), in a fresh context, launched
explicitly at the top tier (section 4 applies here too: name the model, do not inherit). Never use
the context that authored the changes; the whole point is a reader who has to work the tree out from
what it says. It hunts pairwise contradictions, rules that silently override one another, gaps where
one document assumes something another never establishes, and cross-references gone stale. Porting
the process docs into a separate context and having independent reviewers read them as outsiders is
a proven technique here: it surfaced four live contradictions that per-change reviews had passed.
Findings route like any other review; triage them, push back on the wrong ones, and fix the valid
ones.

## 6. Commit, push, and PR

- **Commit and push at regular intervals.** Do not hoard work in a local branch; frequent, honest
  commits keep the work reviewable and recoverable. Follow the project's Conventional Commits
  convention (`CONTRIBUTING.md`) for message shape.
- **One PR per feature is the default, with a size ceiling.** Put the whole feature in a single PR,
  SDD artifacts included. Split into multiple PRs only when there is a good reason. The usual one is
  legitimate SDD phases that each carry independent, standalone value; another is cross-effort
  visibility, per the `sdd` skill's merge-artifacts-early guidance: when another effort could build
  against your design (under an active saga, assume one can), the SDD artifacts land on `main` ahead
  of the implementation instead of riding the feature branch to the end. A phase that only has value
  once a later phase lands is not a reason to split; it is a commit within the one PR. Always-green
  phased commits give reviewers a natural commit-by-commit reading order inside a single large PR.
  The ceiling: when a feature's projected diff grows past what one reviewer can actually hold (as a
  rough guide, a few thousand lines of substantive change), the default flips and the effort ships
  as a PR series of always-green phases. Plan the split at plan-writing time, not when the branch is
  already huge; review depth decays faster than diff size grows, and a monster PR forces the review
  to happen after the design has hardened, when findings are most expensive to act on.
- **Within a PR series, stack dependent phases; don't wait for merge.** The expensive deltas come
  from review, not from merge, so the gate for building phase N+1 on phase N is the dust settling on
  N: its major review findings incorporated and re-review clean, not its merge. Before that gate,
  stacking bets against exactly the reshaping a review can force; after it, the remaining churn is
  mechanical and stacking is preferred for parallel-yet-dependent work. Keep the stack shallow (one
  not-yet-merged layer at a time; a stack of unreviewed PRs is the big PR wearing a disguise), and
  the stack's owner carries the rebases and retargets the base branch as predecessors merge. Work
  that does not actually depend on the unmerged phase branches off `main` as a sibling instead, and
  design-time work (LLDs, content, research) needs no branch gate at all: paper does not rebase.
- **Open a PR when the work is close to merge-ready**, not before. A PR signals "this is ready for
  eyes," so open it when that is true.
- **Non-draft by default.** Avoid draft PRs unless specifically asked for one. The single routine
  exception is the SDD pre-implementation artifact review, which the `sdd` skill runs as a draft PR
  on purpose (section 2).
- **Draft/ready is the re-review signal** (operator convention, 2026-08-10). When starting a new
  round of edits on an existing PR, toggle it to draft immediately, before pushing any changes, so a
  ready PR's head is always its complete handoff state. Toggle back to ready only after two things
  are true: every change of the round is pushed, and a comment is posted detailing the changes, the
  rationale, and any pushbacks on previous review findings. The toggle says _when_ to re-review, the
  comment says _what changed and why_; a ready flip without both is a false signal. Reviewers treat
  the draft-to-ready transition as the re-review request; nobody has to infer from push traffic
  whether work is mid-flight. The convention supports both consumption styles: edge-trigger on the
  transition itself, or poll ready PRs and remember the last-reviewed head per PR; a new head on a
  ready PR always means there is something to review, so a consumer that missed the edge loses
  nothing.
- **Checkpoint reviews use the `review-requested` label** (operator convention, 2026-08-10). Ready
  keeps its full meaning: round complete, handoff comment posted, believed mergeable. When work that
  is NOT at merge intent needs eyes now (the `sdd` skill's phased artifact reviews on a draft PR, a
  pre-implementation schema gate, a mid-effort design consult), apply the `review-requested` label
  to the draft PR together with a comment scoping what to review. The reviewer removes the label
  when the review is delivered, so the label is simultaneously the request and the pending state:
  self-clearing, never stale. Consumers watch label events or poll
  `gh pr list --label review-requested`.

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
  together. This pass is deliberately exempt from section 4's reviewer-tier floor: it is a
  complementary lens, not the reviewer of record, which stays bound by that floor.

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
