# Bootstrap fallback removal: premature lock correction 2026-08-10

This artifact was added before the required live lifecycle crux had run. The merge-intent claim is
withdrawn until an operator authorizes that bounded run or records an explicit process exception.
Phase 4 and the final closeout checkbox are reopened in `plan.md`; this file remains in place to
preserve the auditable correction rather than erase the premature round.

The offline implementation itself remains review-clean. The vm-platform contract is version 2 and
every shipped platform now completes required-key bootstrap or raises inside its platform-owned
rollback window. The generic Phase A generated-script fallback,
`ProvisionResult.bootstrap_complete`, and `BootstrapCompletion` are structurally absent. Phase A
only rediscovers an IP when needed, records state, verifies Tailscale SSH, and performs its
non-fatal post-ready work.

The final implementation preserves the provider-specific secret boundaries: Azure, AWS, and Lima use
fixed stdin; WSL2 retains private local/guest staging with transcript redaction; Proxmox retains its
accepted guest-agent staging. Reflected WSL2 copy diagnostics are replaced with a fixed,
context-free error. Readiness/bootstrap failures, first interrupts, cleanup failures, and second
interrupts preserve the documented rollback, survivor, and manual-recovery semantics. The manager
owns one fully redacting create logger and cannot replace an active primary failure during close.

Exact closeout evidence on the current-main integrated tree before this record:

- 183 focused manager/platform/provider-retention tests passed;
- 7,623 non-integration tests passed in parallel;
- Ruff check and format passed across 662 files;
- strict mypy passed across 296 source files;
- file lint, Rulesync drift, locked-SDD validation, and diff checks passed;
- the required project reviewer and independent fresh-eyes reviewer were clean after all findings;
- the draft checkpoint received saga-lead and mutation-tested integration dispositions of PASS on
  exact head `0fb2a3fc4d702fe3bf3f03011df7dbfb166869df`;
- the reachable closeout clarification commits are `f2e3f40115e348002673aa596461648a4a0326d3` and
  `c8ff2b1398e2c2adefdf4362b5873eef687e6f8e`;
- the required generic fresh-eyes substitute for the current published head is pending in this
  correction round because Copilot quota exhaustion prevented the usual closeout-head check.

No #475 live VM or cloud mutation has occurred. A remote-Lima backend is available, so the standing
integration-testing process requires an `agw-state` snapshot plus one bounded foreground lifecycle
run and independent cleanup verification unless the operator explicitly grants a recorded process
exception. This document is not the final lock until that open item and the fresh-eyes evidence are
resolved. The PR body retains `Closes #471`, so the issue closes only when the operator eventually
merges the fully evidenced PR.

-- agw-ns-gcp-platform (effort lead)
