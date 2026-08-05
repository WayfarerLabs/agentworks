---
name: smol-dev-loop
description: >-
  The autonomous issue-to-mergeable-PR loop: scan smol-dev issues, vet them, drive each through the
  standard development process, cycle the PR to green, and hand it back for the operator to merge
---

# Smol Dev Loop

This is the outer loop that turns a queue of `smol-dev` issues into reviewed, mergeable pull
requests, one at a time and hands-off up to the merge. It does not redefine how a change gets built:
that is the `agentic-dev-process` skill, which this loop invokes for each issue. What this skill
adds is only the four things that skill does not cover:

1. issue intake and the vetting gate (sections 2 and 3),
2. the `wfscot` integration tester as a required voice on the PR (section 5),
3. the repeat-until-drained loop and its pacing (sections 1 and 9), and
4. the autonomy ceiling: stop at approved-and-green and hand off; never self-merge (section 7).

It is orchestrated by the main agent (the lead), not by a subagent. The lead keeps the authority to
vet an issue, to push back on a reviewer, and to escalate to the operator; it delegates the depth
(implementation, review, integration testing) to the `agentworks-dev`, `agentworks-reviewer`, and
`agentworks-tester` subagents exactly as `agentic-dev-process` prescribes.

## 1. The loop

1. **Pick.** Find the next `smol-dev` issue (section 2).
2. **Vet.** Decide whether it is well-specified and self-contained. If not, comment and skip
   (section 3).
3. **Build.** Run it through `agentic-dev-process` end to end: size the work, take the SDD track if
   it is significant, delegate implementation, review every step, commit and push (section 4).
4. **Open PR.** Non-draft, so Copilot and wfscot engage (section 5).
5. **Cycle.** Gather and address feedback from `agentworks-reviewer`, Copilot, and wfscot until all
   three are satisfied and CI is green (sections 5 and 6).
6. **Hand off.** Pause and notify the operator that the PR is approved and green, ready to merge. Do
   not merge it (section 7).
7. **Repeat.** On the next wake, return to step 1 for the next issue.
8. **Idle.** When no `smol-dev` issue remains and nothing is in flight, do not stop; switch to a
   long-poll cadence and wake periodically to re-check. On the empty-to-nonempty transition (a new
   `smol-dev` issue appears), return to step 1 and resume. Prompt pickup is not required.

One issue is in flight at a time. The loop is serial by design: carry an issue to hand-off before
picking up the next.

## 2. Finding work

- Scan open issues labeled `smol-dev` in the project repository. Take them oldest-first unless an
  issue is explicitly prioritized.
- Skip an issue that already has an open PR, is assigned to a human, or was previously
  commented-and-skipped by this loop with no new information since.

## 3. Vet before building (the intake gate)

This gate is the single most important quality lever in the loop. Before writing any code, read the
issue and decide one thing: is it well-specified and straightforward enough to implement without
guessing at the operator's intent?

Well-specified and straightforward means all of:

- The desired end state is clear and testable; you can state the definition of done in a sentence.
- It is self-contained: no unresolved design decision that is the operator's to make, and no
  dependency on unmerged or unspecified work.
- It fits an existing pattern or is a modest, low-risk change. It may still run the SDD track if it
  is significant, but the problem and scope must be clear enough to spec honestly.

If the issue clears the gate, proceed to section 4.

If it does not (ambiguous intent, missing acceptance criteria, a hidden design fork, or scope that
balloons on inspection), do not start building. Post a single, specific comment on the issue that
names exactly what is blocking it and what would unblock it, then move on to the next issue.
Friction is a skip, not a grind (`ask-questions`, `push-back`).

## 4. Build through the standard process

Hand the vetted issue to the `agentic-dev-process` skill and follow it fully: size the work, run the
SDD track for anything significant, delegate implementation to `agentworks-dev`, review every step
with `agentworks-reviewer` (reviewer tier at least the dev tier), and commit and push at regular
intervals. That skill is the source of truth for how the change is built and internally reviewed;
this loop does not restate it.

Branch and PR hygiene follows `agentic-dev-process` section 6: one PR per issue by default, opened
non-draft when the work is close to merge-ready.

## 5. The PR review cycle: three voices

A ready (non-draft) PR draws the full review panel. All three voices must be satisfied before
hand-off:

- **`agentworks-reviewer`**: the project-values-and-conventions review, run here on every code-heavy
  change per `agentic-dev-process` section 5.
- **Copilot**: the fresh-eyes generic pass, automatic on a ready PR (`agentic-dev-process` section
  7). If Copilot is unavailable, substitute the generic reviewer on a lower model that that section
  prescribes.
- **`wfscot` (integration tester)**: the `agentworks-tester` persona exercising the real CLI against
  live backends and reporting its findings on the PR as wfscot. This is the voice
  `agentic-dev-process` does not cover: behavior observed against a running system, not the diff
  read statically. Invoke it with a scoped charter, an environment inventory, and a resource budget,
  per that subagent's contract.

Because Copilot and wfscot answer on wall-clock latency, the loop opens the PR and then sleeps and
polls (section 9) rather than holding a blocking wait.

## 6. Addressing feedback

Apply the same stance as `agentic-dev-process` section 5 to all three voices:

- Push back on any finding that is genuinely wrong; a reviewer, Copilot, or the tester is not
  infallible, and following a wrong finding makes the code worse. State the disagreement plainly on
  the PR.
- Otherwise err on the side of addressing anything valid, down to the minor and the merely-nicer.
- Route fixes by ownership: findings on code loop back to the implementing `agentworks-dev` subagent
  (it keeps the context and the authorship); findings on a lead-owned artifact are the lead's to
  apply.
- After a fix, re-request or re-run the relevant voice, and keep cycling until no valid finding is
  outstanding and CI is green.

## 7. The stop line: approved and green, then hand off

When `agentworks-reviewer`, Copilot, and wfscot are all satisfied and CI is green, the PR is done as
far as this loop goes. Pause and notify the operator that the PR is ready to merge; do not merge it.
The merge is the operator's, and it is the one irreversible step this loop deliberately leaves to a
human. On the next wake, return to section 2 for the next issue.

## 8. Escalate the big stuff; otherwise keep cycling

Escalation pauses the loop and surfaces to the operator immediately, mid-flight, rather than waiting
for hand-off. Trip it on any of:

- a finding that reveals the issue itself is wrong, out of scope, or needs a design decision that is
  the operator's (`push-back`);
- a disagreement with a reviewer or with wfscot that the loop judges it should not override on its
  own;
- a smell it cannot resolve cleanly, or CI that stays red after a reasonable number of honest
  attempts (`permission-to-fail`);
- anything that would widen the blast radius beyond the issue as it was vetted.

Short of escalation, keep the loop moving: vet, build, cycle to green, hand off, next. The value is
a steady stream of mergeable PRs the operator can trust without driving each step, punctuated by
clear stops at the two moments that need a human: the merge, and a genuine escalation.

## 9. Running it

The loop runs as a self-paced main-agent loop (`/loop`):

- Between opening a PR and its feedback arriving, sleep and wake to poll PR review state and CI
  rather than holding a blocking wait. A cadence on the order of a few minutes fits Copilot and CI;
  a wfscot integration run may take longer. Tighten or relax the interval to match what is actually
  being awaited.
- When the `smol-dev` queue is empty and nothing is in flight, switch to a long-poll cadence (on the
  order of tens of minutes) rather than stopping. The loop keeps resting-checking and picks back up
  automatically when a `smol-dev` issue appears; it does not need to react quickly, only reliably.
- The operator can interrupt at any time.
