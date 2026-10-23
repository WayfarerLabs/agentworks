# Independent SSH carrier: locked, superseded

**Locked:** 2026-10-20

This effort stopped at operator direction on 2026-10-20 together with its transport counterpart, and
is superseded by [transport-take2](../2026-10-20-transport-take2/frd.md).

## What reached `main`

- The FRD, HLA, SSH LLD, migration strategy, comparison with `main`, plan and proof results (PR
  #796, merged 2026-09-19).
- The SSH carrier proof under `cli/agentworks/execution/carriers/ssh/` (PR #796): about 0.5k
  production and 1.1k test lines. No production command reaches it. The successor either adopts or
  deletes it. Its test README links to this directory's `poc-results.md`, so wiping this directory
  waits until that link is gone.

No production behavior changed.

## What did not

The full carrier implementation (PR #832, head `060e1827a`) closed unmerged on 2026-10-22. It was
stacked on the unmerged transport stack (PR #833); its own delta over that base was small, mostly
POSIX terminal wiring, and most of it was SDD records. The plan's 11 open items are abandoned.

## What carries forward

The successor keeps this effort's connection policy and most of its trust requirements in condensed
form: ignore operator SSH configuration, offer only the configured identity, require the approved
OpenSSH minimum, and carry existing trust over by explicit import without modifying the source
known-hosts file, bound to the VM rather than its address, with first-use acceptance only for a VM
being created. It does not keep preserving CA policy and revocations, including separate KRL files;
the import reports CA and revocation markers in known-hosts files instead. The rule against calling
legacy execution code, directly or indirectly, carries forward for the execution layer only; copying
remains allowed.
