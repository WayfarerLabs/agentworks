# FRD: Installer Resource Plugins (Pre-0.14)

- Status: Revised for artifact review
- Date: 2026-08-14
- Parent: the `2026-08-04-next-steps` saga
- Review: the effort lead owns these artifacts and implementation; the saga lead reviews the
  artifact and implementation PRs

## Purpose

Agentworks ships optional apt and user install-command declarations from the built-in manifest
package. These rows are useful catalog entries, not defaults that core requires. Bucket the existing
declarations into opt-in system plugins so their provider matches their optional ownership while the
generic resource framework and initializer execution stay in core.

This is the complete scope. The operator's 2026-08-13 correction supersedes the merged seed at
`bbe9a506` and the broader design that followed it. Seed R7 and its possible `consumer_gating`
trigger, C4's collision redesign, and the bundle growth path are not commitments of this child; the
saga's 2026-08-13 correction defers collision redesign and this child makes no promise for the other
removed concepts.

## Requirements

- R1. Add an `apt` system plugin that owns the five existing `apt-source` rows and five existing
  `apt-package` rows.
- R2. Add an `install-command` system plugin that owns the six existing `user-install-command` rows.
- R3. Relocate all 16 rows without changing their names, specifications, dependency edges, selection
  fields, or execution order.
- R4. Both plugins are installed with Agentworks, disabled by default, and enabled through the
  existing `[plugins].system` setting.
- R5. Preserve current generic publication, visibility, disabled-plugin use gating, and same-name
  row precedence. A dependency keeps its own enablement state even when an operator declaration wins
  the referring row.
- R6. Core continues to own apt and install-command kinds, validation, reference extraction,
  execution, idempotency, and initializer orchestration.
- R7. Each plugin contributes concise guide teaching, and the sample config, permanent docs,
  completions assessment, and 0.14 upgrade guide reflect the ownership change.
- R8. The packaged wheel contains both plugins' manifests and guide content.
- R9. The breaking ownership change ships in 0.14.0.
- R10. The standard disabled-resource message is the complete transition experience. Do not add a
  warning runway, compatibility alias, automatic enablement, migrator, special remediation, or
  downgrade support.

## Scope boundaries

The following remain core and are not changed by this effort:

- snap installation
- mise installation and configuration
- dotfiles synchronization
- tmuxinator installation and workspace configuration
- Claude marketplace and plugin setup
- all initializer lifecycle and remote execution code
- all raw package and command configuration fields

No initializer capability, callback, execution seat, resource-disable setting, collision resolver,
or consumer gate is introduced.

## Acceptance

- AC1. The `apt` plugin is the only app-shipped provider of the five named apt sources and five
  named apt packages.
- AC2. The `install-command` plugin is the only app-shipped provider of the six named user install
  commands.
- AC3. With the owning plugin enabled, every moved selector resolves to its unchanged payload and is
  consumed by the unchanged core execution path.
- AC4. A same-name operator declaration continues to win row precedence. If that row references a
  moved dependency, the standard gate refuses the disabled dependency unless the operator enables
  its plugin or replaces or removes the dependency.
- AC5. Resource completion follows the visible enabled inventory, while guide completion retains
  discoverable conceptual and dynamic topics, including topics for disabled rows. Neither gains a
  new completion mechanism.
- AC6. The CLI docs, resource guide, plugin docs, sample config, conceptual guide topics, and 0.14
  upgrade guide consistently teach the two opt-ins and all 16 moved selectors.
- AC7. Installed-wheel coverage proves that both manifest bundles ship and remain readable, while
  the existing package-data coverage continues to include their guide content.

## Compatibility ruling

The operator waived the saga's default one-release warning runway on 2026-08-14. No shipped template
or sample selects these optional catalog rows; affected configurations explicitly name one of the 16
moved resources. R10 is the authoritative transition contract.
