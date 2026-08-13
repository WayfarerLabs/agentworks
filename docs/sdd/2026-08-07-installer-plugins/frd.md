# FRD: Installer Resource Plugins (Pre-0.14)

- Status: Revised for the 2026-08-13 scope correction
- Date: 2026-08-13
- Parent: the `2026-08-04-next-steps` saga
- Review: the effort lead owns these artifacts and implementation; the saga lead reviews the
  artifact and implementation PRs

## Purpose

Agentworks ships optional apt and user install-command declarations from the built-in manifest
package. These rows are useful catalog entries, not defaults that core requires. Bucket the existing
declarations into opt-in system plugins so their provider matches their optional ownership while the
generic resource framework and initializer execution stay in core.

This is the complete scope. The operator's 2026-08-13 correction supersedes the broader design in
unmerged PR #451.

## Requirements

- R1. Add an `apt` system plugin that owns the five existing `apt-source` rows and five existing
  `apt-package` rows.
- R2. Add an `install-command` system plugin that owns the six existing `user-install-command` rows.
- R3. Relocate all 16 rows without changing their names, specifications, dependency edges, selection
  fields, or execution order.
- R4. Both plugins are installed with Agentworks, disabled by default, and enabled through the
  existing `[plugins].system` setting.
- R5. Preserve the current generic resource behavior, including disabled-plugin use gating and
  same-name operator override behavior. Improving either contract is deferred.
- R6. Core continues to own apt and install-command kinds, validation, reference extraction,
  execution, idempotency, and initializer orchestration.
- R7. Each plugin contributes concise guide teaching, and the sample config, permanent docs,
  completions assessment, and 0.14 upgrade guide reflect the ownership change.
- R8. The packaged wheel contains both plugins' manifests and guide content.
- R9. The breaking ownership change ships in 0.14.0.

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
consumer gate, compatibility alias, or migrator is introduced.

## Acceptance

- AC1. The `apt` plugin is the only app-shipped provider of the five named apt sources and five
  named apt packages.
- AC2. The `install-command` plugin is the only app-shipped provider of the six named user install
  commands.
- AC3. With the owning plugin enabled, every moved selector resolves to its unchanged payload and is
  consumed by the unchanged core execution path.
- AC4. With the owning plugin disabled, current registry visibility, use-gate, and operator override
  behavior remain unchanged.
- AC5. Resource completion follows the visible enabled inventory, while guide completion retains
  discoverable conceptual and dynamic topics, including topics for disabled rows. Neither gains a
  new completion mechanism.
- AC6. The CLI docs, resource guide, plugin docs, sample config, conceptual guide topics, and 0.14
  upgrade guide consistently teach the two opt-ins and all 16 moved selectors.
- AC7. Source and installed-wheel tests prove that both manifest bundles and their guide content are
  packaged and readable.
