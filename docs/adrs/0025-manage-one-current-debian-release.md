# 25. Manage one current Debian release

Date: 2026-08-30

## Status

Accepted

Supersedes [ADR 0002](0002-use-debian-as-the-vm-base-image.md).

## Context

ADR 0002 standardized every Agentworks VM on Debian 12 Bookworm. Standardizing on Debian remains the
right boundary, but fixing that decision to one codename made the next stable Debian release look
like a one-off platform migration. Release-dependent provider images and APT repositories also had
no shared type or observation record.

Agentworks needs to advance Debian stable without introducing operator-selected operating systems,
parallel creation releases, or an arbitrary release-upgrade graph.

## Decision

Agentworks continues to support Debian as its one managed guest operating system. It has one ordered
registry of recognized Debian release profiles, and the final profile is the sole current release
for new VM creation. Core passes that concrete release through the vm-platform capability; each
platform maps it to its own reviewed artifact and verifies the live guest while it can still roll
back a mismatch. Core independently probes the returned transport and persists only its own
observation. When the artifact catalog is operator-owned, the platform also verifies the live
release before Agentworks bootstrap mutates the guest.

The database stores the last release observed from each live VM. Existing unknown rows are observed
rather than guessed. A recognized release immediately before current is supported for an explicit
in-place upgrade to current. Older recognized releases remain available for ordinary operations on a
best-effort basis with a warning, but Agentworks does not start a direct or chained upgrade from
them.

Release-dependent values are explicit maps keyed by Debian release and stay with the component that
understands them. Supporting a future Debian stable release means adding and certifying its profile,
platform artifacts, APT values, and adjacent transition policy, then appending that profile as the
new current release.

The adjacent upgrade automatically acquires one managed offline checkpoint after local backups and
before its first package mutation. The vm-platform contract requires create, list, restore, and
delete operations for that checkpoint on every supported platform. Agentworks owns its generated
name and durable descriptor, limits each VM to one operational checkpoint slot, retains it after
upgrade or restore, and restores it only on an explicit operator command. Restore verifies that the
current effective declarations still match the captured fingerprint, independently attests the live
Debian release afterward, and returns the VM to stopped state for reinitialization.

Operators cannot select another Debian creation release or bring another operating system into the
managed lifecycle. A provider-specific artifact identifier may remain operator configuration where
that provider already requires it, but core still chooses the release key it must satisfy. Live
release verification has no operator bypass.

## Consequences

- New VMs across every platform target the same core-selected Debian stable release.
- Platforms fail clearly when their implementation or operator-owned release map lacks that release.
- Agentworks can reinitialize existing current and previous-release VMs using their observed release
  instead of current creation policy.
- The supported upgrade graph stays one edge: current-1 to current.
- Every supported platform provides the same managed recovery boundary before an adjacent upgrade;
  the one-slot rule avoids introducing a general snapshot-retention product.
- A future stable promotion reuses the registry, persistence, platform contract, and durable upgrade
  framework, while still requiring release-specific engineering and certification.
- Multi-distribution support, operator-selected releases, downgrades, and multi-hop upgrades remain
  outside the product contract.
