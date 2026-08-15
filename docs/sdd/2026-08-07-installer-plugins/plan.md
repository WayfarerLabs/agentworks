# Implementation Plan: Installer Resource Plugins

- Status: Revised for artifact review
- Date: 2026-08-14
- Scope authority: [FRD](./frd.md)
- Architecture: [HLA](./hla.md)
- Migration: [migration strategy](./migration-strategy.md)

## Delivery shape

This is one implementation PR after the revised artifact review. The change is a single coherent
move: both plugins, all 16 rows, tests, guides, sample config, permanent docs, and upgrade teaching
land together. The artifact revision merges first because this is an active saga child.

## Phase 0: revised artifact set

- [x] Integrate the delivered saga-rename message and remove the message file.
- [x] Replace the seed's broad scope with the 16-row inventory, revised FRD, HLA, migration
      strategy, and implementation plan.
- [x] Run scoped documentation gates and obtain a clean independent artifact review.
- [x] Publish a draft artifact PR for saga-lead review, address findings, promote it when directed,
      and merge it before implementation.

Definition of done: the artifacts name exactly two manifest-only plugins and contain no disable,
collision, initializer extension, or non-declared-installer implementation work.

## Phase 1: green manifest-only plugin move

- [x] Add and register the `apt` and `install-command` plugin descriptors with empty capabilities.
- [x] Move the ten apt and six user install-command rows unchanged into the owning packages.
- [x] Remove the now-empty built-in installer manifest files and update source comments that name
      their ownership.
- [x] Make the plugin manifest provenance source derive from the actual manifest package anchor so
      the hyphenated plugin name records a truthful import path.
- [x] Preserve the exact payload oracle while changing its provider and enablement expectations.
- [x] Prove the two descriptors are shipped and disabled by default, and pin truthful provenance
      from each manifest anchor. Rely on the existing generic framework tests for publication,
      gating, row precedence, and multiple enabled plugins.

Definition of done: each of the 16 selectors has exactly one app-shipped provider, both plugins are
installed and disabled by default, all affected production tests pass, and no model, reference,
predicate, runner, or initializer moved.

## Phase 2: guide and operator teaching

- [ ] Add one complete conceptual guide topic under each plugin package through guide-scoped,
      first-party adapters selected by a module-local two-entry mapping; keep ordinary plugin
      imports I/O-free and leave the public plugin contract unchanged.
- [ ] Add one import-boundary test proving neither plugin import reads guide content. Rely on the
      existing generic guide tests for candidate validation and completion behavior.
- [ ] Extend installed-wheel coverage for both manifest YAML bundles and require both new guide
      topics to survive the existing catalog probe without scoped issues; rely on the existing
      global guide-content glob for the Markdown assets themselves.
- [ ] Update source ownership comments, the sample config, CLI and plugin READMEs, resource guide,
      idempotency guidance where affected, and the 0.14 upgrade guide.
- [ ] Confirm no completion implementation or generated artifact change is required.

Definition of done: guide reads are request-scoped, the installed artifact carries both asset
families, and every permanent operator surface teaches the opt-in ownership and composite dependency
behavior accurately.

## Phase 3: verification and closeout

- [ ] Run scoped lint, type, and test gates, then the full repository gates required by CI.
- [ ] Build and install the wheel in an isolated environment and load both plugins' manifests and
      guide content through package resources.
- [ ] Exercise the shipped CLI in an isolated home with each plugin disabled and enabled, including
      list, use gating, guide, completion, and doctor scenarios.
- [ ] Obtain clean independent implementation, documentation, and fresh-eyes reviews; resolve every
      valid finding.
- [ ] Record any unavailable live backend as an explicit test gap. Do not require a remote VM when
      unchanged executor tests and installed-artifact CLI acceptance cover the declaration move.
- [ ] Update the plan truthfully, add `locked.md`, and deliver the breaking change with a
      conventional commit containing a `BREAKING CHANGE:` footer.

Definition of done: AC1 through AC7 have evidence, CI and installed-wheel gates pass, permanent docs
are current, no test residue remains, and the SDD is ready to lock with the implementation.
