# Direct artifact declarations: implementation plan

This effort implements the operator-approved response to #907 in one complete PR from main
`432766fa`. It uses a new SDD because the authoring and inheritance contract spans all artifact
types and owning scopes. Existing locked efforts are unchanged. Requirements and architecture are
recorded in `frd.md` and `hla.md`; the additive transition is in `migration-strategy.md`.

- [x] Share the five-map content schema between bundles and direct owner declarations; implement
      strict authoring and ordinary map/entry merge, null clearing and bundle-list independence.
- [x] Feed effective direct content through shared capture after bundles, preserving acquisition
      limits, normalized IDs, bundle-only fingerprints and persisted format compatibility.
- [x] Update offline inspection, current selection, replacement evidence and safe diagnostics for
      direct content, including absent and stale captures.
- [x] Verify every owning template/instance path, schema/reference extraction, source forms,
      override/clear combinations and representative existing delivery/cleanup behavior.
- [ ] Ship permanent guides, schema teaching and usable examples; verify installed guide/package
      parity without accessing operator state or provisioning resources.
- [ ] Complete independent project, Muntz and generic correctness reviews of a pinned source, assess
      findings, run required gates and retain original failures and corrected evidence.
- [ ] Publish a complete green head and scoped handoff, mark ready without `review-requested`,
      monitor the independent integration pipeline and critically assess its actual report.

No automatic merge or public feedback/fix budget is inferred from the earlier MCP correction's
allowance. The separate integration-test session owns its live inventory and resource budget. Record
its coverage and limitations before considering closeout; do not claim source tests prove native
loading or a successful model conversation.

The code lane `5dbc397b` was integrated as `55fd88eb`. Its focused artifact/setup selection passed
1,782 tests with five platform skips, with 27 incremental direct/native cases passing afterward.
These selections overlap. Ruff check and format passed, and full mypy passed across 1,069 files.
Initial new-test fixture errors, one test typing failure and their successful corrections remain
retained in the development evidence; they are not reported as a green original run. The lead read
the full developer report and independently checked its owned environment/cache/fixture cleanup.

The nested null clear required a narrow instance-spec admission change at `artifacts.declare`;
template merging already preserves it. Per-entry tombstones and unrelated nulls remain refused.
Actual session-manager replacement, persisted restart and clear paths are covered. Across the three
native MCP formats, moving equal bundle content to direct declarations preserves the input ID,
ordinary claim tuple and native entry bytes. A separate lead check loaded and resolved the new
worked manifest without acquisition, proving its inherited and cleared selections.

The initial documentation lint found one spelling issue after a read-only default package-cache
startup refusal. The package tool now uses an owned cache, and the wording was corrected. The
complete integrated gate run, package parity, private review and public handoff remain owed below.
