# HLA: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Scope: Trixie creation, release observation, and explicit adoption after operator-led upgrades

## Architectural summary

Agentworks has one ordered registry of recognized Debian releases. The last entry is the current
release used for all new VM creation. This effort registers Bookworm and Trixie, making Trixie
current. Support labels are derived from position, so appending a future release makes today's
current release previous without changing the lifecycle model.

Core passes the concrete current release to the version-1 vm-platform `create` operation. Each
platform resolves that value through a release-keyed artifact map. Core then independently probes
the returned native transport and records only the live release it verified.

Agentworks does not run a Debian distribution upgrade or manage provider snapshots. An operator uses
Debian's release notes and provider-native recovery facilities, then runs:

```text
operator upgrade or restore
          |
          v
vm confirm-release NAME
  probe recognized live Debian
  show recorded and live values
  confirm change
  transaction: observation = live, init_status = pending
          |
          v
vm reinit NAME
  converge Agentworks-managed declarations for the recorded release
```

The split is deliberate. Release confirmation records an external fact. Reinitialization is a
separate convergence operation that can fail and be retried while the database remains truthful. The
existing initialization state is sufficient; a new VM state is not introduced.

Doctor remains local and bounded. It reads recorded database state where appropriate, but this
effort adds no per-VM transport, activation, release probe, upgrade scan, or network wait.

## Core release model

`agentworks.debian` owns:

- `DebianRelease`, the recognized codename values;
- `DebianReleaseProfile`, which pairs a codename with its `VERSION_ID`;
- `DEBIAN_RELEASES`, the ordered profile tuple;
- `CURRENT_DEBIAN_RELEASE`, derived only from the final profile;
- `classify_release`, which derives current, previous, or legacy from position; and
- the `/etc/os-release` parser and transport probe.

Profiles contain release identity, not an upgrade script or pair-specific transition policy. A
future promotion appends a reviewed profile and supplies its platform and APT mappings. Any
release-specific compatibility work remains ordinary implementation work for that promotion.

The parser accepts only a consistent recognized triple:

```text
ID=debian
VERSION_ID=<profile version>
VERSION_CODENAME=<profile codename>
```

Non-Debian, unknown, incomplete, and contradictory observations fail. Neither the requested image
nor the recorded database value substitutes for a live observation.

## Create pipeline

The VM manager computes `creation_release = CURRENT_DEBIAN_RELEASE` before constructing the pending
VM graph. The required `ProvisionRequest.debian_release` carries that concrete enum value to every
bundled platform.

Each platform owns a release-keyed artifact map appropriate to its backend:

| Platform | Release-mapped value                                       |
| -------- | ---------------------------------------------------------- |
| AWS EC2  | Debian SSM release selector plus architecture              |
| Azure VM | Marketplace publisher, offer, SKU, version, and disk floor |
| GCP GCE  | image project and architecture-specific family             |
| Lima     | architecture-specific Debian cloud image                   |
| WSL2     | Debian OCI tag and derived cache identity                  |
| Proxmox  | operator-configured template VMID                          |

Code-owned maps are complete for the core current release and covered by conformance tests.
Operator-owned Proxmox configuration is checked by `validate_create_release` before secrets or
backend authentication are resolved, and again by `create` before mutation. A missing value names
the exact `template_vmids.<release>` key. No platform falls back to Bookworm, latest, a scalar
template, or an arbitrary image.

The platform returns a `ProvisionResult` containing the native transport and opaque platform
identity, but no platform-authored release assertion. Core probes that transport against the exact
pre-dispatch request. Only a successful core probe records `debian_release` and
`debian_release_observed_at` and proceeds to release-aware initialization. A post-create mismatch
leaves an addressable failed VM row for explicit cleanup.

Provisioning output names the release in the create line, emits
`Confirming Debian release <release>...` immediately before the core probe, and prints
`Provisioning complete.` only after the provisioning section succeeds.

The vm-platform contract remains version 1. It is an internal capability with no external plugin
compatibility promise, so the request addition and all bundled implementations change atomically.
The base capability README, vm-platform README, and plugin-author README teach the same contract.

## Persisted observation

Migration 33 adds nullable columns to `vms`:

```text
debian_release
debian_release_observed_at
```

A database check requires both to be null or both non-null. SQL deliberately does not enumerate
codenames, so a later recognized value does not require a schema-shape migration. Conversion to the
typed model validates non-null stored releases against the running build.

Existing rows remain unknown after migration. Agentworks does not infer a release from the site,
image selector, creation date, platform metadata, or new current default.

The ordinary `verified_vm_release` service has two safe cases:

- an unknown row is filled from a recognized live observation; or
- a matching row has its observation timestamp refreshed.

If recorded and live releases differ, ordinary release-sensitive work fails and directs the operator
to `vm confirm-release`. It never silently adopts an external distribution change.

`vm list` and `vm describe` expose recorded state only. The compact list has one `DEBIAN` column;
human detail also exposes relative support and observation time, while JSON exposes the recorded
release and observation time. These read paths do not perform a live Debian release probe; existing
status and resource inspection behavior is unchanged.

## Explicit release adoption

`agw vm confirm-release NAME [-y|--yes]` uses the ordinary named-VM boundary and canonical Tailscale
SSH transport. It activates the VM when that boundary normally requires activation, reads
`/etc/os-release`, and accepts only a release recognized by the running Agentworks build.

The command displays the recorded value, including `not recorded`, and the live value. When they
differ it asks a default-negative question unless `--yes` is supplied. Declining makes no database
change.

After consent, one database transaction:

1. records the live release and observation time; and
2. when the value changed, sets the existing `init_status` to `pending`.

The command does not call `reinit`. Its completion message names the separate `agw vm reinit NAME`
step. If reinitialization later fails, the observed release remains truthful and `pending` records
that Agentworks-managed declarations have not converged. Retrying reinit uses the existing
initialization lifecycle.

A matching observation only refreshes the timestamp. It does not prompt or change initialization
state. A recognized forward or backward change is allowed so the same explicit adoption works after
an operator upgrade or provider restore.

## Release-aware initialization

An `apt-source` resource has exactly one of:

- a scalar `source` for a release-neutral vendor suite; or
- a `sources` map keyed by recognized Debian release.

The initializer resolves a map from the VM's verified observed release. A missing selected key fails
before key or source mutation. Codename-bearing scalars are rejected so a future promotion cannot
reuse a stale host-specific suite by accident. Values are policy, not string substitution; for
example, a vendor may intentionally serve a Trixie host from a differently named suite.

`vm reinit` verifies or reconciles the live release before applying release-aware resources. It
remains configuration convergence only. It neither invokes `apt full-upgrade` nor removes packages
as part of a distribution transition.

## Existing VMs and support tiers

Current and previous VMs continue ordinary lifecycle operations. A recognized legacy VM emits one
warning on access, then proceeds through the same concrete checks on a best-effort basis. Release
age alone does not block start, stop, shell, backup, copy, reinit, or delete.

Unknown stored state is not treated as Bookworm. Release-sensitive operations probe and persist it
when safe. A live release newer than the running registry requires an Agentworks build that
recognizes it.

There is no Agentworks upgrade eligibility graph. Documentation supports the operator workflow only
from previous to current because that is the product support contract, and recommends a new current
VM plus data copy for legacy guests.

## Migration 34 retirement

Migration 34 created the managed-checkpoint table during development and has already run on at least
one operator installation. It remains immutable migration history.

Migration 35 inspects `vm_checkpoints` inside the normal migration transaction:

- if the table is absent, it succeeds;
- if the table is empty, it drops it; and
- if ownership rows remain, it raises before dropping anything and names the affected VMs.

The nonempty case directs the operator to reinstall the prior development build, delete each
provider artifact through the old managed command, and retry. This avoids silently losing the only
Agentworks record of a possibly billed provider artifact. The final schema sentinel records the
table as removed at version 35.

## Trixie operational corrections

Trixie commonly mounts `/tmp` as memory-backed storage. Size-unbounded VM backup and workspace-copy
archives therefore stage in secure disk-backed `/var/tmp` paths. The transport path remains
quote-safe and cleanup occurs after success or failure.

Other Trixie differences, including SSH keys, PAM environment behavior, sysctl locations, network
interface naming, and deb822 sources, remain certification concerns for creation and initialization.
They do not justify an upgrade orchestrator.

## Doctor boundary

Doctor does not probe live VM Debian releases or inspect remote upgrade state. It must not activate
a VM, wait for a stopped WSL distribution, or add a network timeout for every database row.

The operator chooses a named command when live information is wanted:

- `vm confirm-release` observes and adopts release state;
- `vm verify-connection` checks reachability; and
- `vm reinit` converges Agentworks-managed state.

Doctor may continue to report existing local database or capability readiness findings. No Debian
release transition logic changes its runtime shape.

## Failure and integrity behavior

- Missing create mappings fail before backend mutation and never fall back.
- Core create attestation persists no requested release until the live guest matches.
- Ordinary recorded/live drift fails with the explicit confirmation command.
- A declined confirmation performs no write.
- A changed confirmation updates observation and pending initialization atomically.
- Reinitialization failure does not roll the observation backward.
- Migration 35 never discards nonempty checkpoint ownership records.
- No Agentworks command claims portable rollback, snapshot atomicity, or distribution-upgrade
  recovery.

## Validation strategy

Unit and contract tests cover:

- registry ordering, support classification, and future profile append behavior;
- `/etc/os-release` parsing and mismatch handling;
- migration 33 observation shape and migration 35 empty/nonempty behavior;
- unknown, matching, changed, declined, forward, and backward confirmation cases;
- atomic pending initialization on changed confirmation;
- release-aware APT map validation and selection;
- concrete release dispatch and pre-mutation map failures on all platforms;
- core post-create attestation and addressable failure state;
- one `DEBIAN` list column and recorded-only read projections;
- the absence of upgrade, checkpoint, and live doctor surfaces; and
- secure disk-backed staging cleanup.

Live certification covers current-release create, core observation, initialization, ordinary VM
operations, reinit, backup, and delete on available platforms. It does not use production VMs to
certify an Agentworks-managed upgrade because no such product behavior ships.

Permanent docs teach the operator-led procedure and link to Debian's release-specific release notes
without duplicating their package commands. Capability docs describe only the version-1 create and
ordinary VM lifecycle contract.

## Deliberate boundaries

- no public OS, Debian release, or image selector for creation;
- no Agentworks-managed distribution upgrade;
- no checkpoint or snapshot object, contract method, or CLI;
- no automatic reinit after release confirmation;
- no second `reinit required` state beside existing pending initialization;
- no automatic release adoption during ordinary operations;
- no live-VM doctor scan;
- no general resource-locking framework in this effort; and
- no external vm-platform compatibility layer.
