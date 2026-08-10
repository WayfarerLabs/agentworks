# Bootstrap fallback removal: functional requirements

## Context

VM creation currently uses `ProvisionResult.bootstrap_complete` to decide whether Phase A should
trust a platform-completed bootstrap or generate and stage a full bootstrap script. The flag
conflates two different states:

- WSL2 intentionally leaves its primary bootstrap for Phase A.
- Azure and AWS can attempt their provider-owned bootstrap, fail readiness before delivering the
  Tailscale key, and return the same incomplete result.

The second state routes a resolved key through a generated local and guest staging file after the
provider had already selected the fixed stdin delivery contract. Issue #471 records the reproduced
Azure and AWS failure paths.

## Goal

Make bootstrap ownership unambiguous. A platform create operation either completes bootstrap and
returns a usable provisioning transport, or fails and rolls back. No generic post-create fallback
may generate a second key-bearing bootstrap script.

## Requirements

### R1: complete-or-raise platform creation

Every `VMPlatform.create()` receives the resolved Tailscale key and owns create-time bootstrap.
Returning from `create()` means the bootstrap succeeded. A failure before or during bootstrap raises
and follows the platform's existing create rollback contract.

The key is required in `ProvisionRequest`; the optional absent-key seam and every provider branch
that defers on `None` are removed. This value-bearing operation input is intentional: the key is
declared by the effective VM template and resolved by the VM domain before platform dispatch. It is
not a secret declared by the platform's own config model, so the capability-config rule that
delivers platform secrets through `ctx.secret(name)` does not apply to it. The permanent VM-platform
contract continues to name this sanctioned seam explicitly.

### R2: no incomplete result state

`ProvisionResult` does not expose a boolean or other state that lets the manager interpret a failed
or deferred bootstrap as permission to retry through a different delivery mechanism.

The shared fixed-stdin bootstrap helper also has no completion boolean. It returns the discovered
`str | None` Tailscale IP after a successful join or raises; the fallback-era `BootstrapCompletion`
record is removed.

The result may omit a Tailscale IP after a successful join. That means only that IP discovery must
be retried; it never permits credential redelivery.

This is a breaking implementation-contract change. The vm-platform descriptor and every in-tree
implementation move together from contract version 1 to version 2. Registration rejects a version 1
implementation before it can run, with the existing exact-version error. No v1 compatibility shim or
dual result interpretation remains.

### R3: Phase A never bootstraps again

After `create()` returns, Phase A may:

- rediscover the Tailscale IP without the key;
- record provisioning state;
- verify Tailscale SSH;
- close platform provisioning access through the existing hooks.

It must not generate, stage, or execute a full bootstrap script.

### R4: intentional WSL2 bootstrap stays supported

WSL2 continues to create and initialize a usable distro. Its current post-create bootstrap is an
intentional primary mechanism, not failure recovery, so it moves under WSL2's create ownership and
must finish before `WSL2Platform.create()` returns.

This change does not broaden the WSL2 secret-retention contract. Its existing private local and
guest staging, verified cleanup attempts, redaction, and failure behavior remain the required
boundary.

### R5: existing provider delivery contracts remain intact

- Lima, Azure, and AWS retain credential-free provider payloads and deliver the key through their
  fixed stdin join.
- Proxmox retains its private guest-agent staging contract.
- Azure and AWS readiness failure never reaches a key-bearing generated-script fallback.
- A future platform, including GCP, is born into the complete-or-raise contract and cannot select a
  generic fallback.

### R6: fail closed without new secret machinery

Readiness or bootstrap failure reports a typed, secret-free error and triggers the platform's
existing rollback. No memory-erasure, snapshot, generalized secret-lifecycle, or alternate retry
framework is introduced.

### R7: observability and operator behavior remain honest

Successful creates retain the current provisioning progress, Tailscale IP recording, SSH
verification, and initialization behavior. Failed creates retain the existing rollback and manual
cleanup guidance for any resource that cannot be removed.

The create rollback window still closes when `create()` returns. A later Tailscale SSH verification
failure remains the current kept VM behavior: Phase A marks the row `FAILED`, invokes
`secure_failed_vm` best-effort, preserves recovery guidance, and does not delete a successfully
created backend VM.

### R8: interrupts preserve the create contract

An operator interrupt during readiness or bootstrap remains inside the platform create rollback
window. Each platform performs its existing best-effort backend cleanup, preserves the original
`KeyboardInterrupt`, supports the documented second-interrupt abandonment behavior, and prints exact
manual-removal guidance when cleanup cannot complete. Moving WSL2 bootstrap ownership must not move
its interrupt outside that window.

## Acceptance criteria

- Azure and AWS SSH exhaustion and cloud-init-wait failure raise from `create()`, roll back, and
  never invoke key-bearing local or guest staging.
- The same failures keep provider-retained payloads, argv, logs, diagnostics, and exception objects
  free of the sentinel key.
- Lima, Azure, and AWS fixed-stdin success and join failure keep the sentinel out of command text,
  returned output, logs, diagnostics, and exception chains; Azure and AWS join failure rolls back.
- WSL2 completes its primary bootstrap before returning from `create()` and Phase A does not run a
  second bootstrap. WSL2 success, bootstrap failure, cleanup failure, and interrupt tests keep the
  sentinel out of command text, returned output, logs, diagnostics, and exception chains, and verify
  its accepted private staging cleanup attempts.
- Proxmox bootstrap failure raises and rolls back rather than returning an incomplete result.
  Proxmox success, bootstrap failure, cleanup failure, and interrupts retain its provider-shaped
  sentinel and private staging cleanup coverage.
- Successful Lima, WSL2, Proxmox, Azure, and AWS create flows still reach initialization and
  Tailscale SSH verification.
- A post-create Tailscale SSH verification failure keeps a secured `FAILED` VM with recovery
  guidance; it does not reopen create rollback.
- Interrupt coverage spans Azure and AWS readiness waits, WSL2's moved bootstrap, and Proxmox
  bootstrap, including second-interrupt/manual-removal behavior.
- The vm-platform descriptor and every in-tree implementation declare contract version 2, and a v1
  implementation is rejected at registration.
- `ProvisionResult` and Phase A contain no generic incomplete-bootstrap fallback surface.
- Focused tests, the full non-integration suite, formatting, lint, and strict type checks pass.

## Out of scope

- Changing how providers retain credential-free bootstrap payloads.
- Replacing WSL2 or Proxmox's already accepted private staging mechanics.
- Adding bootstrap retries beyond the bounded readiness behavior platforms already own.
- Changing VM initialization or `agw vm reinit` behavior after create-time provisioning.
