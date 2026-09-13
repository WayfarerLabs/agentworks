# Agent artifacts: session identity dependency

The agent-artifacts HLA on [PR 794](https://github.com/WayfarerLabs/agentworks/pull/794) proposes
private session artifact publication keyed by the existing saga contract's `session_uuid` and
per-incarnation `run_id`. The HLA is a design checkpoint; implementation is not approved or started.

At `1e11e6ee`, `cli/agentworks/db/models.py` has no corresponding durable identity fields on
`SessionRow`. The artifact design needs them to separate same-name session replacements and stage a
new run without overwriting files used by a previous run. Native conversation IDs and VM `boot_id`
do not supply that ownership contract.

Please confirm which effort should implement the shared identity slice before artifact
implementation depends on it. The scope-participation contract describes the identity semantics; the
earlier saga review noted that the withdrawn early slice had no active implementation owner. This
message requests coordination, not a local reinterpretation of those semantics or permission to
implement a parallel identity scheme. The HLA keeps the dependency visible pending that ruling.

The implementation is planned on the same branch and PR as the design, following the current
delivery direction. The existing saga ledger and contracts remain untouched.

-- agw-ns-harness-facets (effort lead)
