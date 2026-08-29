# Delegation

Load this reference before launching a delegate or running concurrent work.

## Charter

Delegate depth, not lead responsibility. A charter names the owned task or artifact, current
`file:line` anchors, governing contracts, definition of done, required gates, and the handoff the
lead needs. The delegate reads HEAD rather than relying on the lead's summary; the lead reads the
returned result and handoff. Decisions, plan problems, and authority boundaries return to the
invoking lead, which decides or escalates through the authority chain.

## Isolation and recovery

Every delegate gets its own worktree, checked out detached at a pinned head, plus independently
namespaced scratch, fixture, and live-test resources. This is not only for delegates that set out to
write: a read-only charter still leaves a delegate free to run an experiment, and a lane that proves
a claim by deleting a piece and seeing what breaks needs somewhere to do that which is not the
lead's tree. Pinning the head also means every lane reviewing one handoff reviews the same bytes. A
worktree isolates git only.

A delegate's worktree lives as long as the delegate session that owns it, so a follow-up round to
the same delegate finds its scratch and fixtures already there rather than rebuilding them. It does
not preserve the bytes a previous round's findings were made against: a follow-up re-pins to the new
head, and a lane needing the old one re-pins to the SHA the handoff recorded. When a session closes,
the lead drops the worktrees it owned. That owner and that event are the retirement rule for a
review worktree, which is detached at a SHA and has no branch whose merge or abandonment could
retire it. A worktree holding claim state retires on its own terms instead: `smol-dev-loop` gates
that teardown on the work having landed and no unpublished state remaining, and session close is
exactly when unpublished state strands, so those are preserved and investigated rather than
collected.

Budget for the environment, not just the tree. A worktree is cheap; what a lane needs to run
anything is not, and in this repository a working `cli/` environment is hundreds of megabytes. A
lane that only reads needs none, a lane that runs the suite, the CLI, or a deletion experiment sets
up its own, and the lead is the one paying for both that setup and the disk a live session holds. An
isolated delegate commits and pushes its own branch, reports branch and head, and leaves integration
to the lead. Relaunch a long-lived lead session before delegating work governed by rules that
changed since it began.

## Capability selection

Choose capability and reasoning depth deliberately for each task. Use a lighter capability for
mechanical or low-ambiguity work, a standard capability for well-defined implementation, and the
strongest available capability only when the reasoning warrants it. Name the selection when the
harness permits it; inheritance is a choice, not an accidental default.

The reviewer of record has at least the implementer's capability and reasoning depth. An independent
fresh-eyes reading may be lighter because it is complementary, not the reviewer of record. A
complexity pass is complementary too, but its verdicts are design judgments, so scale it to the
judgment the change demands rather than to the implementer's tier. Adapt launch syntax and role
identifiers to the harness rather than treating any one harness's names as universal.
