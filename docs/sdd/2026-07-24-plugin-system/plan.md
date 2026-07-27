# Plan: system plugins (initial structure)

**Status:** Parked (post-reset 2026-07-27), blocked on the registry readiness refactor.

Implements the [FRD](./frd.md) via the [HLA](./hla.md); see the design-dependency note in both. This
effort was reset on 2026-07-27 (implementation dropped, FRD and HLA kept) because the plugin
resolution and disablement semantics depend on a registry change that warrants its own SDD. This
plan is the resumption path. Phases are ordered; the effort does not restart until Phase 0's
dependency lands.

## Phase 0 (dependency, not owned here): the registry readiness refactor lands

The registry redesign is a separate, prerequisite SDD. In brief, it decouples graph construction
from config validation (a total, non-throwing structural graph, with validation a separate pass over
the enabled set); makes disablement a reverse-topological fold that hands each node its
dependencies' verdicts; and splits two signals we had been conflating, "can't run" (resource
readiness, graph-following, present-but-dormant) from "not enabled" (plugin absence,
reference-diagnosed), renaming the readiness hook (today `disabled_reason`) accordingly.

- [ ] The registry readiness refactor SDD is merged. (Blocks every phase below.)

## Phase 1: reconcile the plugin SDD artifacts against the landed refactor

The first real step after the reset. With the registry model settled, revise the parked artifacts so
they describe the plugin work against the new registry, then remove the placeholder notes.

- [ ] FRD: revise R5, R7, R9 and the "disabled" terminology to the landed readiness/enablement
      model; reconfirm the motivation and the empty-installed-set framing; drop the
      design-dependency note.
- [ ] HLA: refresh "Current state" to the post-refactor registry; revise the collision extension
      (component 6b) and the present-but-disabled roster (component 7) against the readiness split;
      drop the design-dependency note.
- [ ] Author the ADR (staged un-numbered in this dir until lock, to dodge an ADR-number race),
      recording the plugin decisions as they stand against the new registry.
- [ ] Fill this plan in at implementation granularity (Phases 2+ below are the skeleton).

## Phase 2: rebuild the plugin framework from scratch

Rebuild `cli/agentworks/plugins/` clean against the new registry, per the reconciled HLA (the origin
variant, the `[plugins]` config, the collision matrix, the doctor roster). Build the pre-reset
architecture reviews' hardening in from the start rather than rediscovering it:

- [ ] `register_plugin` is atomic and validating: validate the whole descriptor (every capability
      kind has an adapter; every impl is a class with a non-empty, slash-free `name`; no
      collisions), then seat. No partial seating on error.
- [ ] Typed error on a duplicate plugin-name in the installed index (the name is the identity the
      origin taxonomy and the future trust model hang off).
- [ ] Descriptor validated at registration, closing the instance-vs-class trap (the `secret-backend`
      kind is the natural one because its built-ins are instances).
- [ ] Publication is tied to seating (`publish_enabled` verifies each impl is seated before it
      publishes a row).
- [ ] Registration control is inverted: the index imports each plugin module and calls
      `register_plugin(module.PLUGIN)` itself, so failures carry plugin attribution and provenance
      is derived from the real module (not the self-declared name).
- [ ] Capability rows honor the host-support gate: the adapter gains a per-kind `publishable` hook
      wired to `unsupported_reason`, with a parity test against the doctor surface.
- [ ] Manifests: a typed error for a missing manifests directory; a `ConfigError` (not an `assert`,
      which is stripped under `python -O`) for load issues.
- [ ] The reserved doors are typed to their vocabularies (`required_scopes` to `ScopeLevel`;
      `commands` given a real placeholder frame).

## Phase 3: prove the mechanism

- [ ] A test-fixture plugin (test-local index) exercises the whole path end to end against the new
      registry: enabled publishes and resolves; not-enabled is absent and reference-diagnosed; the
      collision matrix holds; enable-then-disable within one process leaves the seated impl absent.
- [ ] Author the plugin contract docs (`cli/agentworks/plugins/README.md`).

## Phase 4 (optional, separate value): migrate the built-ins into plugins

Only once the framework is solid. Each is a deliberate breaking change (operators must enable the
plugin). The pre-reset exploration of all three is recoverable in git if useful.

- [ ] `onepassword` (secret-backend).
- [ ] `proxmox` (vm-platform).
- [ ] `azure` (vm-platform `azure-vm` + git-credential-provider `azdo` + the `az-cli`
      system-install-command as a bundled plugin manifest).
