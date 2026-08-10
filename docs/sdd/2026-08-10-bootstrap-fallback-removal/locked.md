# Bootstrap fallback removal: locked 2026-08-10

Issue #471 is complete in the merge-intent PR. An earlier version of this artifact claimed closure
before the required live lifecycle crux and cited one unreachable review commit. That premature
claim was withdrawn, Phase 4 was reopened, the published evidence was corrected, and the bounded
live run has now passed. The correction remains part of the branch history rather than being erased.

The vm-platform contract is version 2 and every shipped platform now completes required-key
bootstrap or raises inside its platform-owned rollback window. The generic Phase A generated-script
fallback, `ProvisionResult.bootstrap_complete`, and `BootstrapCompletion` are structurally absent.
Phase A only rediscovers an IP when needed, records state, verifies Tailscale SSH, and performs its
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
- the required generic fresh-eyes substitute reviewed published code head
  `dbeff38bf8311bbb97b00f5e993f7962ae4aa9f6` clean after running 200 focused tests, Ruff, strict
  mypy across 296 source files, the full 7,623-test non-integration suite, and diff checks; this
  substitutes for the usual closeout-head Copilot check, which could not run after quota exhaustion.

The operator-gated live run passed at exact draft head `9fbfdb01c3ad65d2ede5c142c35b28e3eecbb9da`,
current base `3c2184f54217bb15066c9ce8d8ed551550bb7a59`, and synthetic merge
`3f9b239b78960d04b348b551fbd84adb2c40bbdf`:

- 258 focused lifecycle/security tests, Ruff, strict mypy, file lint, locked-SDD validation,
  Rulesync drift, diff checks, and all required forge checks passed;
- one isolated-home remote-Lima micro VM created successfully, completed platform-owned Tailscale
  join plus reboot/reconnect and initialization, and reported ready with the requested 1 CPU, 1 GiB
  memory, and 10 GiB disk;
- two independent `agw vm exec` calls succeeded with the same measured boot ID;
- the retained Lima request and 3,662 bounded isolated artifacts contained no key-like value or auth
  token;
- delete completed Tailscale deregistration, Lima deletion, log removal, SSH sync, and database
  deletion;
- independent cleanup proved zero candidate/remote Lima instances, instance directories, temporary
  artifacts, detached processes, live matching tailnet nodes, SSH references, workspaces, logs, or
  runtime/entity rows; SQLite integrity was `ok`, schema version 31, and foreign-key violations were
  zero;
- one expected offline tailnet record remained with `Online` false and no ping reply.

The non-blocking isolated-HOME known-hosts write warning is outside #475's bootstrap contract and is
tracked for the broader isolation work in #484. The PR body contains `Closes #471`, so the issue
closes when the operator merges the PR.

-- agw-ns-gcp-platform (effort lead)
