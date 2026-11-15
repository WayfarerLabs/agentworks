# Direct artifact declarations: implementation plan

This effort implements the operator-approved response to #907 in one complete PR from main
`432766fa`. It uses a new SDD because the authoring and inheritance contract spans all artifact types
and owning scopes. Existing locked efforts are unchanged. Requirements and architecture are recorded
in `frd.md` and `hla.md`; the additive transition is in `migration-strategy.md`.

- [ ] Share the five-map content schema between bundles and direct owner declarations; implement
      strict authoring and ordinary map/entry merge, null clearing and bundle-list independence.
- [ ] Feed effective direct content through shared capture after bundles, preserving acquisition
      limits, normalized IDs, bundle-only fingerprints and persisted format compatibility.
- [ ] Update offline inspection, current selection, replacement evidence and safe diagnostics for
      direct content, including absent and stale captures.
- [ ] Verify every owning template/instance path, schema/reference extraction, source forms,
      override/clear combinations and representative existing delivery/cleanup behavior.
- [ ] Ship permanent guides, schema teaching and usable examples; verify installed guide/package
      parity without accessing operator state or provisioning resources.
- [ ] Complete independent project, Muntz and generic correctness reviews of a pinned source,
      assess findings, run required gates and retain original failures and corrected evidence.
- [ ] Publish a complete green head and scoped handoff, mark ready without `review-requested`,
      monitor the independent integration pipeline and critically assess its actual report.

No automatic merge or public feedback/fix budget is inferred from the earlier MCP correction's
allowance. The separate integration-test session owns its live inventory and resource budget.
Record its coverage and limitations before considering closeout; do not claim source tests prove
native loading or a successful model conversation.
