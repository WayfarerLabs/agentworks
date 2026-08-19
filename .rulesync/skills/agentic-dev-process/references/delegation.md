# Delegation

Load this reference before launching a delegate or running concurrent work.

## Charter

Delegate depth, not lead responsibility. A charter names the owned task or artifact, current
`file:line` anchors, governing contracts, definition of done, required gates, and the handoff the
lead needs. The delegate reads HEAD rather than relying on the lead's summary; the lead reads the
returned result and handoff. Decisions, plan problems, and authority boundaries return to the
invoking lead, which decides or escalates through the authority chain.

## Isolation and recovery

Concurrent file-mutating delegates require isolated checkouts and independently namespaced scratch,
fixture, and live-test resources. A worktree isolates git only. An isolated delegate commits and
pushes its own branch, reports branch and head, and leaves integration to the lead. A delegate in a
shared checkout commits only where its charter permits. Relaunch a long-lived lead session before
delegating work governed by rules that changed since it began.

## Capability selection

Choose capability and reasoning depth deliberately for each task. Use a lighter capability for
mechanical or low-ambiguity work, a standard capability for well-defined implementation, and the
strongest available capability only when the reasoning warrants it. Name the selection when the
harness permits it; inheritance is a choice, not an accidental default.

The reviewer of record has at least the implementer's capability and reasoning depth. An independent
fresh-eyes reading may be lighter because it is complementary, not the reviewer of record. Adapt
launch syntax and role identifiers to the harness rather than treating any one harness's names as
universal.
