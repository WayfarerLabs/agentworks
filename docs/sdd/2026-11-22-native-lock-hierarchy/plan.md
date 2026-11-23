# Native lock hierarchy: plan

Status: design in progress. The [FRD](./frd.md) is accepted; the [HLA](./hla.md) and the
[LLD](./lock-lld.md) are drafts in the design PR.

Two PRs: the design PR (#949) carrying the FRD, HLA, LLD and this plan, then one implementation PR
stacked on `main` once the design merges. The implementation lands whole: the single guard and its
token are replaced in one step, with no old and new lock paths side by side on `main`.

## Design

- [x] Draft the FRD and have the operator accept it (2026-11-23).
- [x] Write the HLA and the LLD, including the per-command lock table at HEAD.
- [ ] Run the private review loop on the design (project review and complexity pass), address its
      findings, then mark #949 ready and address published feedback.
- [ ] Merge the design PR.

Done when the HLA, LLD and plan agree with each other and with the code at HEAD, no review finding
is left undisposed, and #949 is merged.

## Implementation

- [ ] The primitive: the `native_locks` package (entities, modes, ordered fail-fast acquisition,
      extension, nested reuse), POSIX `flock` and Windows `LockFileEx`, holder records and the busy
      error naming its holder, replacing `harness_setup/locking.py`.
- [ ] Guest and workstation critical sections: the account-edit helper with its root-only guest
      `flock` and typed timeout, `userdel` split from the home removal, and the per-user rebuild
      mutex for the SSH config and VS Code workspace files.
- [ ] Activation: gate-first and runtime-first patterns in the activation gate and `LiveVMNode`,
      with admission-only shared locks for access commands.
- [ ] Every call site in the LLD's per-command table moved to its lock set, the unguarded mutators
      (grant and revoke, workspace repair, workspace copy, `vm import-host-keys`,
      `vm confirm-release`) guarded, and each identity re-check in place.
- [ ] Prompts moved before acquisition per the LLD's R6 table, with the three exceptions announcing
      themselves.
- [ ] Confirm that every harness integration writes only inside the entity that owns it.
- [ ] Tests: the conflict matrix at the primitive on POSIX and Windows, a crashed holder, the
      older-version interlock, holder naming, extension failure unwinding, nested reuse, and one
      concurrent pair per FRD story through real services; existing lock tests updated.
- [ ] Collateral: every doc the HLA's Delivery section lists, and the upgrade guide entry.
- [ ] Private review loop, integration test on Linux, macOS and Windows workstations against at
      least one VM, and published feedback addressed.

Done when the implementation PR is merged with green CI on all platforms, the tester has driven each
FRD story concurrently on a live VM (two unrelated operations succeed together, a conflicting one
fails at once naming its holder), and no doc describes the single guard.

## Closeout

- [ ] Write `locked.md`.
