# FRD: Installer Plugins (Core Slimming, Pre-0.14)

- Status: Seed, ready for an effort lead
- Date: 2026-08-07
- Seeded by: the roadmap lead. This is a child of the 2026-08-04-next-steps roadmap (operator
  ruling, 2026-08-07: in roadmap scope, gates the 0.14.0 cut). It is launchable whenever: the R1
  inventory has no dependencies, while the plugin moves consume wave 2's descriptor registration and
  the guide topics consume onboarding's first slice, both of which precede the 0.14.0 cut this child
  gates. The effort lead owns the HLA and plan; the roadmap lead reviews PRs. Per the sdd skill,
  this FRD becomes the effort lead's on merge of this seeding PR.

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
- R2. Plugin-bound steps move behind system plugins named for their **mechanism** (`apt`, `snap`,
  `mise` are the observed candidates), using the existing internal plugin framework (registration
  conformance, atomic seating); the descriptor work from wave 2 is the registration substrate.
  Grouping follows the shape test, never a curated theme: mechanisms are distinct when their
  external dependency, config family, and failure modes differ, and the inventory may fold
  mechanisms whose shapes genuinely coincide, recording the shape test's answer per grouping
  (operator direction, 2026-08-07).
- R3. **The disabled experience is a first-class requirement.** An existing config that references a
  moved surface while the owning plugin is not enabled MUST fail with a crisp error that names the
  moved surface, the plugin that now owns it, and the exact remediation (the config line to add).
  This follows the remediation-posture ruling in the roadmap's `target-state.md`: precise errors
  plus guide content, no automated migrator.
- R4. Behavior parity when enabled: with the plugin enabled, initialization behaves as today,
  idempotent reinit included, per `docs/guides/idempotency.md`.
- R5. Guide and docs ride the change: guide topic contributions for the new plugins through the
  universal contribution contract (the onboarding FRD's R14; the always-on guide-contributions rule
  arrives with onboarding phase 1), sample-config and completions updated, and the 0.14 upgrade
  guide gains the enable-the-plugin step.
- R6. Ships in 0.14.0: this is part of the breaking-cleanup release and rides the same runway
  posture (the release that rejects old inputs also ships the teaching that explains them).
- R7. **An explicit disable list**, universal across plugin-provided and built-in resources
  (operator direction, 2026-08-07). A disabled resource leaves the normal views (lists, completions,
  guide topics; retrievable behind a flag); `describe` and `doctor` always surface enablement
  provenance; and a disabled resource that is still referenced is a finalize-time hard error naming
  both ends and the remediation (re-enable it, or declare your own under the name). Wave 2's
  reference extraction is what makes this safe: dangling references to disabled resources are
  detectable at finalize by construction. This may be the firing trigger for the descriptor
  contract's deferred `consumer_gating` field; the effort records that determination in the
  descriptor contract if gating derivation consolidates here.

## Settled constraints (inherited; do not reopen)

- C1. Remediation is precise errors plus the guide (operator ruling, 2026-08-07, `target-state.md`
  compatibility posture). No migration tooling; the disabled error and the upgrade guide are the
  whole path.
- C2. The `development-principles` bad-complexity test applies to the plugin split: model the real
  grouping, no speculative generality, no mechanism without a consumer.
- C3. This exercises the internal plugin framework only; it makes no external plugin promises (wave
  8 still gates those on the distribution-trust model).
- C4. **The name is the contract** (operator agreement, 2026-08-07). A resource name's provider
  changes only by explicit operator act: a silent same-name collision between a plugin-provided
  resource and an operator declaration is a hard error whose remediation names both paths (rename
  the operator's resource, or disable the plugin's and keep the name). Disable-and-redeclare is the
  sanctioned replacement flow, and the substitution is surfaced in provenance, so dependents of the
  name never silently receive something other than what they were built for. Defaults-with-override
  semantics (a synthesized row that an operator declaration replaces, with provenance shown) are
  reserved for surfaces that declare them deliberately; wave 3's synthesized secret sources are the
  canonical case (`docs/sdd/2026-08-07-secret-sources/`).

## Growth path (recorded, explicitly out of scope now)

- **In-plugin bundles**: maintainer-curated selectable subsets of a plugin's contributions ("the
  harness without the installer"), with an easy "all". Deferred with a named trigger: when disabling
  a plugin's pieces feels like maintaining a blocklist, that plugin needs bundles. Pre-committed
  shape so a later addition cannot fork semantics: a bundle is a preset that resolves to the same
  enable/disable state R7 defines, one underlying mechanism with two surfaces, and the maintainer's
  valid-combo guarantee is exactly what a preset is.

## Acceptance

- AC1. A 0.13-shaped config using a moved surface, run against 0.14 with the plugin disabled,
  produces the R3 error naming surface, plugin, and the exact config remediation.
- AC2. The same config with the plugin enabled initializes a VM to the same converged state as 0.13,
  and reinit is idempotent.
- AC3. Core's initializer directory contains only the steps the R1 inventory classified as
  core-essential.
- AC4. `agw guide` teaches the new plugins through their own topic contributions.
- AC5. Disabling a plugin-provided resource and declaring an operator resource under the same name
  yields the operator's resource, with the substitution shown by `describe`; the same config without
  the disable is a hard error naming both remediations.
- AC6. A disabled resource still referenced by another resource fails finalize with an error naming
  the reference and the remediation; the same disabled resource unreferenced simply leaves the
  normal views.

## Open questions for the effort lead

- The R1 inventory and classification (the seed deliberately does not pre-judge it beyond the named
  candidates).
- One plugin or several, and their names.
- Whether the disabled error surfaces at config load, finalize, or first use, and how it interacts
  with `agw doctor`.
