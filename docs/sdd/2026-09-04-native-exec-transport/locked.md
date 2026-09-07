# Native execution transport: locked

**Locked:** 2026-09-06

This effort is complete in PR #746. The lock takes effect when that PR lands on `main`; until then,
this file records the final reviewed and live-validated implementation state. The implementation is
post-0.18.0 and closes issue #727.

## What shipped

- `ExecTransport` names the non-interactive command surface required from every version-1 VM
  platform. Full `Transport` extends it with file transfer and interaction. Core provisioning,
  Debian attestation, Tailscale repair, rekey, and logout use only the narrow contract; the explicit
  `vm shell --platform` path is the sole core consumer allowed to require the full subtype.
- Every VM platform supplies one required native execution transport. Existing AWS, Azure, GCP,
  Lima, and WSL carriers retain their behavior. Proxmox supplies a QEMU Guest Agent carrier for
  Tailscale-independent execution and declares interactive platform shell unavailable with serial
  console guidance.
- The Proxmox carrier dispatches a command once, polls its PID inside one remaining-time budget,
  validates the provider response at the API boundary, rejects incomplete or truncated output, maps
  only valid exits and signals, and never treats timeout as cancellation or redispatch authority.
  Sensitive stdin is bounded to the provider's 65,536-character field limit and excluded from
  arguments, logs, results, diagnostics, and exception graphs.
- Proxmox provisioning returns the QGA carrier for bootstrap and release attestation. Cloud-init
  exit 0 succeeds, exit 2 continues with a recoverable-warning message, completed hard failures fail
  immediately, and a genuine timeout retains the last safe provider diagnostic without exceeding its
  outer deadline.
- The setup script builds the current Debian Trixie template, attaches the exact volume Proxmox
  records after import, supports directory and block-backed storage, and stops with repository
  guidance without rewriting host package sources. A later failure removes only the partial VM
  created by that invocation and never scans for pre-existing orphan volumes.
- Proxmox VE 8 keeps `VM.Monitor`; VE 9 uses only `VM.GuestAgent.Unrestricted`, which covers the
  Agentworks network, file-write, exec, and status calls. Unknown majors are refused before setup
  mutation. Clone permission is scoped to the source template rather than the target VM pool.

## Verification and review

The final production checkpoint is `87b1c1e2e24dde1afb9a1e47517476a9b88b88a8`, containing current
`main` at `0c8cf6bc77dd49a2a30440cde0326f37a3980689`. Verification recorded:

- 151 focused Proxmox tests and 8,549 non-integration Python tests with two expected skips;
- Ruff check and format plus strict mypy over the complete CLI source and test tree;
- repository file lint, locked-SDD, Rulesync drift, shell syntax, diff, and residual-contract scans;
- 160 Python and 103 Node website tests plus deterministic root and project-path site builds;
- byte-identical wheel and source-distribution rebuilds followed by a fresh installed-wheel CLI and
  Proxmox import smoke; and
- hosted Linux Python 3.12, 3.13, and 3.14, Windows Python 3.13, repository, website, CodeQL, and
  aggregate CI success on the exact checkpoint.

Private agentworks-reviewer, Muntz, and cold correctness/security passes converged cleanly on the
production checkpoint. Two authorized published runtime feedback/fix rounds completed; the third was
not needed.

Live validation used a fresh Proxmox VE 9.2.11 host and directory-backed storage. The unmodified
setup script rebuilt the user, roles, and pool and validated the existing Trixie template. After the
tester corrected a bed-local missing bridge, the issued token created a VM successfully on the next
attempt. The live guest exercised degraded cloud-init completion, full QGA bootstrap, create-time
release attestation, ordinary execution, typed platform-shell refusal, Tailscale-independent rekey
with Tailscale down, logout during forced deletion, and final absence of guest residue. The bridge
fault was explicitly classified as test infrastructure rather than product behavior.

## Permanent homes and accepted limits

The capability contract lives in `cli/agentworks/capabilities/README.md`,
`cli/agentworks/capabilities/vm_platform/README.md`, and the transport and VM-platform base types.
The operator contract lives in `cli/command-reference.md` and `docs/guides/proxmox.md`. Provider
modules, setup automation, and behavioral tests carry the executable rules. Nothing under this SDD
directory is required to operate or maintain the feature.

The live bed covers Proxmox VE 9, not VE 8. VE 8 retains its established `VM.Monitor` permission and
numeric QGA boolean normalization under automated coverage. Completed exit and signal fields remain
strictly typed and fail closed if a provider returns an unknown wire shape; no live VE 8 behavior is
inferred. Existing full native carriers changed only at their type boundary and retain focused and
full regression coverage; the new carrier and correctness crux received the live pass.

Canonical no-fallback, timeout, secret containment, and setup rollback are covered by exact-head
behavioral or failure-injection tests plus independent review, not by deliberate live fault
injection. The live successful setup, VM lifecycle, and final residue check complement that bounded
evidence. QGA still does not provide an interactive terminal, and a timed-out command may continue
in the guest because Proxmox exposes no matching cancellation operation. These limits are explicit
and fail safe.

The operator owns merging PR #746. The effort lead does not merge it.

-- agw-trixie (cloud VM permission-check implementation lead)
