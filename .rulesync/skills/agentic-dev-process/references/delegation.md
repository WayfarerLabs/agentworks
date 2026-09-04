# Delegation

Load this reference before launching a delegate or running concurrent work.

## Charter

Delegate depth, not lead responsibility. A charter names the owned task or artifact, current
`file:line` anchors, governing contracts, definition of done, required gates, the handoff the lead
needs, and, for a delegate that will commit or post on its own, the identifier it signs and trails
with (the `message-signatures` rule). The delegate reads HEAD rather than relying on the lead's
summary; the lead reads the returned result and handoff. Decisions, plan problems, and authority
boundaries return to the invoking lead, which decides or escalates through the authority chain.

## Isolation and recovery

Every delegate gets its own worktree, started from a named SHA, plus independently namespaced
scratch, fixture, and live-test resources. This is not only for delegates that set out to write: a
read-only charter still leaves a delegate free to run an experiment, and a lane that proves a claim
by deleting a piece and seeing what breaks needs somewhere to do that which is not the lead's tree.
A review lane stays detached at that SHA, so every lane reviewing one handoff reviews the same
bytes. A delegate that writes takes its own branch from it, because it has to commit and push and
report what it pushed. A worktree isolates git only.

A delegate's worktree and the environment inside it go under the workspace, so that deleting the
workspace takes them too. Where exactly is the harness's choice, provided the place reads as
temporary to someone who finds it, and is either outside every repository or ignored by the one it
sits in. A workspace root is usually itself a clone, so ignored is the common answer. It lives as
long as the delegate session that owns it, so a follow-up round finds its scratch and fixtures
already built. It does not preserve the bytes a previous round's findings were made against: a
follow-up re-pins to the new head, and a lane needing the old one re-pins to the SHA the handoff
recorded.

The lead drops what its session owned when that session closes. That is the cleanup, and it is what
keeps disk bounded while an effort runs. The location is the backstop for the case the lead cannot
cover: a session that crashed or ran out of context drops nothing, and a deleted workspace takes
everything under it. Treat that as a last resort rather than the plan. `/tmp` does not serve as one,
because it survives until the machine reboots and these machines are long-lived.

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
