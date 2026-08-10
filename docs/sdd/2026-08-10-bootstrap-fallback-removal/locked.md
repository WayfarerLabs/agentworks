# Bootstrap fallback removal: locked 2026-08-10

Issue #471 is complete in the merge-intent PR. The vm-platform contract is version 2 and every
shipped platform now completes required-key bootstrap or raises inside its platform-owned rollback
window. The generic Phase A generated-script fallback, `ProvisionResult.bootstrap_complete`, and
`BootstrapCompletion` are structurally absent. Phase A only rediscovers an IP when needed, records
state, verifies Tailscale SSH, and performs its non-fatal post-ready work.

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
  exact head `0fb2a3fc4d702fe3bf3f03011df7dbfb166869df`, and the two requested closeout
  clarifications were independently re-reviewed clean through commit
  `1691bd7ebda427c2288a10974fffc7e8d50dfb13`.

No live VM or cloud mutation was required for this provider-shaped contract correction. The PR body
contains `Closes #471`, so the issue closes when the operator merges the PR.

-- agw-ns-gcp-platform (effort lead)
