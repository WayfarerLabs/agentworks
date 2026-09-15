# Harness facet ruling and successor routing

The operator directed the harness-scope-framework lead to make explicit activation of an
unimplemented setup facet a hard error. This supersedes the base no-op rule in
[scope-participation-contract.md](scope-participation-contract.md). Please reconcile the saga-owned
contract with that decision. The implementation and amended
[FRD R3](../2026-09-06-harness-scope-framework/frd.md#functional-requirements) travel together in
[PR 761](https://github.com/WayfarerLabs/agentworks/pull/761). An implemented facet may succeed
without changes; inactive integrations receive no successful applied record. Retirement of prior
owned effects retains its existing reconciliation path. The base setup methods reject unsupported
invocations; there is no separate support registry or performed-work flag.

The operator also approved the successor fallback: without activation of an integration's VM facet,
all defined inputs remain unhandled and go directly to the session facet. Core does not guess user
versus workspace placement or broadcast through both. An inactive intermediate facet likewise leaves
its applicable routed inputs for the session. Resolution can be lazy for the session's selected
integration and actual ancestors, reusing active ancestor results without rerunning setup or eagerly
accounting for every inactive integration during agent initialization. Preserve origin and avoid
duplicate delivery. Setup completion does not imply an input was handled.

The
[FRD successor context](../2026-09-06-harness-scope-framework/frd.md#future-artifact-context-not-this-efforts-contract)
records this direction and the remaining design questions. Artifact formats and routing are still
successor work; this PR introduces no artifact API. This message is for pickup through main when the
PR lands, not a claim that the saga contract has already been updated.
