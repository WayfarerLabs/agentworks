# FRD: Installer Plugins (Core Slimming, Pre-0.14)

- Status: Seed, ready for an effort lead
- Date: 2026-08-07
- Seeded by: the roadmap lead. This is a child of the 2026-08-04-next-steps roadmap (operator
  ruling, 2026-08-07: in roadmap scope, gates the 0.14.0 cut). It has no dependencies on other waves
  and can run whenever. The effort lead owns the HLA and plan; the roadmap lead reviews PRs. Per the
  sdd skill, this FRD becomes the effort lead's on merge of this seeding PR.

## Purpose

Core VM initialization currently carries miscellaneous built-in installers and setup steps that are
not fundamental to what a VM is. They belong behind one or more system plugins, so the core surface
shrinks, the plugin boundary gets first-party exercise before wave 8 promises anything externally,
and operators opt into what they actually use. Candidates observed in
`cli/agentworks/vms/initializer/` include the package installers (apt sources and packages, snap)
and the mise tooling step; the authoritative inventory of what moves versus what is genuinely core
(credentials, SSH keys, workspace directories look core) is the effort lead's first deliverable.

## Requirements

- R1. Inventory: enumerate every initializer and setup step in core, classify each as core-essential
  or plugin-bound, and record the rationale per item. The classification is reviewed before the
  moves begin (phased artifact review per the sdd skill).
- R2. Plugin-bound steps move behind one or more system plugins using the existing internal plugin
  framework (registration conformance, atomic seating). One plugin versus several is the effort
  lead's call, made on cohesion, not convenience; the descriptor work from wave 2 is the
  registration substrate.
- R3. **The disabled experience is a first-class requirement.** An existing config that references a
  moved surface while the owning plugin is not enabled fails with a crisp error that names the moved
  surface, the plugin that now owns it, and the exact remediation (the config line to add), making
  it super easy to know what to do. This follows the remediation-posture ruling in the roadmap's
  `target-state.md`: precise errors plus guide content, no automated migrator.
- R4. Behavior parity when enabled: with the plugin enabled, initialization behaves as today,
  idempotent reinit included, per `docs/guides/idempotency.md`.
- R5. Guide and docs ride the change: guide topic contributions for the new plugins per the
  `guide-contributions` rule, sample-config and completions updated, and the 0.14 upgrade guide
  gains the enable-the-plugin step.
- R6. Ships in 0.14.0: this is part of the breaking-cleanup release and rides the same runway
  posture (the release that rejects old inputs also ships the teaching that explains them).

## Settled constraints (inherited; do not reopen)

- C1. Remediation is precise errors plus the guide (operator ruling, 2026-08-07, `target-state.md`
  compatibility posture). No migration tooling; the disabled error and the upgrade guide are the
  whole path.
- C2. The `development-principles` bad-complexity test applies to the plugin split: model the real
  grouping, no speculative generality, no mechanism without a consumer.
- C3. This exercises the internal plugin framework only; it makes no external plugin promises (wave
  8 still gates those on the distribution-trust model).

## Acceptance

- AC1. A 0.13-shaped config using a moved surface, run against 0.14 with the plugin disabled,
  produces the R3 error naming surface, plugin, and the exact config remediation.
- AC2. The same config with the plugin enabled initializes a VM to the same converged state as 0.13,
  and reinit is idempotent.
- AC3. Core's initializer directory contains only the steps the R1 inventory classified as
  core-essential.
- AC4. `agw guide` teaches the new plugins through their own topic contributions.

## Open questions for the effort lead

- The R1 inventory and classification (the seed deliberately does not pre-judge it beyond the named
  candidates).
- One plugin or several, and their names.
- Whether the disabled error surfaces at config load, finalize, or first use, and how it interacts
  with `agw doctor`.
