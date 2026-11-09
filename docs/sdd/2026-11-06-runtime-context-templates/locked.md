# Runtime context templates: implementation closeout

Date: 2026-11-09.

PR #867 delivers the full namespaced runtime-context feature, this SDD, and permanent documentation
in one change. Plaintext env winners and declared command fields use `${agw:NAME}` against explicit
consuming-user/resource context. Secrets remain opaque, rendering is one pass, and offline env
inspection shares the plaintext projection. Shell preflight renders its selected field before
workload/env secret resolution; command behavior is established by execution.

The permanent contract is [runtime context and interpolation](../../arch/runtime-context.md).
Operational details also live with the CLI, schemas, integration documentation, guide signposts, and
sample config. None requires this SDD to remain present.

This records implementation completion, not whole-PR validation clearance. The completed campaign on
`425d015be071a91b5c17235fcb0daa96545dc9cd` passed all required gates and broad Linux live cases,
with explicit failures and coverage gaps recorded in [the plan](plan.md). This final correction
fixes resume-command error attribution and supplies release-visible namespace metadata. The final
ready-head report remains required before merge disposition. No merge or acceptance waiver is
authorized here.

Inherited SSH literal-delivery behavior is tracked in issue #873 under the operator's instruction to
leave transport work to its owner. Quoted global workspace-root initialization and inert sockets
after refused native launches remain recorded for VM initialization and session lifecycle ownership.
Quota-blocked or untested authenticated modes and missing exact-head Windows/macOS coverage are not
passes. This closeout does not resolve or hide those cells.

The lock takes effect at merge. While the branch remains unmerged, review corrections may still
amend the SDD under the normal workflow.
