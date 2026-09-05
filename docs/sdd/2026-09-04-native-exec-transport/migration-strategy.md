# Native Execution Transport: Migration Strategy

- Status: Design
- Date: 2026-09-04
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [exec-transport-lld.md](./exec-transport-lld.md)

## Migration objective

Replace one broad optional native transport with one narrow required native transport in a single
post-0.18 runtime change. All bundled platforms and core callers move together while the capability
stays at version 1.

There is no database migration, data rewrite, configuration compatibility period, command alias, or
schema-version change.

## Baseline inventory

Snapshot at `c962f52043e9ea239197ad96d5a383f98db9164d` on 2026-09-04:

| Surface                            | Baseline state                                        |
| ---------------------------------- | ----------------------------------------------------- |
| `Transport`                        | execution plus interactive, streaming, and copy       |
| `ProvisionResult.native_transport` | required full `Transport`                             |
| `VMPlatform.native_transport`      | optional `Transport`, default `None`                  |
| native factory                     | full `Transport`, rejects `None`, probes reachability |
| full platform implementations      | Lima, remote Lima, WSL2, Azure, EC2, GCE              |
| Proxmox existing VM                | returns no native transport                           |
| Proxmox create result              | Tailscale `SSHTransport`                              |
| core native consumers              | use `run`, except `vm shell --platform`               |
| vm-platform capability             | version 1                                             |
| Proxmox declared support           | VE 8                                                  |

The implementation refreshes this inventory against its post-0.18 baseline before mutation.

## Atomic transition

### 1. Extract the narrow base

Add `ExecTransport` and move only `describe`, execution attributes, timeout resolution, and the four
native run options to it. Make `Transport` extend it and retain the wider existing run surface. Keep
established concrete full implementations behaviorally unchanged.

### 2. Build the Proxmox carrier

Split QGA dispatch and status polling at the API seam and add the execution-only adapter with
focused tests. Preserve the specialized bootstrap staging flow. The adapter exists before the
platform API requires it.

### 3. Cut over the contract atomically

Change `ProvisionResult.native_transport`, the platform hook, the native factory, Phase A
provisioning, Debian release, and Tailscale helpers to `ExecTransport`. Make the hook required,
implement it on Proxmox, return the adapter from Proxmox create, and update version-1 conformance
fixtures together. Delete the `None` path. Add one early shell-availability declaration and one full
type check to `vm shell --platform`; keep canonical shell unchanged.

### 4. Update permanent collateral and proofs

Update root and vm-platform capability obligations, the Proxmox guide, platform descriptions,
fixtures, and tests in the same implementation. Run a residual scan for the optional Proxmox
exception and old claim that native transport always supports interaction.

## Compatibility treatment

- **Capability version:** remains 1 because the API is internal and every implementation changes in
  one release.
- **Database:** no change. Proxmox already persists node and VMID; VM rows already carry the admin
  username.
- **Configuration:** no change. Proxmox already declares and resolves the API token. No danger flag
  or recovery selection is introduced.
- **CLI and machine output:** no grammar or schema change. Proxmox native shell remains unsupported,
  with more accurate reasoning.
- **Runtime:** implementation begins only after 0.18.0. It does not enter the 0.18 release branch or
  release candidate.
- **Provider support:** VE 8 remains the declared Proxmox target. VE 9 permission changes are not
  silently folded into this migration.

## Delivery ordering

The SDD lands as a documentation-only PR if its review converges. Runtime implementation requires a
separate authenticated operator direction after 0.18.0 and a fresh branch at the correct baseline.
The runtime PR keeps the type split, all bundled platforms, Proxmox implementation, tests, and
permanent documentation together. There is no useful partially compatible midpoint to release.

Within the runtime branch, tests may land alongside each step, but every pushed handoff must keep
the capability registry and all bundled implementations internally consistent.

## Rollback

The artifact PR has no runtime rollback need. The implementation can be reverted without restoring
state because it writes no new data and reads no new configuration. A post-release rollback removes
Proxmox recovery support and restores a known contract exception, so a forward fix is preferred.
