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
parallel creation releases, or a distribution-upgrade product. Debian upgrades and provider recovery
are release-, workload-, and backend-specific. A provider disk restore also cannot atomically
restore Agentworks' relational agents, sessions, consoles, workspaces, and declarations.

## Decision

Agentworks continues to support Debian as its one managed guest operating system. It has one ordered
registry of recognized Debian release profiles, and the final profile is the sole current release
for new VM creation. Core passes that concrete release through the internal version-1 vm-platform
capability. Each platform maps it to its own reviewed artifact. Core probes the returned transport
and persists only its own live observation.

The database stores the last release observed from each VM. Existing unknown rows are observed
rather than guessed. Current and previous releases remain ordinarily operable. Older recognized
releases continue best effort with a warning instead of being bricked.

Release-dependent values are explicit maps keyed by Debian release and stay with the component that
understands them. Supporting a future Debian stable release means adding and certifying its profile,
platform artifacts, and APT values, then appending that profile as current. It may still require
release-specific work, but it does not require a new lifecycle model.

Agentworks does not execute a Debian distribution upgrade and does not manage provider checkpoints.
Operators follow Debian's release notes and use provider-native backup and recovery. After an
operator changes or restores a guest, `agw vm confirm-release NAME` probes and displays the recorded
and live releases. A confirmed change atomically records the live observation and sets the existing
initialization status to pending. The operator then runs the separate `agw vm reinit NAME` command.

Keeping confirmation and reinitialization separate preserves truthful state when reinitialization
fails. A new `reinit required` VM state is unnecessary because pending initialization already
represents incomplete convergence.

Doctor does not discover live Debian state. Live observation is an explicit named-VM operation, not
a fleet-wide network scan that may activate VMs or wait on unavailable platforms.

Operators cannot select another Debian creation release or bring another operating system into the
managed lifecycle. A provider-specific artifact identifier may remain operator configuration where
that provider already requires it, but core still chooses the release key it must satisfy. Live
release verification has no operator bypass.

## Consequences

- New VMs across every platform target the same core-selected Debian stable release.
- Platforms fail clearly when their implementation or operator-owned release map lacks that release.
- Agentworks can reinitialize existing VMs using their observed release instead of current creation
  policy.
- The supported operator upgrade path stays relative: previous to current. Legacy VMs remain usable,
  but a new current VM plus data copy is the recommended transition.
- Upgrade execution, package recovery, and provider snapshots remain outside Agentworks' product
  contract.
- A failed reinitialization cannot make the release observation false; pending initialization names
  the remaining convergence work.
- A future stable promotion reuses the registry, persistence, platform contract, explicit adoption,
  and release-aware initialization while still requiring release-specific certification.
- Multi-distribution support, operator-selected releases, automatic downgrades, and multi-hop
  upgrades remain outside the product contract.
