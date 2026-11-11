# Harness scope framework: locked

Date: 2026-11-11.

The framework is complete. Harness integrations expose VM, user, workspace and session facets;
VM/admin/agent/workspace setup uses explicit name-keyed activation maps and facet config, while the
session retains one tagged selector. Core supplies actual-owner env. Explicit consumer-owned
required/recommended prerequisites remain distinct from advisory artifact freshness. Independent
setup facets continue after recoverable facet failures and report collected failures at the command
boundary; cancellation, shared persistence and internal failures retain their semantics.

Native plugin/settings setup applies at its defining resource and converges through ordinary typed
instance state, including adoption, explicit retention and safe removal. Applied native state adds
no parent-deletion or workspace-rehome prerequisite. Claude-specific core setup fields are removed.

[Acceptance](acceptance.md#final-functional-acceptance-2026-11-11) reconciles all retained R1-R16
obligations and their exact source/mode evidence. The final independent
[PR 871 report](https://github.com/WayfarerLabs/agentworks/pull/871#issuecomment-6770963076) cleared
`fe2209cf`; earlier framework vertical and policy observations retain their own pins. The
[final plan record](plan.md#final-task-and-validation-record-2026-11-11) records the follow-up
diagnostic cleanup, permanent promotion, current gates and private review. Historical corrected or
partial candidates are not relabeled as accepted releases.

Permanent homes are the harness facet/native setup/idempotency/migration guides and capability,
integration and database READMEs. The SDD directory is not required to operate or extend the
framework. Framework R7/R12/R16 were transferred and completed by the companion artifact effort; MCP
support and single-session workspace modes are subsequent separate efforts.

The documented native cleanup, settings-retention, concurrency and platform limits remain. This lock
claims no model cooperation, universal native-version/provider coverage, native transactions or new
Transport/SSH/provider/VM-restore behavior. Known unrelated issues remain separately tracked.

This lock takes effect when the closeout merges to main. Ordinary pre-merge review and corrections
remain permitted; the normal PR validation and operator merge process still apply.
