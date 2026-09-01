# FRD: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Scope: reusable Debian release lifecycle, Trixie creation, and explicit adoption of operator-led
  upgrades

## Rulings this SDD rests on

- **Debian remains the one supported guest operating system.** Agentworks does not add an operating
  system selector, custom image field, or best-effort bring-your-own distribution mode.
- **Agentworks has exactly one current Debian release.** New VMs always target the core-selected
  current release. This effort advances current from Bookworm to Trixie.
- **A future promotion is data, not a new framework.** The ordered release registry gains the newly
  recognized release and its platform and APT mappings. Release-specific implementation work may
  still be necessary, but no new lifecycle model should be required.
- **Existing VMs remain operable.** Previous-release VMs continue ordinary supported operations. A
  recognized VM older than previous remains available on a best-effort basis with a warning on
  access. Agentworks does not brick it or attempt a multi-release upgrade.
- **Operators own distribution upgrades and recovery.** Agentworks does not execute Debian package
  upgrades, manage provider checkpoints, or promise portable rollback. Operators use Debian and
  provider-native procedures.
- **Agentworks explicitly adopts an externally changed release.** `agw vm confirm-release NAME`
  observes the live guest, displays the recorded and observed releases, and requires confirmation
  before changing the database. A change marks initialization pending and leaves `vm reinit` as a
  separate explicit operation.
- **The guest release is durable observed state.** Agentworks records the last release it verified.
  It does not hide that cross-platform fact in platform metadata or infer it from the product
  default.
- **Release-varying values are mappings.** Platform image selectors, third-party APT stanzas, and
  other release-specific facts are keyed by Debian release.
- **Core selects; platforms translate.** The VM create request carries the concrete current Debian
  release. Each platform maps it to an artifact and never infers a local meaning of "current".
- **Operator-owned image catalogs still implement the core decision.** Proxmox maps the requested
  release to an operator-configured template VMID. The platform cannot choose a different release.
- **Doctor remains local and bounded.** This effort adds no doctor checks that connect to VMs,
  observe live releases, or inspect upgrade residue.

The assisted-upgrade and managed-checkpoint design previously implemented in this effort was
superseded by operator ruling on 2026-09-01. Its implementation and completed plan record remain
visible in history, but neither surface ships.

## Why now

Debian 13 Trixie became stable on 2025-08-09. Agentworks still creates Bookworm VMs and hardcodes
Bookworm in platform implementations and shipped APT sources. Continuing that behavior creates new
legacy machines and makes the next Debian promotion another search-and-replace exercise.

The cutover is broader than replacing image strings. Existing rows do not record their guest
release, Proxmox clones an operator-owned template without checking it, and the initializer cannot
select a release-specific APT value. Trixie also makes `/tmp` memory-backed by default, while some
Agentworks flows stage size-unbounded archives there.

## Requirements

### R1: One ordered Debian release model

Agentworks has one typed ordered release-profile registry. Its initial values are `bookworm` and
`trixie`; the final entry is current. A profile contains the codename and `VERSION_ID` needed to
validate `/etc/os-release`. It does not contain an upgrade procedure or adjacent transition policy.

The registry derives three support tiers:

- `current`: the final profile;
- `previous`: the profile immediately before current; and
- `legacy`: every recognized older profile.

Appending a future profile changes those relative classifications without a database migration or a
separate current constant. Unknown releases fail clearly rather than being guessed.

### R2: Every new VM is verified as current Debian

Core selects `CURRENT_DEBIAN_RELEASE` and passes the concrete value in every `ProvisionRequest`.
Every bundled vm-platform accepts that value, maps it to a platform artifact, and returns a native
transport that lets core verify the live guest before provisioning is accepted.

The platform does not receive or infer a generic "current" token. Missing mappings fail before
backend mutation with an error that distinguishes code-owned mappings from operator-owned platform
configuration. There is no fallback to Bookworm, latest, or an arbitrary operator image.

Core independently probes `/etc/os-release` after the platform returns. The guest must identify
Debian with the expected codename and version pair. A mismatch leaves enough persisted platform
identity for explicit cleanup and never records the requested release as observed.

The provisioning output names the selected release in the create line, announces release
confirmation when it actually occurs, and ends the section with `Provisioning complete.`

Platform responsibilities are documented in:

- `cli/agentworks/capabilities/README.md`;
- `cli/agentworks/capabilities/vm_platform/README.md`; and
- `cli/agentworks/plugins/README.md`.

The internal vm-platform contract remains version 1 because all bundled implementations move
together and no external plugin compatibility promise exists.

### R3: Persist the last verified live release

The VM row stores nullable `debian_release` and `debian_release_observed_at` fields as one fact.
Existing rows remain null until a release-sensitive operation proves the live guest. No platform,
site name, image string, or current default is used as a backfill.

Creation records the verified requested release. Ordinary release-sensitive operations may fill an
unknown row or refresh a matching observation. They refuse a recognized live mismatch and direct the
operator to `vm confirm-release`.

`vm list`, `vm describe`, and their JSON projections expose the recorded observation. The compact
list has one `DEBIAN` column; it does not duplicate the relative support tier there.

### R4: Explicitly confirm an operator-led release change

`agw vm confirm-release NAME [-y|--yes]` activates the named VM through the ordinary boundary and
probes the canonical live guest. It accepts only a release recognized by this Agentworks build.

The command displays recorded and live releases. If they differ, including an unknown recorded
value, it asks a default-negative confirmation unless `--yes` is present. Declining changes nothing.

After consent, changing the observation and setting the existing initialization status to `pending`
happen in one database transaction. The command then directs the operator to `agw vm reinit NAME`.
It does not run reinitialization itself. A matching observation refreshes the observation time
without changing initialization status or prompting.

This separation is intentional:

- release confirmation records an externally observed fact;
- reinitialization converges Agentworks-managed state for that fact; and
- a failed reinitialization retains the truthful release while the existing initialization status
  records that convergence is incomplete.

The command permits a recognized observation to move forward or backward. This supports both an
operator-led Debian upgrade and an operator-led provider restore.

### R5: Release-specific values are explicit maps

Every value whose correctness depends on Debian release is represented as a release-keyed map. This
includes platform image selectors, Proxmox template VMIDs, and shipped or operator-authored APT
sources.

An `apt-source` may use either:

- one scalar `source` that is genuinely release-neutral; or
- a `sources` map keyed by recognized Debian releases.

Codename-bearing scalars are rejected with guidance to use the map. The initializer selects using
the VM's verified observed release and fails before writing keys or source files when the selected
mapping is missing. Mapped values are policy, not codename substitution; a vendor may intentionally
use a differently named suite.

### R6: Existing release support is relative and non-destructive

Current and previous VMs continue their ordinary lifecycle behavior. Legacy VMs emit one warning
when an operation accesses them, then proceed through the same concrete checks in best-effort mode.
Release age alone does not make start, stop, shell, backup, copy, or delete unavailable.

Agentworks does not execute an upgrade for any tier. Documentation points operators to Debian's
release notes, provider-native backup and recovery facilities, `vm confirm-release`, and
`vm reinit`. A legacy VM's recommended path is a current VM plus data copy rather than a
multi-release jump.

### R7: Trixie operational assumptions are corrected

Size-unbounded VM backup and workspace-copy staging moves from `/tmp` to a secure disk-backed
location. Temporary paths remain private, quote-safe, and cleaned after success or failure.

Release-aware initialization preserves the existing Agentworks contract on Trixie, including SSH,
APT, Tailscale, identities, workspaces, and system settings. This effort does not turn
reinitialization into package removal or a distribution upgrade.

### R8: Doctor does not probe live VMs

Doctor does not connect to VMs for Debian release observation, upgrade diagnostics, or residue
inspection. It therefore does not start VMs, wait on unavailable WSL distributions, or add one
network timeout per managed VM.

Operators use the explicit named-VM commands when they want live evidence:

- `vm confirm-release` for release observation and adoption;
- `vm verify-connection` for canonical connectivity; and
- `vm reinit` for convergence.

### R9: Retire the unshipped checkpoint schema forward

Migration 33 adds the release observation fields. Migration 34 created the managed-checkpoint table
and has already run on development installations, so it remains immutable history.

Migration 35 removes that table. It first proves the table contains no checkpoint ownership rows. If
rows remain, migration fails without dropping them and directs the operator to use the previous
build to delete the provider artifacts before retrying. A fresh database therefore passes through 34
and 35 to the clean final schema; an existing development database never loses provider ownership
metadata silently.

## Acceptance

- New VM creation on every bundled platform receives the concrete Trixie release and verifies a
  matching live Debian 13 guest.
- Missing platform mappings and Proxmox template mappings fail clearly without fallback.
- Existing VM rows migrate with a null release observation and remain usable.
- Creation and reinitialization resolve APT sources from the VM's verified release.
- `vm confirm-release` refuses unrecognized guests, requires consent for change, updates release and
  pending initialization atomically, and does not call reinitialization.
- A matching confirmation refreshes observation time without changing initialization status.
- `vm list` and `vm describe` expose recorded Debian release without live probes.
- Doctor performs no Debian-related VM network access.
- Legacy recognized VMs warn and continue best effort; previous VMs are not treated as legacy.
- Migration 35 removes an empty checkpoint table and refuses a nonempty one.
- No `vm upgrade`, checkpoint CLI, checkpoint capability method, provider checkpoint implementation,
  remote upgrade journal, or experimental upgrade gate remains.
- Large backup and workspace-copy staging no longer depends on Trixie's memory-backed `/tmp`.
- Permanent docs and capability contracts describe the operator-led upgrade boundary.

## Constraints and non-goals

- No non-Debian guest support.
- No operator choice of Debian release for new VM creation.
- No arbitrary provider image override.
- No Agentworks-managed distribution upgrade.
- No Agentworks-managed checkpoint or snapshot abstraction.
- No automatic `vm reinit` from `vm confirm-release`.
- No automatic release adoption during ordinary operations.
- No direct or chained multi-release upgrade.
- No doctor live-VM release scan.
- No package-level downgrade or rollback promise.

## Settled decisions

- Current is derived from the final release profile.
- Bookworm remains recognized as previous when Trixie becomes current.
- Core passes the concrete release through the version-1 vm-platform create request.
- Core verifies the live release independently after platform provisioning.
- Persisted release is observed state, not desired configuration.
- A changed confirmed release reuses `InitStatus.PENDING` rather than introducing another VM state.
- `confirm-release` and `reinit` remain separate commands.
- Platform-native upgrade backup and recovery remain operator responsibilities.
- Migration 34 is not rewritten; migration 35 retires it safely.

## Open questions

None.
