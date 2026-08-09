# FRD: Installer Resource Plugins (Pre-0.14)

- Status: Revised scope, independent artifact review clean, pending roadmap review
- Date: 2026-08-07
- Seeded by: the roadmap lead. This is a child of the 2026-08-04-next-steps roadmap (operator
  ruling, 2026-08-07: in roadmap scope, gates the 0.14.0 cut). It is launchable whenever: the R1
  inventory has no dependencies, while the plugin moves consume wave 2's descriptor registration and
  the guide topics consume onboarding's first slice, both of which precede the 0.14.0 cut this child
  gates. The effort lead owns the HLA and plan; the roadmap lead reviews PRs. Per the sdd skill,
  this FRD becomes the effort lead's on merge of this seeding PR.

## Purpose

Core currently ships a catalog of optional named apt and install-command resources as built-in
manifests. Those declarations belong behind mechanism-named system plugins so operators opt into the
catalogs they actually use, the internal plugin boundary gets first-party exercise before wave 8,
and a stable resource name never silently changes provider.

This effort moves existing declared resource rows, not initializer execution. The generic apt and
install-command executors remain core because they also run operator-declared resources. Snap, mise,
dotfiles, tmuxinator, and Claude setup remain core. This boundary is an operator ruling from
2026-08-08 that supersedes the seed's broader installer-candidate framing.

## Requirements

- R1. Inventory: enumerate every built-in apt and install-command resource row, assign each to its
  destination plugin, and record the rationale. Also record the execution and setup surfaces that
  remain core so the move cannot expand silently. The classification is reviewed before the moves
  begin (phased artifact review per the sdd skill).
- R2. The declared rows move into manifest-carrying system plugins named for their mechanism: `apt`
  and `install-command`. They use the existing internal plugin framework and shared bundled manifest
  loader. No new capability kind, initializer callback, execution seat, or raw config surface is
  introduced.
- R3. **The disabled experience is a first-class requirement.** An existing config that references a
  moved surface while the owning plugin is not enabled MUST fail with a crisp error that names the
  moved surface, the plugin that now owns it, and an exact, valid TOML replacement snippet that
  preserves every plugin already enabled. This follows the remediation-posture ruling in the
  roadmap's `target-state.md`: precise errors plus guide content, no automated migrator.
- R4. Behavior parity when enabled: with the owning plugin enabled, the unchanged core executor
  consumes the moved row exactly as it does today, idempotent reinit included, per
  `docs/guides/idempotency.md`.
- R5. Guide and docs ride the change: guide topic contributions for the new plugins through the
  universal contribution contract (the onboarding FRD's R14; the always-on guide-contributions rule
  arrives with onboarding phase 1), sample-config and completions updated, and the 0.14 upgrade
  guide gains the enable-the-plugin step.
- R6. Ships in 0.14.0: this is part of the breaking-cleanup release and rides the same runway
  posture (the release that rejects old inputs also ships the teaching that explains them).
- R7. **An explicit disable list**, universal across plugin-provided and built-in resources
  (operator ruling, 2026-08-07). A disabled resource leaves the normal views (lists, completions,
  guide topics), retrievable in listings behind a flag; `describe` and `doctor` always surface
  enablement provenance; and a disabled resource that is still referenced by another resource is a
  finalize-time hard error naming both ends and the remediation (re-enable it, or declare your own
  under the name). Wave 2's reference extraction is what makes this safe: references to disabled
  resources are detectable at finalize by construction. Scope: R7 governs resource-to-resource
  references; settings references keep the presence-not-availability contract wave 2 landed
  (`config/references.py`), with `doctor` surfacing the disabled state. R3's plugin-enablement error
  is a distinct, plugin-level mechanism whose surfacing point stays open below.

## Settled constraints (inherited; do not reopen)

- C1. Remediation is precise errors plus the guide (operator ruling, 2026-08-07, `target-state.md`
  compatibility posture). No migration tooling; the disabled error and the upgrade guide are the
  whole path.
- C2. The `development-principles` bad-complexity test applies to the plugin split: model the real
  grouping, no speculative generality, no mechanism without a consumer.
- C3. This exercises the internal plugin framework only; it makes no external plugin promises (wave
  8 still gates those on the distribution-trust model).
- C4. **The name is the contract** (operator ruling, 2026-08-07). A resource name's provider changes
  only by explicit operator act: a silent same-name collision between a plugin-provided resource and
  an operator declaration is a hard error whose remediation names both paths (rename the operator's
  resource, or disable the plugin's and keep the name). Disable-and-redeclare is the sanctioned
  replacement flow, and the substitution is surfaced in provenance, so dependents of the name never
  silently receive something other than what they were built for. Defaults-with-override semantics
  (a synthesized row that an operator declaration replaces, with provenance shown) are reserved for
  surfaces that declare them deliberately; wave 3's synthesized secret sources are the canonical
  case (`docs/sdd/2026-08-07-secret-sources/`).
- C5. **Declared-resource move only** (operator ruling, 2026-08-08). Generic apt and install-command
  execution stays core. Snap, mise, dotfiles, tmuxinator, and Claude setup are not moved or
  behaviorally changed by this child. Moving any of those later requires its own scoped effort and,
  for Claude user setup, the roadmap's harness-integration user facet.

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
- AC3. The 16 inventoried rows no longer publish from `manifests/builtin`; they publish from the
  `apt` or `install-command` plugin with unchanged names and specs. Core initializer execution is
  unchanged.
- AC4. `agw guide` teaches the new plugins through their own topic contributions.
- AC5. Disabling a plugin-provided resource and declaring an operator resource under the same name
  yields the operator's resource, with the substitution shown by `describe`; the same config without
  the disable is a hard error naming both remediations.
- AC6. A disabled resource still referenced by another resource fails finalize with an error naming
  the reference and the remediation; the same disabled resource unreferenced simply leaves the
  normal views.

## Resolved design decisions

- [HLA D1](./hla.md#d1-add-a-settings-only-resource-disable-policy) defines the settings spelling
  and selector validation.
- [HLA D2 through D4](./hla.md#d2-collect-a-complete-provider-claim-ledger) define complete-claim,
  publication-order-independent collision adjudication and retained provenance.
- [HLA D5](./hla.md#d5-reject-enabled-resource-edges-to-disabled-targets-at-finalize) defines the
  finalize invariant and remediation contract; describe and doctor render the same stored evidence.
