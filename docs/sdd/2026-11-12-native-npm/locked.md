# Native npm support closeout

2026-11-12: implementation and private review are complete. Admin and agent templates support native
per-user npm packages, an enable toggle, and configurable minimum registry release age. Node.js/npm
provisioning stays explicit. Mise is optional at VM scope; both package installers precede custom
user commands with their executables available through the managed profile.

The tested production head is `6d6289f2672b29bda0b6b1aa28d5f5685eb69f26`. The full non-integration
suite passed 13,140 tests with 35 skips. Native npm 11.16.0 and 12.2.2 registry fixtures, actual
Bash login behavior, static checks, website gates, and three independent review lanes are recorded
in the plan. Remote VM create/reinit was not exercised. Age filtering follows npm registry
publication timestamps; non-registry dependency sources have no age guarantee.

Permanent operator guidance lives in `docs/guides/npm.md` and the resource field descriptions. No
implementation work remains in this effort.

2026-11-13: publication preparation rebased the completed feature onto current main without changing
its implementation. The plan records fresh focused and static verification; the PR carries
current-head CI and feedback-round evidence.
